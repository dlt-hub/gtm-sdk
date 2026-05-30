"""Attio OAuth-scope preflight for the record-writer path.

Why this exists (ai-ica): the prod ``ATTIO_API_KEY`` had
``record_permission:read-write`` (so company/person/meeting upserts worked) but
only ``object_configuration:read`` — so the meeting-lifecycle dispatcher's
just-in-time ``ensure_select_options`` POST (seeding ``event_type:calcom_meeting``)
failed with Attio's opaque *"...does not exist or you do not have permission to
access it."* The failure surfaced four ops deep inside a write, after three other
ops had already succeeded, making it look like a per-object access bug.

This module calls ``GET /v2/self`` once per token at the orchestration entrypoint
and turns a missing scope into a legible, actionable error *before* any write —
raising :class:`AttioScopeError` for a missing required scope and logging a
warning for a missing recommended scope. It deliberately lives at the entrypoint
(webhook ``_export``, backfill scripts), NOT inside ``src.attio.export.execute``,
so unit tests that mock the client don't pay a ``/v2/self`` round-trip.
"""

from __future__ import annotations

import logging

from libs.attio.client import get_client, resolve_api_key
from libs.attio.errors import AttioScopeError

logger = logging.getLogger(__name__)

# Hard requirement for ANY Attio record write. Without this the writer path
# cannot create or patch records at all.
REQUIRED_WRITE_SCOPES: frozenset[str] = frozenset({"record_permission:read-write"})

# Needed only when the path mutates schema at runtime: ``ensure_select_options``
# POSTing a new option, or ``create_attribute`` adding an attribute. If the
# target schema is fully pre-bootstrapped (see
# scripts/attio-bootstrap-tracking_events.py) the JIT calls become no-op GETs and
# this scope is not required at runtime — hence "recommended", surfaced as a
# warning rather than a hard failure.
RECOMMENDED_WRITE_SCOPES: frozenset[str] = frozenset(
    {"object_configuration:read-write"},
)

# Process-level cache of validations already performed, so backfill loops that
# call the writer once per row don't re-hit /v2/self each time. Keyed by
# (token, required scopes, recommended scopes) — NOT token alone: the webhook
# path checks `record_permission:read-write` while the bootstrap checks
# `object_configuration:read-write`, and a lenient pass must not suppress a
# later stricter check for the same token in a long-lived process.
#
# The key holds the raw token. These caches are in-memory and process-local;
# the token is never logged, persisted, or otherwise exposed by living here, and
# it already resides in the env var / contextvar for the process lifetime. We do
# NOT hash it for a key: a fast crypto hash adds no real protection for an
# in-memory dict key and trips CodeQL's weak-sensitive-data-hashing rule.
_ValidationKey = tuple[str, frozenset[str], frozenset[str]]
_validated_fingerprints: set[_ValidationKey] = set()

# Cache of token -> the token's authorizing workspace-member id (``/v2/self``
# ``authorized_by_workspace_member_id``). Used to stamp the owner of records the
# token writes. Workspace-member ids are per-workspace, so this must be resolved
# from the live token, never hardcoded (ai-ica: a hardcoded dev-era UUID broke
# the prod meeting-lifecycle owner write).
_owner_member_cache: dict[str, str] = {}


def reset_scope_cache() -> None:
    """Clear the per-token caches (scope-validation + owner member id).

    Intended for tests that re-exercise the preflight across token states; the
    caches are otherwise process-lived by design.
    """
    _validated_fingerprints.clear()
    _owner_member_cache.clear()


def resolve_owner_member_id(api_key: str | None = None) -> str | None:
    """Return the active token's authorizing workspace-member id, or ``None``.

    Reads ``authorized_by_workspace_member_id`` from ``GET /v2/self`` — the
    member who created the token, which is the correct per-workspace "owner"
    actor for records the token writes (e.g. cal.com meeting-lifecycle rows).
    Cached per token. Returns ``None`` (rather than raising) when the field is
    absent, so callers can treat owner as best-effort metadata and omit it
    rather than fail the whole write.
    """
    token = resolve_api_key(api_key)
    cached = _owner_member_cache.get(token)
    if cached is not None:
        return cached
    try:
        with get_client(api_key) as client:
            identity = client.meta.get_v2_self()
    except Exception:  # noqa: BLE001
        # owner is best-effort metadata: a transient /v2/self failure must NOT
        # abort the record write that called us. Degrade to None (caller omits
        # owner) and let the next write retry the lookup (not cached on failure).
        logger.warning("attio_owner_member_lookup_failed", exc_info=True)
        return None
    member_id = getattr(identity, "authorized_by_workspace_member_id", "") or ""
    if member_id:
        _owner_member_cache[token] = member_id
        return member_id
    return None


def fetch_token_scopes(api_key: str | None = None) -> tuple[bool, set[str], str]:
    """Return ``(active, scopes, workspace_slug)`` from ``GET /v2/self``.

    ``scopes`` is the parsed set from the space-separated ``scope`` string. An
    inactive token (or one whose response omits ``scope``) yields an empty set.
    """
    with get_client(api_key) as client:
        identity = client.meta.get_v2_self()
    active = bool(getattr(identity, "active", False))
    scope_str = getattr(identity, "scope", "") or ""
    workspace = getattr(identity, "workspace_slug", "") or ""
    return active, set(scope_str.split()), workspace


def assert_attio_token_scopes(
    *,
    required: frozenset[str] = REQUIRED_WRITE_SCOPES,
    recommended: frozenset[str] = RECOMMENDED_WRITE_SCOPES,
    api_key: str | None = None,
    force: bool = False,
) -> None:
    """Fail fast if the active Attio token lacks a required scope.

    Raises :class:`AttioScopeError` (with ``.missing``) when a ``required`` scope
    is absent or the token is inactive. Logs a warning — but does not raise —
    when a ``recommended`` scope is absent, naming the consequence (runtime JIT
    schema seeding will fail) and the remedy (pre-bootstrap the schema).

    The result is cached per (token, required, recommended) for the process
    lifetime; pass ``force=True`` to bypass the cache (tests, deliberate
    re-checks).
    """
    token = resolve_api_key(api_key)
    cache_key: _ValidationKey = (token, required, recommended)
    if not force and cache_key in _validated_fingerprints:
        return

    active, scopes, workspace = fetch_token_scopes(api_key)

    if not active:
        msg = (
            "Attio token is inactive (GET /v2/self reported active=false). "
            "Regenerate the token in Attio → Settings → Developers and update "
            "it in Infisical."
        )
        raise AttioScopeError(msg, missing=sorted(required))

    missing_required = sorted(required - scopes)
    if missing_required:
        msg = (
            f"Attio token (workspace={workspace!r}) is missing required "
            f"scope(s) {missing_required} for the record-writer path. Present "
            f"scopes: {sorted(scopes)}. Regenerate the token in Attio → "
            "Settings → Developers with the full read-write scope set "
            "(matching dev) and update it in Infisical."
        )
        raise AttioScopeError(msg, missing=missing_required)

    missing_recommended = sorted(recommended - scopes)
    if missing_recommended:
        logger.warning(
            "attio_token_missing_recommended_scope",
            extra={
                "workspace": workspace,
                "missing_recommended": missing_recommended,
                "consequence": (
                    "runtime just-in-time schema seeding (ensure_select_options / "
                    "create_attribute) will fail with a permission error unless "
                    "the target schema is pre-bootstrapped via "
                    "scripts/attio-bootstrap-tracking_events.py"
                ),
            },
        )

    _validated_fingerprints.add(cache_key)
