#!/usr/bin/env -S uv run python
"""Verify the Attio meeting↔person/company link does not strip prior links.

This gates the Fathom→Attio meeting backfill (ai-crf). Per the
`attio-inverse-relationship-multiselect` knowledge: Attio enforces inverse-link
uniqueness based on the *other* side's `is_multiselect`. If the inverse
attribute on `people`/`companies` (the record-reference pointing back at
`meetings`) is single-valued, then each person/company can be linked to at most
ONE meeting at a time — linking a person to a new meeting would synchronously
strip them off the meeting they were previously linked to. Running the
204-recording backfill against a single-valued inverse would silently corrupt
links as it walks the recordings.

The meetings feature is ALPHA and provisioned only in PROD (dev POST /v2/meetings
404s), so this is verifiable only against the prod `dltHub` workspace.

Two modes:

  * Config inspection (default, READ-ONLY): reads `is_multiselect` on the
    people/companies record-reference attributes that point at meetings, and
    dumps the `relationship` object on the meeting linking attribute when the
    alpha schema exposes it. Emits a config verdict. Because the alpha meetings
    schema may not expose these attributes via the objects/attributes API, a
    missing inverse attribute is reported as INCONCLUSIVE (not a hard STOP) —
    the empirical mode below is the authoritative gate.

  * Empirical (`--idempotency-check --execute`, WRITES TO PROD): creates
    clearly-labeled throwaway meetings and proves two things directly:
      1. Idempotency — re-POSTing one meeting with the same ical_uid + links
         converges to the same meeting with `linked_records` identical
         (set-union, no duplicate append, no second meeting created).
      2. No contention — linking the SAME person AND company to a second
         meeting does NOT strip either off the first meeting (the real
         backfill-gating concern), exercising both independent inverses.
    The organizer/participant email is kept distinct from the probed person so
    participant auto-linking cannot mask a single-valued linked_records inverse,
    and a preflight refuses to run unless the probed records have zero existing
    meeting links (so the probe can never strip a real link). /v2/meetings has
    no DELETE, so these test meetings are permanent; they are named
    `ai-3hq-verify-*` and join the handful of existing test meetings.

The Infisical environment is explicit (no silent prod default): pass `--env` or
set `INFISICAL_ENV`. The script self-bootstraps `infisical run` from
`gtm-sdk/.env.local` when `ATTIO_API_KEY` is not already injected.

Usage:

    uv run python scripts/attio-inspect-meeting-relationship.py --env prod
    uv run python scripts/attio-inspect-meeting-relationship.py --env prod \
        --idempotency-check --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.env import (  # noqa: E402
    clean_env,
    infisical_run_example,
    read_infisical_credentials,
)

# Sentinel propagated through `os.execvp` -> `infisical run` -> child python.
# Prevents an infinite re-bootstrap loop when the chosen Infisical env simply
# does not contain `ATTIO_API_KEY`.
_BOOTSTRAP_SENTINEL_ENV = "_ATTIO_MEETING_REL_BOOTSTRAPPED"

# The object slug under which Attio exposes meetings. Used both for the schema
# inspection target and for reading a meeting back via the meetings sub-SDK.
_MEETINGS_SLUG = "meetings"


# ---------------------------------------------------------------------------
# Pure verdict logic (unit-tested without a live workspace)
# ---------------------------------------------------------------------------

ConfigStatus = Literal["pass", "stop", "inconclusive"]


@dataclass(frozen=True)
class InverseAttr:
    """A people/companies record-reference attribute pointing at meetings."""

    target_object: str  # "people" | "companies"
    api_slug: str
    is_multiselect: bool


@dataclass
class ConfigVerdict:
    status: ConfigStatus
    reasons: list[str] = field(default_factory=list)
    inverse_attrs: list[InverseAttr] = field(default_factory=list)


def evaluate_inverse_multiselect(
    people_inverse: list[InverseAttr],
    company_inverse: list[InverseAttr],
) -> ConfigVerdict:
    """Decide the config-level verdict from the discovered inverse attributes.

    PASS only if BOTH people and companies expose at least one meetings-pointing
    record-reference attribute and EVERY such attribute is `is_multiselect=true`.
    STOP if any discovered inverse attribute is single-valued (`is_multiselect`
    false) — that is the corruption-causing configuration. INCONCLUSIVE if a
    side exposes no inverse attribute at all (the alpha meetings schema may not
    surface it via the objects/attributes API); the empirical mode is then the
    authoritative gate.
    """
    found = [*people_inverse, *company_inverse]
    single_valued = [a for a in found if not a.is_multiselect]
    if single_valued:
        reasons = [
            f"{a.target_object}.{a.api_slug} is_multiselect=false "
            "(single-valued inverse → linking strips prior meeting links)"
            for a in single_valued
        ]
        return ConfigVerdict(status="stop", reasons=reasons, inverse_attrs=found)

    missing_sides = [
        side
        for side, attrs in (("people", people_inverse), ("companies", company_inverse))
        if not attrs
    ]
    if missing_sides:
        reasons = [
            f"No meetings-pointing record-reference attribute found on "
            f"{side} via the objects/attributes API"
            for side in missing_sides
        ]
        reasons.append(
            "Alpha meetings schema may not expose the inverse attribute; rely on "
            "the empirical --idempotency-check run for the authoritative gate.",
        )
        return ConfigVerdict(
            status="inconclusive",
            reasons=reasons,
            inverse_attrs=found,
        )

    return ConfigVerdict(
        status="pass",
        reasons=[f"{a.target_object}.{a.api_slug} is_multiselect=true" for a in found],
        inverse_attrs=found,
    )


def _linked_record_keys(linked_records: list[Any]) -> set[tuple[str, str]]:
    """Normalize a meeting's `linked_records` into a set of (object, record_id).

    Attio's read model exposes ``object_slug``; the write model uses ``object``.
    Accept either so the same helper works on SDK objects and plain dicts.
    """
    keys: set[tuple[str, str]] = set()
    for lr in linked_records:
        if isinstance(lr, dict):
            obj = lr.get("object_slug") or lr.get("object") or ""
            rid = lr.get("record_id") or ""
        else:
            obj = getattr(lr, "object_slug", None) or getattr(lr, "object", "") or ""
            rid = getattr(lr, "record_id", "") or ""
        keys.add((str(obj), str(rid)))
    return keys


def _has_duplicate_links(linked_records: list[Any]) -> bool:
    """True if the same (object, record_id) appears more than once."""
    seen: set[tuple[str, str]] = set()
    for lr in linked_records:
        if isinstance(lr, dict):
            obj = lr.get("object_slug") or lr.get("object") or ""
            rid = lr.get("record_id") or ""
        else:
            obj = getattr(lr, "object_slug", None) or getattr(lr, "object", "") or ""
            rid = getattr(lr, "record_id", "") or ""
        key = (str(obj), str(rid))
        if key in seen:
            return True
        seen.add(key)
    return False


# ---------------------------------------------------------------------------
# Live Attio reads/writes
# ---------------------------------------------------------------------------


def _discover_inverse_attrs(target_object: str) -> list[InverseAttr]:
    """Find record-reference attributes on ``target_object`` pointing at meetings.

    Read-only. Uses ``libs.attio.attributes.list_attributes`` whose normalized
    ``AttributeInfo`` resolves ``allowed_objects`` to api_slugs, so we can filter
    on the ``meetings`` slug directly.
    """
    from libs.attio.attributes import list_attributes

    out: list[InverseAttr] = []
    for attr in list_attributes(target_object):
        if attr.attribute_type != "record-reference":
            continue
        if _MEETINGS_SLUG not in attr.allowed_objects:
            continue
        out.append(
            InverseAttr(
                target_object=target_object,
                api_slug=attr.api_slug,
                is_multiselect=attr.is_multiselect,
            ),
        )
    return out


def _dump_meeting_relationship() -> list[dict[str, Any]]:
    """Raw-dump the `relationship`/config on the meeting object's attributes.

    The memory's recommended config check is to read ``relationship.is_multiselect``
    on the attribute you write to. ``AttributeInfo`` drops the ``relationship``
    field, so we hit the SDK directly. Returns ``[]`` when the alpha meetings
    object is not exposed via the objects/attributes API (404), which is an
    expected outcome we treat as INCONCLUSIVE rather than an error.
    """
    from attio.errors.sdkerror import SDKError

    from libs.attio.client import get_client

    dumped: list[dict[str, Any]] = []
    with get_client() as client:
        try:
            response = client.attributes.get_v2_target_identifier_attributes(
                target="objects",
                identifier=_MEETINGS_SLUG,
                show_archived=False,
            )
        except SDKError as exc:
            status = getattr(getattr(exc, "raw_response", None), "status_code", None)
            if status == 404:
                return []
            raise
        for a in response.data:
            if getattr(a, "type", "") != "record-reference":
                continue
            relationship = getattr(a, "relationship", None)
            dumped.append(
                {
                    "api_slug": getattr(a, "api_slug", ""),
                    "is_multiselect": bool(getattr(a, "is_multiselect", False)),
                    "relationship": _to_plain(relationship),
                },
            )
    return dumped


def _to_plain(value: Any) -> Any:
    """Best-effort convert an SDK model to a JSON-serializable structure."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:  # noqa: BLE001 — diagnostic dump, never fatal
            return str(value)
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return str(value)


