"""Integration-suite preflight: fail loudly when required Attio objects are missing.

The nightly ``tests-integration`` GitHub Actions job runs ``pytest -m integration``
against the workspace pointed to by ``ATTIO_API_KEY``. Several live regression
tests (notably ``test_attio_mention_writer_live.py`` for AI-290) gate themselves
on the ``social_mention`` custom object existing — when it's missing they
``pytest.skip`` cleanly, which means the nightly job stays green even though the
regression coverage is silently absent.

This conftest closes the gap. When integration tests are collected, it probes
the target workspace for a fixed set of required custom objects and, if any are
missing, emits a GitHub Actions ``::error::`` workflow command (surfaces as a
red banner on the run page regardless of step exit code) and exits the pytest
session non-zero. The annotation references the beads issue tracking the
bootstrap blocker so the next operator knows exactly where to follow up.

See ai-0ou (this guard) and ai-o32 (dev workspace at Attio object cap).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

import pytest

# Required custom objects for the integration suite. Add a slug here when a
# new live test depends on a custom object existing in the target workspace.
_REQUIRED_OBJECT_SLUGS: frozenset[str] = frozenset({"social_mention"})

# Maps a required object slug to the beads issue that tracks bootstrapping
# it in the target workspace. Surfaced in the failure message so the next
# operator does not have to spelunk for context.
_BOOTSTRAP_BLOCKERS: dict[str, str] = {
    "social_mention": "ai-o32",
}

# Attio API keys are 64 chars per the API's own validation. A shorter value
# is a placeholder/stub — treat it as "no real credentials" and skip the
# precheck rather than emitting a misleading red banner.
_ATTIO_KEY_MIN_LEN = 64

# Exit code used when the preflight aborts because a required Attio object is
# missing ("infra not ready"), deliberately distinct from pytest's own reserved
# codes (0–5) and from 1 (a genuine test failure) so operators can tell an
# unbootstrapped workspace apart from a real regression at a glance. Both are
# non-zero, so CI is RED either way. Keep in sync with the constant of the same
# name in .github/workflows/ci/pytest_integration_dagger.py.
PREFLIGHT_MISSING_OBJECT_RC = 86


def _emit_gh_actions_error(message: str) -> None:
    # GitHub Actions surfaces "::error::" workflow commands as red banners on
    # the run summary regardless of step exit code. Emit on stderr to coexist
    # with pytest's own stdout.
    # https://docs.github.com/en/actions/learn-github-actions/workflow-commands-for-github-actions
    sys.stderr.write(f"::error::{message}\n")
    sys.stderr.flush()


def _format_missing_objects_message(
    missing: Iterable[str],
    blockers: dict[str, str],
) -> str:
    parts: list[str] = []
    for slug in sorted(missing):
        blocker = blockers.get(slug)
        suffix = f" (blocked on {blocker})" if blocker else ""
        parts.append(f"{slug}{suffix}")
    return (
        "Integration suite preflight failed: required Attio object(s) missing "
        f"in target workspace: {', '.join(parts)}. The corresponding live "
        "regression test(s) would otherwise skip silently and let real "
        "failures land green. Bootstrap with scripts/attio-bootstrap-social_mentions.py "
        "against an unblocked workspace, or free a slot in the target one."
    )


def _has_real_attio_credentials() -> bool:
    key = os.environ.get("ATTIO_API_KEY", "").strip()
    return bool(key) and len(key) >= _ATTIO_KEY_MIN_LEN


def _fetch_existing_object_slugs() -> set[str] | None:
    """Probe Attio for current object slugs. Returns None on auth failure.

    Auth failures are intentionally swallowed because the per-test fixtures
    in ``tests/conftest.py`` (``attio_auth_probe``) already convert 401/403
    into a clean skip with a clear message. The point of *this* precheck is
    object existence, not credentials — let auth flow through its own path.
    """
    # Local imports keep test collection cheap when ATTIO_API_KEY isn't set
    # (the probe is never invoked in that case).
    from attio.errors import SDKError

    from libs.attio.objects import list_object_api_slugs

    try:
        return list_object_api_slugs()
    except SDKError as exc:
        if exc.status_code in (401, 403):
            return None
        raise


def pytest_collection_modifyitems(
    session: pytest.Session,  # noqa: ARG001
    config: pytest.Config,  # noqa: ARG001
    items: list[pytest.Item],
) -> None:
    if not any("integration" in item.keywords for item in items):
        return
    if not _has_real_attio_credentials():
        return

    existing = _fetch_existing_object_slugs()
    if existing is None:
        return

    missing = set(_REQUIRED_OBJECT_SLUGS) - existing
    if not missing:
        return

    message = _format_missing_objects_message(missing, _BOOTSTRAP_BLOCKERS)
    _emit_gh_actions_error(message)
    pytest.exit(reason=message, returncode=PREFLIGHT_MISSING_OBJECT_RC)