def _read_meeting_links(
    meeting_id: str,
    *,
    expect: set[tuple[str, str]] | None = None,
    attempts: int = 4,
    backoff_seconds: float = 0.5,
) -> list[Any]:
    """GET a meeting and return its `linked_records` list.

    Attio reads are eventually consistent, so a read immediately after a write
    can lag. When ``expect`` is given, retry with linear backoff until every
    expected ``(object, record_id)`` key is present (or attempts are exhausted).
    This only delays the FAILURE case — a genuinely stripped/absent link never
    appears, so the bounded retries lapse and the real (incomplete) state is
    returned, keeping the verdict correct rather than masking a true negative.
    """
    from libs.attio.client import get_client

    links: list[Any] = []
    for attempt in range(attempts):
        with get_client() as client:
            response = client.meetings.get_v2_meetings_meeting_id_(
                meeting_id=meeting_id,
            )
        links = list(getattr(response.data, "linked_records", []) or [])
        if expect is None or expect.issubset(_linked_record_keys(links)):
            return links
        if attempt + 1 < attempts:
            time.sleep(backoff_seconds * (attempt + 1))
    return links


def _existing_meeting_link_count(target_object: str, record_id: str) -> int:
    """Count meetings already linking ``record_id`` (via the meetings list filter)."""
    from libs.attio.client import get_client

    with get_client() as client:
        response = client.meetings.get_v2_meetings(
            linked_object=target_object,
            linked_record_id=record_id,
            limit=1,
        )
    return len(list(getattr(response, "data", []) or []))


def _assert_no_existing_meeting_links(person_id: str, company_id: str) -> None:
    """Abort unless the chosen person and company have zero meeting links.

    SAFETY GATE: the contention probe links these records into fresh meetings. If
    the meeting↔record inverse turned out single-valued (the failure mode we are
    testing for), the very first POST would reassign the inverse away from any
    meeting the record is *already* on — i.e. the diagnostic would mutate real
    production data in the exact corruption scenario it exists to detect. So we
    refuse to run unless the records are effectively sacrificial (no prior
    meeting links). Point --person-email / --company-domain at dedicated test
    records if this fires.

    Residual: Attio reads are eventually consistent, so a link created in the
    same instant elsewhere might not yet be visible here. Retrying cannot
    surface a not-yet-propagated link (it just re-reads zero), so this gate is
    best-effort against that narrow race — choose records that are not being
    concurrently mutated.
    """
    person_links = _existing_meeting_link_count("people", person_id)
    company_links = _existing_meeting_link_count("companies", company_id)
    if person_links or company_links:
        raise SystemExit(
            "Refusing to run the contention probe against records that already "
            "have meeting links (could strip a real link if the inverse is "
            f"single-valued): person={person_id} has {person_links} meeting "
            f"link(s), company={company_id} has {company_links}. Re-run with "
            "--person-email / --company-domain pointing at dedicated, "
            "meeting-unlinked test records.",
        )


def _assert_organizer_safe(organizer_email: str, probed_person_id: str) -> None:
    """Abort if the organizer email is an unsafe choice.

    Attio auto-links a meeting to the Person record matching a participant
    email, so the organizer email is two hazards at once:

    * If it resolves to the SAME Person as the probed person (possible even with
      a different string — aliases, secondary emails, plus-addressing all
      collapse to one record), participant auto-linking would keep that person
      on m1 independently of the explicit ``linked_records`` inverse, masking a
      single-valued inverse. Reject on resolved-id equality, not string equality.
    * If it resolves to a DIFFERENT existing Person that already has meeting
      links, the auto-link on POST could strip a real link. Reject that too.

    If the organizer email resolves to no Person, it is safe: Attio auto-creates
    a fresh throwaway record with no prior links.
    """
    from libs.attio.notes import resolve_record_id_for_ref

    organizer_id = resolve_record_id_for_ref(
        parent_object="people",
        email=organizer_email,
    )
    if organizer_id is None:
        return
    if organizer_id == probed_person_id:
        raise SystemExit(
            f"--organizer-email {organizer_email!r} resolves to the SAME Person "
            f"as --person-email ({probed_person_id}); participant auto-linking "
            "would confound the linked_records contention test. Use an organizer "
            "email that maps to a different (or not-yet-existing) Person.",
        )
    link_count = _existing_meeting_link_count("people", organizer_id)
    if link_count:
        raise SystemExit(
            f"--organizer-email {organizer_email!r} resolves to an existing "
            f"Person ({organizer_id}) with {link_count} meeting link(s); the "
            "auto-link on POST could strip a real link if the inverse is "
            "single-valued. Use a throwaway organizer email that is not an "
            "existing meeting-linked person.",
        )


def _resolve_links(
    person_email: str,
    company_domain: str,
) -> tuple[str, str]:
    """Resolve a real prod person + company record_id to link in test meetings."""
    from libs.attio.notes import resolve_record_id_for_ref

    person_id = resolve_record_id_for_ref(parent_object="people", email=person_email)
    if not person_id:
        raise SystemExit(
            f"Could not resolve a prod Person record for email {person_email!r}. "
            "Pass --person-email pointing at a known prod person.",
        )
    company_id = resolve_record_id_for_ref(
        parent_object="companies",
        domain=company_domain,
    )
    if not company_id:
        raise SystemExit(
            f"Could not resolve a prod Company record for domain {company_domain!r}. "
            "Pass --company-domain pointing at a known prod company.",
        )
    return person_id, company_id


def _post_test_meeting(
    *,
    ical_uid: str,
    title: str,
    person_id: str,
    company_id: str | None,
    organizer_email: str,
    start: datetime,
) -> str:
    """POST a throwaway test meeting; return its meeting_id."""
    from libs.attio.meetings import find_or_create_meeting
    from libs.attio.models import (
        MeetingExternalRef,
        MeetingInput,
        MeetingLinkedRecord,
        MeetingParticipantInput,
    )

    links = [MeetingLinkedRecord(object="people", record_id=person_id)]
    if company_id is not None:
        links.append(MeetingLinkedRecord(object="companies", record_id=company_id))

    envelope = find_or_create_meeting(
        MeetingInput(
            external_ref=MeetingExternalRef(ical_uid=ical_uid),
            title=title,
            description="ai-3hq verification meeting — safe to ignore/archive.",
            start=start,
            end=start + timedelta(hours=1),
            is_all_day=False,
            participants=[
                MeetingParticipantInput(
                    email_address=organizer_email,
                    is_organizer=True,
                ),
            ],
            linked_records=links,
        ),
    )
    if not envelope.success or not envelope.record_id:
        raise SystemExit(
            f"POST /v2/meetings failed for {ical_uid!r}: "
            f"{[e.model_dump() for e in envelope.errors]}",
        )
    return envelope.record_id


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------


def run_config_inspection(*, render: bool) -> tuple[ConfigVerdict, dict[str, Any]]:
    """Compute the config verdict and a JSON-serializable payload.

    Prints human-readable output only when ``render`` is True; the caller emits
    JSON itself (combining config + empirical into a single document) so that a
    ``--json`` run always produces exactly one valid JSON object.
    """
    people_inverse = _discover_inverse_attrs("people")
    company_inverse = _discover_inverse_attrs("companies")
    meeting_rel = _dump_meeting_relationship()
    verdict = evaluate_inverse_multiselect(people_inverse, company_inverse)

    payload: dict[str, Any] = {
        "mode": "config",
        "status": verdict.status,
        "reasons": verdict.reasons,
        "inverse_attrs": [
            {
                "target_object": a.target_object,
                "api_slug": a.api_slug,
                "is_multiselect": a.is_multiselect,
            }
            for a in verdict.inverse_attrs
        ],
        "meeting_relationship": meeting_rel,
    }

    if render:
        print("=== Config inspection (read-only) ===")
        print("people inverse attrs:", people_inverse or "(none found)")
        print("companies inverse attrs:", company_inverse or "(none found)")
        print("meeting record-reference attrs:", json.dumps(meeting_rel, indent=2))
        print(f"\nCONFIG VERDICT: {verdict.status.upper()}")
        for reason in verdict.reasons:
            print(f"  - {reason}")
    return verdict, payload


@dataclass
class EmpiricalResult:
    idempotent: bool
    no_contention: bool
    details: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.idempotent and self.no_contention


def run_empirical_check(
    *,
    person_email: str,
    company_domain: str,
    organizer_email: str,
    render: bool,
) -> tuple[EmpiricalResult, dict[str, Any]]:
    # The organizer/participant email MUST be distinct from the probed person:
    # Attio auto-links a meeting to the Person record matching a participant
    # email, so reusing person_email as the organizer would keep the person on
    # m1 via participant matching — masking a single-valued linked_records
    # inverse. A neutral organizer isolates the explicit linked_records path.
    if organizer_email == person_email:
        raise SystemExit(
            "--organizer-email must differ from --person-email so participant "
            "auto-linking does not confound the linked_records contention test.",
        )
    person_id, company_id = _resolve_links(person_email, company_domain)
    # Safety gate: never run against records that already carry meeting links —
    # a single-valued inverse would strip those real links on the first POST.
    # Covers the probed person/company AND the auto-linked organizer.
    _assert_no_existing_meeting_links(person_id, company_id)
    _assert_organizer_safe(organizer_email, person_id)
    person_key: tuple[str, str] = ("people", person_id)
    company_key: tuple[str, str] = ("companies", company_id)
    # The link set every meeting is expected to carry in the PASS case. Used to
    # let read-backs wait out Attio read-after-write lag (see _read_meeting_links).
    expected: set[tuple[str, str]] = {person_key, company_key}
    # A high-entropy suffix (uuid4) guarantees a FRESH meeting per run: the UID
    # keys Attio's find-or-create, so a same-second rerun or partial-run
    # recovery would otherwise reuse a prior meeting and read stale link state,
    # reporting an incorrect pass/fail. Microsecond precision keeps the stamp
    # human-readable; the uuid4 fragment provides the actual collision guard.
    stamp = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex[:8]}"
    )
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # Meeting 1: idempotency + contention-anchor. Links person + company.
    m1_uid = f"ai-3hq-verify-{stamp}-m1"
    m1_id = _post_test_meeting(
        ical_uid=m1_uid,
        title="[ai-3hq verify] m1 idempotency+anchor",
        person_id=person_id,
        company_id=company_id,
        organizer_email=organizer_email,
        start=start,
    )
    links_after_first = _read_meeting_links(m1_id, expect=expected)
    # Re-POST the identical meeting (same ical_uid + links) to prove idempotency.
    m1_id_repost = _post_test_meeting(
        ical_uid=m1_uid,
        title="[ai-3hq verify] m1 idempotency+anchor",
        person_id=person_id,
        company_id=company_id,
        organizer_email=organizer_email,
        start=start,
    )
    links_after_repost = _read_meeting_links(m1_id, expect=expected)

    keys_first = _linked_record_keys(links_after_first)
    keys_repost = _linked_record_keys(links_after_repost)
    # Idempotent requires the re-POST to CONVERGE to the same meeting (find, not
    # create): if Attio regressed to creating a duplicate meeting for the same
    # ical_uid, m1's links could look unchanged while a second meeting exists —
    # so assert the returned record_id matches before trusting the link compare.
    converged_to_same_meeting = m1_id_repost == m1_id
    # Idempotency is judged on the EXPLICIT links we sent (person + company), not
    # the full link set: the neutral organizer auto-links to its own Person
    # record, which can materialize between the two reads — a full-set equality
    # check would then flap even though the upsert behaved correctly. The real
    # property is "the explicit links are present on both reads, exactly once,
    # and the re-POST converged to the same meeting."
    idempotent = (
        converged_to_same_meeting
        and expected.issubset(keys_first)
        and expected.issubset(keys_repost)
        # Reject duplicate links on BOTH responses: a dup on the first POST that
        # the repost happened to normalize (or vice versa) would otherwise pass.
        and not _has_duplicate_links(links_after_first)
        and not _has_duplicate_links(links_after_repost)
    )

    # Meeting 2: link the SAME person AND company again. Then re-read m1 to
    # confirm neither was stripped off m1 (the real backfill-gating concern).
    # Both sides must be exercised: the people inverse and the companies inverse
    # are independent attributes, so a single-valued company inverse would strip
    # company links even if the people inverse is multi-valued.
    m2_uid = f"ai-3hq-verify-{stamp}-m2"
    m2_id = _post_test_meeting(
        ical_uid=m2_uid,
        title="[ai-3hq verify] m2 contention",
        person_id=person_id,
        company_id=company_id,
        organizer_email=organizer_email,
        start=start + timedelta(hours=2),
    )
    m1_links_after_m2 = _read_meeting_links(m1_id, expect=expected)
    m2_links = _read_meeting_links(m2_id, expect=expected)
    m1_keys_after = _linked_record_keys(m1_links_after_m2)
    m2_keys = _linked_record_keys(m2_links)
    person_in_m1 = person_key in m1_keys_after
    company_in_m1 = company_key in m1_keys_after
    person_in_m2 = person_key in m2_keys
    company_in_m2 = company_key in m2_keys
    no_contention = person_in_m1 and company_in_m1 and person_in_m2 and company_in_m2

    details = {
        "person_id": person_id,
        "company_id": company_id,
        "organizer_email": organizer_email,
        "m1_ical_uid": m1_uid,
        "m1_meeting_id": m1_id,
        "m1_meeting_id_repost": m1_id_repost,
        "repost_converged_to_same_meeting": converged_to_same_meeting,
        "m2_ical_uid": m2_uid,
        "m2_meeting_id": m2_id,
        "m1_links_after_first_post": sorted(keys_first),
        "m1_links_after_repost": sorted(keys_repost),
        "m1_links_after_m2_post": sorted(m1_keys_after),
        "m2_links": sorted(m2_keys),
        "person_still_in_m1_after_m2": person_in_m1,
        "company_still_in_m1_after_m2": company_in_m1,
        "person_in_m2": person_in_m2,
        "company_in_m2": company_in_m2,
    }
    result = EmpiricalResult(
        idempotent=idempotent,
        no_contention=no_contention,
        details=details,
    )
    payload = {
        "mode": "empirical",
        "idempotent": idempotent,
        "no_contention": no_contention,
        "passed": result.passed,
        "details": details,
    }

    if render:
        print("\n=== Empirical check (PROD writes) ===")
        print(json.dumps(details, indent=2, default=str))
        print(f"\nIDEMPOTENT (re-POST converged, no duplicate/append): {idempotent}")
        print(
            "NO CONTENTION (m2 stripped neither person nor company off m1): "
            f"{no_contention}",
        )
        print(f"\nEMPIRICAL VERDICT: {'PASS' if result.passed else 'STOP — escalate'}")
    return result, payload


# ---------------------------------------------------------------------------
# Infisical bootstrap + CLI
# ---------------------------------------------------------------------------


def _bootstrap_via_infisical(env: str, forward_args: list[str]) -> int:
    if os.environ.get(_BOOTSTRAP_SENTINEL_ENV):
        print(
            f"ATTIO_API_KEY is not present in the Infisical '{env}' environment.\n"
            "Verify the secret exists at that env, or pass --env to switch.",
            file=sys.stderr,
        )
        return 2

    creds = read_infisical_credentials()
    if creds is None:
        print(
            "ATTIO_API_KEY is not set and INFISICAL_PROJECT_ID/INFISICAL_TOKEN\n"
            f"were not found in the environment or {REPO_ROOT / '.env.local'}.\n"
            "Run via:\n"
            f"  {infisical_run_example('scripts/attio-inspect-meeting-relationship.py')}",
            file=sys.stderr,
        )
        return 2

    project_id, token = creds
    argv = [
        "infisical",
        "run",
        "--projectId",
        project_id,
        "--token",
        token,
        f"--env={env}",
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        *forward_args,
    ]
    os.environ[_BOOTSTRAP_SENTINEL_ENV] = "1"
    # trunk-ignore(bandit/B606): argv is built from local config + the script's own path
    os.execvp(argv[0], argv)  # noqa: S606


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help=(
            "Infisical environment to read ATTIO_API_KEY from. Required unless "
            "INFISICAL_ENV is set. Meetings are ALPHA and prod-only; dev 404s."
        ),
    )
    parser.add_argument(
        "--idempotency-check",
        action="store_true",
        help="Run the empirical PROD writes (idempotency + no-contention).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required to actually POST test meetings to PROD.",
    )
    parser.add_argument(
        "--person-email",
        default="elvis@dlthub.com",
        help="Email of a known prod Person to link in the test meetings.",
    )
    parser.add_argument(
        "--company-domain",
        default="dlthub.com",
        help="Domain of a known prod Company to link in the test meetings.",
    )
    parser.add_argument(
        "--organizer-email",
        default="ai-3hq-verify-organizer@dlthub.com",
        help=(
            "Meeting organizer/participant email. MUST differ from "
            "--person-email so participant auto-linking does not confound the "
            "linked_records contention test. Defaults to a throwaway address."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )
    args = parser.parse_args()

    # --execute only has meaning for the empirical writes. Reject it on its own
    # so an operator who typed `--execute` (expecting a prod run) is not silently
    # dropped onto the read-only config path and misled into thinking the
    # empirical probe ran.
    if args.execute and not args.idempotency_check:
        parser.error(
            "--execute requires --idempotency-check (it only gates the "
            "empirical PROD writes; the config inspection is read-only).",
        )

    api_key = clean_env(os.environ.get("ATTIO_API_KEY"))
    if not api_key:
        env = args.env or clean_env(os.environ.get("INFISICAL_ENV"))
        if env not in {"dev", "prod"}:
            print(
                "Infisical environment is required to bootstrap ATTIO_API_KEY. "
                "Pass --env=dev|prod or set INFISICAL_ENV. (Refusing to default "
                "to prod silently.)",
                file=sys.stderr,
            )
            return 2
        forward = [f"--env={env}"]
        if args.idempotency_check:
            forward.append("--idempotency-check")
        if args.execute:
            forward.append("--execute")
        forward += ["--person-email", args.person_email]
        forward += ["--company-domain", args.company_domain]
        forward += ["--organizer-email", args.organizer_email]
        if args.json:
            forward.append("--json")
        return _bootstrap_via_infisical(env, forward)

    # In --json mode, suppress per-function human prints and emit ONE combined
    # JSON object at the end, so the output is always a single valid document.
    render = not args.json
    config_verdict, config_payload = run_config_inspection(render=render)

    if not args.idempotency_check:
        if args.json:
            print(json.dumps(config_payload, indent=2, default=str))
        # Config-only run: STOP is a hard fail; INCONCLUSIVE exits non-zero to
        # signal the operator should run the empirical gate.
        return 0 if config_verdict.status == "pass" else 1

    if not args.execute:
        print(
            "\n--idempotency-check writes throwaway meetings to PROD (no DELETE "
            "on /v2/meetings). Re-run with --execute to proceed.",
            file=sys.stderr,
        )
        return 2

    result, empirical_payload = run_empirical_check(
        person_email=args.person_email,
        company_domain=args.company_domain,
        organizer_email=args.organizer_email,
        render=render,
    )
    if args.json:
        print(
            json.dumps(
                {"config": config_payload, "empirical": empirical_payload},
                indent=2,
                default=str,
            ),
        )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
