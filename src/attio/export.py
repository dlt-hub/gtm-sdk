"""Source-agnostic Attio operation dispatcher.

Source webhooks return ``list[AttioOp]`` via their ``attio_get_operations``
method; this module turns that into Attio SDK calls. The dispatcher imports
only from ``libs.attio.*`` and ``src.attio.ops`` — adding a new source webhook
should require no change here.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from libs.attio.companies import (
    get_company_values,
    upsert_company as libs_upsert_company,
)
from libs.attio.contracts import ErrorEntry, ReliabilityEnvelope, WarningEntry
from libs.attio.errors import SchemaMismatchError
from libs.attio.meetings import find_or_create_meeting
from libs.attio.mentions import upsert_mention as libs_upsert_mention
from libs.attio.models import (
    CompanyInput,
)
from libs.attio.models import (
    MeetingExternalRef as LibMeetingExternalRef,
)
from libs.attio.models import (
    MeetingInput,
    MeetingLifecycleEventInput,
    MeetingLinkedRecord,
    MeetingParticipantInput,
    MentionInput,
    NoteInput,
    PersonInput,
    TrackingEventInput,
)
from libs.attio.notes import (
    add_note as libs_add_note,
)
from libs.attio.notes import (
    find_note_by_title as libs_find_note_by_title,
)
from libs.attio.notes import (
    resolve_record_id_for_ref as libs_resolve_record_id_for_ref,
)
from libs.attio.people import (
    error_envelope,
    get_person_values,
    upsert_person as libs_upsert_person,
)
from libs.attio.preflight import resolve_owner_member_id
from libs.attio.tracking_events import (
    find_or_create_meeting_lifecycle_event,
    find_or_create_tracking_event,
)
from libs.logging.structured import log
from src.attio.ops import (
    AttioOp,
    CompanyRef,
    EmitMeetingLifecycleEvent,
    MeetingRef,
    PersonRef,
    UpsertCompany,
    UpsertMeeting,
    UpsertMention,
    UpsertNote,
    UpsertPerson,
    UpsertTrackingEvent,
)

# ---------- LookupTable ----------


@dataclass
class LookupTable:
    """In-plan registry mapping (kind, attribute, value) -> Attio record_id.

    Handlers consult the table to resolve ``Ref`` values (e.g. ``UpsertNote.parent``)
    against earlier ops in the same plan. Pass 3 wires ``_handle_upsert_note`` to
    use this.
    """

    _store: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def record(self, op: AttioOp, record_id: str | None) -> None:
        if record_id is None:
            return
        keys = self._key_for_op(op)
        if keys is not None:
            for key in keys:
                self._store[key] = record_id

    def resolve(self, ref: PersonRef | CompanyRef | MeetingRef) -> str | None:
        return self._store.get(self._key_for_ref(ref))

    @staticmethod
    def _key_for_op(op: AttioOp) -> list[tuple[str, str, str]] | None:
        if isinstance(op, UpsertPerson):
            value = getattr(op, op.matching_attribute)
            if value is None:
                return None
            return [("person", op.matching_attribute, value)]
        if isinstance(op, UpsertCompany):
            return [("company", "domain", op.domain)]
        if isinstance(op, UpsertMeeting):
            return [("meeting", "ical_uid", op.external_ref.ical_uid)]
        return None

    @staticmethod
    def _key_for_ref(ref: PersonRef | CompanyRef | MeetingRef) -> tuple[str, str, str]:
        if isinstance(ref, PersonRef):
            return ("person", ref.attribute, ref.value)
        if isinstance(ref, CompanyRef):
            return ("company", "domain", ref.domain)
        return ("meeting", "ical_uid", ref.ical_uid)


# ---------- Outcomes ----------


@dataclass
class OpOutcome:
    op_index: int
    op_type: str
    success: bool
    record_id: str | None
    envelope: ReliabilityEnvelope
    # True when this op was marked ``optional`` and failed: recorded for
    # visibility but excluded from the overall execution ``success`` bit and
    # not allowed to abort the plan. See ai-0ex.
    optional: bool = False


@dataclass
class ExecutionResult:
    success: bool
    outcomes: list[OpOutcome]
    fail_index: int | None = None
    fail_reason: str | None = None

    def body(self) -> str:
        import orjson

        payload: dict[str, Any] = {
            "success": self.success,
            "outcomes": [
                {
                    "op_index": o.op_index,
                    "op_type": o.op_type,
                    "success": o.success,
                    "record_id": o.record_id,
                    # The keys below are emitted only when they carry signal, so
                    # the common clean-success body is unchanged for existing
                    # consumers (ai-0ex):
                    #  - `optional`: a failed best-effort op (didn't abort).
                    #  - `partial_success` + `warnings`: e.g. a mention written
                    #    WITHOUT its person link — would otherwise look like a
                    #    plain success and hide the degradation.
                    #  - `errors`: failure detail.
                    **({"optional": True} if o.optional else {}),
                    **({"partial_success": True} if o.envelope.partial_success else {}),
                    **(
                        {"warnings": [w.model_dump() for w in o.envelope.warnings]}
                        if o.envelope.warnings
                        else {}
                    ),
                    **(
                        {"errors": [e.model_dump() for e in o.envelope.errors]}
                        if not o.success
                        else {}
                    ),
                }
                for o in self.outcomes
            ],
        }
        if not self.success:
            payload["fail_index"] = self.fail_index
            payload["fail_reason"] = self.fail_reason
        return orjson.dumps(payload).decode()


# ---------- Handlers ----------


def _has_populated_value(existing: dict[str, Any], key: str) -> bool:
    """Check if a field in the existing values dict has a non-empty value."""
    value = existing.get(key)
    if not value:
        return False
    # value is typically a list of dicts from Attio
    if isinstance(value, list):
        return any(_is_value_populated(v) for v in value)
    return True


def _is_value_populated(v: Any) -> bool:
    """Check if an individual value object is populated."""
    if not v:
        return False
    if isinstance(v, dict):
        # Check if any meaningful field exists
        return any(
            v.get(k)
            for k in ["value", "option", "email_address", "full_name", "locality"]
        )
    return bool(str(v).strip())


def _handle_upsert_person(
    op: UpsertPerson,
    table: LookupTable,  # noqa: ARG001 — kept for handler signature parity
) -> ReliabilityEnvelope:
    title = op.title
    city = op.city
    state = op.state
    zipcode = op.zipcode

    if op.merge_only_if_empty:
        existing = get_person_values(
            matching_attribute=op.matching_attribute,
            email=op.email,
            linkedin=op.linkedin,
            github_handle=op.github_handle,
        )
        if existing is not None:
            # For each field in merge_only_if_empty, check if existing has a non-empty value
            if "title" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "title",
            ):
                title = None
            if "city" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "primary_location",
            ):
                city = None
            if "state" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "primary_location",
            ):
                state = None
            if "zipcode" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "primary_location",
            ):
                zipcode = None

    try:
        return libs_upsert_person(
            PersonInput(
                email=op.email,
                first_name=op.first_name,
                last_name=op.last_name,
                linkedin=op.linkedin,
                github_handle=op.github_handle,
                github_url=op.github_url,
                phone=op.phone,
                company_domain=op.company_domain,
                title=title,
                city=city,
                state=state,
                zipcode=zipcode,
                additional_emails=[],
                replace_emails=False,
            ),
            matching_attribute=op.matching_attribute,
        )
    except SchemaMismatchError as exc:
        # A matching attribute that the people object doesn't define (e.g. the
        # `github` slug if it were archived/absent) surfaces from the lib layer
        # as a typed SchemaMismatchError. Classify it as a normal
        # `schema_mismatch` failed envelope instead of letting the dispatcher's
        # catch-all tag it `handler_exception` (ai-0ex). When this op is
        # `optional`, execute() keeps going and the mention still lands.
        return error_envelope(exc)


def _handle_upsert_company(
    op: UpsertCompany,
    table: LookupTable,  # noqa: ARG001 — kept for handler signature parity
) -> ReliabilityEnvelope:
    industry = op.industry
    employee_count = op.employee_count
    estimate_revenue = op.estimate_revenue
    linkedin_url = op.linkedin_url

    if op.merge_only_if_empty:
        existing = get_company_values(op.domain)
        if existing is not None:
            if "industry" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "industry",
            ):
                industry = None
            if "employee_count" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "employee_count",
            ):
                employee_count = None
            if "estimate_revenue" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "estimate_revenue",
            ):
                estimate_revenue = None
            if "linkedin_url" in op.merge_only_if_empty and _has_populated_value(
                existing,
                "linkedin",
            ):
                linkedin_url = None

    return libs_upsert_company(
        CompanyInput(
            name=op.name or op.domain,
            domain=op.domain,
            industry=industry,
            employee_count=employee_count,
            estimate_revenue=estimate_revenue,
            linkedin_url=linkedin_url,
        ),
    )


def _meeting_input(
    op: UpsertMeeting,
    linked_records: list[MeetingLinkedRecord],
) -> MeetingInput:
    return MeetingInput(
        external_ref=LibMeetingExternalRef(
            ical_uid=op.external_ref.ical_uid,
            provider=op.external_ref.provider,
            is_recurring=op.external_ref.is_recurring,
            original_start_time=op.external_ref.original_start_time,
        ),
        title=op.title,
        description=op.description,
        start=op.start,
        end=op.end,
        is_all_day=op.is_all_day,
        participants=[
            MeetingParticipantInput(
                email_address=p.email_address,
                is_organizer=p.is_organizer,
                status=p.status,
            )
            for p in op.participants
        ],
        linked_records=linked_records,
    )


def _handle_upsert_meeting(
    op: UpsertMeeting,
    table: LookupTable,
) -> ReliabilityEnvelope:
    """Create the meeting and link it to the people/companies it involves (ai-ch3).

    Resolution runs in two phases around the write because ``/v2/meetings``
    auto-creates a Person for each ``participant`` — a record that does not yet
    exist when we first resolve, so a single pre-write pass would permanently
    drop every first-time attendee (the gap roborev flagged).

    Phase 1 links the records that already exist (``attempts=1`` — no read-after-
    write race for pre-existing records) and POSTs the meeting, which creates the
    participant Persons. Phase 2 re-resolves the refs that missed with
    ``attempts=3`` and a repeat POST re-syncs the newly found records onto the
    meeting. ``/v2/meetings`` is a sync endpoint — repeat POSTs sharing an
    ``ical_uid`` converge the same record's ``linked_records`` — so the second
    write attaches them rather than duplicating the meeting. The retry serves a
    participant Person (just auto-created — guaranteed to resolve) and a Company
    alike (a transient lookup miss, or one created by a concurrent pipeline).

    Companies are still link-if-exists, never create: Attio does not create them
    from participants the way it does Persons, and manufacturing a company per
    raw email domain (e.g. ``gmail.com``) would pollute the CRM. A domain that is
    genuinely not in Attio stays unresolved after the retry and is surfaced in
    the ``unresolved_meeting_links`` warning rather than linked or invented.

    Person identities other than ``email`` (linkedin/github_handle) resolve only
    via the plan's LookupTable — the live lookup is email-only — so an unresolved
    one is dropped immediately (not retriable) and likewise surfaced.
    """
    # (parent_object, record_id) -> link, deduped across both phases.
    links: dict[tuple[str, str], MeetingLinkedRecord] = {}
    retriable: list[PersonRef | CompanyRef] = []
    unresolved_persons = 0
    unresolved_companies = 0
    for ref in op.linked_records:
        parent_object = _REF_KIND_TO_PARENT_OBJECT[ref.ref_kind]
        record_id = _resolve_ref_record_id(ref, table, attempts=1)
        if record_id is not None:
            links[(parent_object, record_id)] = MeetingLinkedRecord(
                object=parent_object,
                record_id=record_id,
            )
        elif isinstance(ref, CompanyRef) or ref.attribute == "email":
            # Both resolve via the email/domain live lookup, so both can be
            # retried after the write (read-after-write for the auto-created
            # participant Person; transient/concurrent miss for the company).
            retriable.append(ref)
        else:
            # Non-email PersonRef: the live lookup can't resolve it, so a table
            # miss is terminal.
            unresolved_persons += 1

    envelope = find_or_create_meeting(_meeting_input(op, list(links.values())))
    if not envelope.success:
        return envelope

    # Phase 2: re-resolve the refs that missed before the write and re-sync them.
    newly_linked: list[PersonRef | CompanyRef] = []
    for ref in retriable:
        parent_object = _REF_KIND_TO_PARENT_OBJECT[ref.ref_kind]
        record_id = _resolve_ref_record_id(ref, table, attempts=3)
        if record_id is None:
            if isinstance(ref, CompanyRef):
                unresolved_companies += 1
            else:
                unresolved_persons += 1
            continue
        key = (parent_object, record_id)
        if key not in links:
            links[key] = MeetingLinkedRecord(
                object=parent_object,
                record_id=record_id,
            )
            newly_linked.append(ref)

    if newly_linked:
        relink = find_or_create_meeting(_meeting_input(op, list(links.values())))
        if not relink.success:
            # The meeting exists (phase 1 succeeded with its links attached);
            # only the re-sync that attaches the newly found records failed. Count
            # exactly those as unresolved so the warning below is accurate, and
            # degrade to a partial success rather than aborting the plan's
            # downstream notes.
            unresolved_companies += sum(
                1 for ref in newly_linked if isinstance(ref, CompanyRef)
            )
            unresolved_persons += sum(
                1 for ref in newly_linked if isinstance(ref, PersonRef)
            )

    # Make dropped associations observable (ai-ch3): a meeting that links some
    # but not all of its people/companies must NOT read as a clean success, or
    # the hydration silently no-ops when Attio lookups lag or a company is not
    # yet in the CRM. Mirrors the mention handler — the primary record wrote, but
    # enrichment links did not fully attach -> partial_success.
    if unresolved_persons or unresolved_companies:
        envelope.partial_success = True
        envelope.warnings.append(
            WarningEntry(
                code="unresolved_meeting_links",
                message=(
                    f"meeting linked, but {unresolved_persons} participant(s) and "
                    f"{unresolved_companies} external compan(y/ies) could not be "
                    "linked (record not in Attio, or non-email person ref); these "
                    "link on a later touch"
                ),
                field="linked_records",
                retryable=True,
            ),
        )

    return envelope


# Notes hang off a *standard object* record. Meetings are deliberately absent:
# Attio's Notes API rejects ``parent_object="meetings"`` (a meeting is not an
# object), so a meeting can only be *associated* via ``meeting_id`` — never be a
# parent. See ai-gez and the Ref union in ``src/attio/ops.py``.
_REF_KIND_TO_PARENT_OBJECT: dict[str, str] = {
    "person": "people",
    "company": "companies",
}


def _unresolved_ref_envelope(label: str, detail: str) -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=False,
        partial_success=False,
        action="failed",
        record_id=None,
        errors=[
            ErrorEntry(
                code="unresolved_ref",
                message=f"could not resolve {label}:{detail}",
                error_type="UnresolvedRefError",
                fatal=True,
            ),
        ],
        warnings=[],
        skipped_fields=[],
        meta={"output_schema_version": "v1"},
    )


def _resolve_ref_record_id(
    ref: PersonRef | CompanyRef,
    table: LookupTable,
    *,
    attempts: int = 3,
) -> str | None:
    """Resolve a person/company Ref to a record_id, falling back to a live query.

    Prefer the plan's LookupTable (a prior ``UpsertPerson``/``UpsertCompany``
    in the same plan). When the ref was not created explicitly in this plan
    resolve it by email/domain. ``PersonRef`` identities other than ``email``
    (linkedin/github_handle) cannot be resolved this way and return ``None``.

    ``attempts`` tunes the live-query retry. Notes default to ``3`` (read-after-
    write: the participant Person was just auto-created by the meeting POST, and
    Attio's record search can lag). The meeting ``linked_records`` path passes
    ``1``: there a miss is the expected normal case (the record genuinely does
    not exist yet — link-only), so paying retry backoff per unknown attendee
    would dominate the backfill (ai-ch3).
    """
    resolved = table.resolve(ref)
    if resolved is not None:
        return resolved
    if isinstance(ref, PersonRef) and ref.attribute == "email":
        return libs_resolve_record_id_for_ref(
            parent_object="people",
            email=ref.value,
            attempts=attempts,
        )
    if isinstance(ref, CompanyRef):
        return libs_resolve_record_id_for_ref(
            parent_object="companies",
            domain=ref.domain,
            attempts=attempts,
        )
    return None


def _handle_upsert_note(
    op: UpsertNote,
    table: LookupTable,
) -> ReliabilityEnvelope:
    parent_object = _REF_KIND_TO_PARENT_OBJECT[op.parent.ref_kind]

    parent_record_id = _resolve_ref_record_id(op.parent, table)
    if parent_record_id is None:
        return _unresolved_ref_envelope(op.parent.ref_kind, str(op.parent.model_dump()))

    # Resolve the optional Meeting association. The ``UpsertMeeting`` runs
    # earlier in the same plan, so its record_id is in the table; a set-but-
    # unresolved meeting is a plan-ordering bug, not a recoverable miss.
    meeting_record_id: str | None = None
    if op.meeting is not None:
        meeting_record_id = table.resolve(op.meeting)
        if meeting_record_id is None:
            return _unresolved_ref_envelope("meeting", str(op.meeting.model_dump()))

    # Idempotency: Attio has no native upsert for notes and no natural key, so
    # skip creation when a matching note already exists on the parent. Titles
    # emitted by webhook producers are deterministic per note kind (e.g.
    # "Fathom summary — Sales Discovery", "Action items"). Because the parent is
    # now a person/company shared across many meetings, title alone is not
    # unique — scope the replay check to the associated meeting via meeting_id.
    #
    # Rollout safety (ai-gez): this new (title, meeting_id) key cannot duplicate
    # any *pre-existing* notes. Before this change the only producer of these
    # titles was the Fathom transform, which parented notes to "meetings" — a
    # request Attio rejects with HTTP 400, so those notes never persisted. There
    # is therefore no legacy person-parented Fathom note (with meeting_id unset)
    # for the scoped match to miss. Notes the new code writes always carry
    # meeting_id (verified against prod), and non-meeting callers pass
    # op.meeting is None and keep the original title-only dedup below.
    # Scan via ``find_note_by_title`` rather than materializing the whole list:
    # it streams the paginated history and stops at the first match, so an
    # already-written note on an early page is found without forcing a full-
    # history read. (A full read would let the paginator's fail-closed cap turn
    # an oversized parent into a hard export abort even when the match is early.)
    # ``meeting_record_id`` is None exactly when ``op.meeting`` is None, so this
    # preserves the title-only dedup for non-meeting notes.
    existing_note_id = libs_find_note_by_title(
        parent_object=parent_object,
        parent_record_id=parent_record_id,
        title=op.title,
        meeting_id=meeting_record_id,
    )
    if existing_note_id is not None:
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="noop",
            record_id=existing_note_id,
            errors=[],
            warnings=[],
            skipped_fields=[],
            meta={"output_schema_version": "v1"},
        )

    result = libs_add_note(
        NoteInput(
            title=op.title,
            content=op.content,
            parent_object=parent_object,
            parent_record_id=parent_record_id,
            meeting_id=meeting_record_id,
            # Webhook producers (Fathom) emit markdown summaries / checklists;
            # render them as rich text rather than escaped plaintext.
            format="markdown",
        ),
    )
    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action="created",
        record_id=result.note_id,
        errors=[],
        warnings=[],
        skipped_fields=[],
        meta={"output_schema_version": "v1"},
    )


def _handle_upsert_mention(
    op: UpsertMention,
    table: LookupTable,
) -> ReliabilityEnvelope:
    related_person_record_id = None
    degrade_warnings: list[WarningEntry] = []
    if op.related_person:
        related_person_record_id = table.resolve(op.related_person)
        if related_person_record_id is None:
            if not op.related_person_optional:
                # Default: an unresolved ref is a hard data-integrity error.
                # Keeping this loud means a genuine missing-reference bug in
                # some other plan fails fast instead of silently persisting an
                # unlinked mention.
                return ReliabilityEnvelope(
                    success=False,
                    partial_success=False,
                    action="failed",
                    record_id=None,
                    errors=[
                        ErrorEntry(
                            code="unresolved_ref",
                            message=(
                                f"could not resolve {op.related_person.ref_kind}:"
                                f"{op.related_person.model_dump()}"
                            ),
                            error_type="UnresolvedRefError",
                            fatal=True,
                        ),
                    ],
                    warnings=[],
                    skipped_fields=[],
                    meta={"output_schema_version": "v1"},
                )
            # Opted-in best-effort link (octolens): the mention is the primary
            # record and the person is enrichment, so when the related person
            # could not be resolved (e.g. its optional UpsertPerson failed
            # because the people object lacks the `github` attribute) write
            # the mention WITHOUT the link rather than dropping it — silent data
            # loss is the bug this fixes (ai-0ex).
            degrade_warnings.append(
                WarningEntry(
                    code="related_person_unresolved",
                    message=(
                        f"related person {op.related_person.ref_kind} did not "
                        f"resolve ({op.related_person.model_dump()}); writing "
                        "the mention without a person link."
                    ),
                    field="related_person",
                    retryable=False,
                ),
            )

    envelope = libs_upsert_mention(
        MentionInput(
            mention_url=op.mention_url,
            last_action=op.last_action,
            source_platform=op.source_platform,
            source_id=op.source_id,
            mention_title=op.mention_title,
            mention_body=op.mention_body,
            mention_timestamp=op.mention_timestamp,
            author_handle=op.author_handle,
            author_profile_url=op.author_profile_url,
            author_avatar_url=op.author_avatar_url,
            relevance_score=op.relevance_score,
            relevance_comment=op.relevance_comment,
            primary_keyword=op.primary_keyword,
            keywords=op.keywords,
            octolens_tags=op.octolens_tags,
            sentiment=op.sentiment,
            language=op.language,
            subreddit=op.subreddit,
            view_id=op.view_id,
            view_name=op.view_name,
            bookmarked=op.bookmarked,
            image_url=op.image_url,
            related_person_record_id=related_person_record_id,
        ),
    )
    if degrade_warnings:
        # Record the degradation context regardless of outcome, but only call
        # it a *partial* success when the mention actually wrote — a failed
        # mention write must keep its failed envelope, not look partial (ai-0ex).
        envelope.warnings.extend(degrade_warnings)
        if envelope.success:
            envelope.partial_success = True
    return envelope


def _handle_upsert_tracking_event(
    op: UpsertTrackingEvent,
    table: LookupTable,
) -> ReliabilityEnvelope:
    person_id: str | None = None
    company_id: str | None = None
    if op.subject_person is not None:
        person_id = table.resolve(op.subject_person)
        if person_id is None:
            return ReliabilityEnvelope(
                success=False,
                partial_success=False,
                action="failed",
                record_id=None,
                errors=[
                    ErrorEntry(
                        code="unresolved_ref",
                        message=(
                            f"could not resolve {op.subject_person.ref_kind}:"
                            f"{op.subject_person.model_dump()}"
                        ),
                        error_type="UnresolvedRefError",
                        fatal=True,
                    ),
                ],
                warnings=[],
                skipped_fields=[],
                meta={"output_schema_version": "v1"},
            )
    if op.subject_company is not None:
        company_id = table.resolve(op.subject_company)
        if company_id is None:
            return ReliabilityEnvelope(
                success=False,
                partial_success=False,
                action="failed",
                record_id=None,
                errors=[
                    ErrorEntry(
                        code="unresolved_ref",
                        message=(
                            f"could not resolve {op.subject_company.ref_kind}:"
                            f"{op.subject_company.model_dump()}"
                        ),
                        error_type="UnresolvedRefError",
                        fatal=True,
                    ),
                ],
                warnings=[],
                skipped_fields=[],
                meta={"output_schema_version": "v1"},
            )

    return find_or_create_tracking_event(
        TrackingEventInput(
            external_id=op.external_id,
            source=op.source,
            name=op.name,
            event_type=op.event_type,
            event_subtype=op.event_subtype,
            event_timestamp=op.event_timestamp,
            body_json=op.body_json,
            captured_url=op.captured_url,
            referrer=op.referrer,
            is_repeat_visit=op.is_repeat_visit,
            tags=op.tags,
            location=op.location,
            related_person_record_id=person_id,
            related_company_record_id=company_id,
        ),
    )


def _handle_emit_meeting_lifecycle_event(
    op: EmitMeetingLifecycleEvent,
    table: LookupTable,
) -> ReliabilityEnvelope:
    """Resolve the host Person record_id via the plan's LookupTable and write.

    The Cal.com dispatcher always emits ``UpsertCompany`` + ``UpsertPerson``
    for the host BEFORE this op in the same plan, so the table is populated
    by the time this handler runs. A missing host PersonRef is a programming
    error (the dispatcher should not emit this op without it).
    """
    host_record_id = table.resolve(op.host)
    if host_record_id is None:
        return ReliabilityEnvelope(
            success=False,
            partial_success=False,
            action="failed",
            record_id=None,
            errors=[
                ErrorEntry(
                    code="unresolved_ref",
                    message=(
                        "EmitMeetingLifecycleEvent.host did not resolve via "
                        f"LookupTable: {op.host.model_dump()}. The Cal.com "
                        "dispatcher must emit UpsertPerson for the host first."
                    ),
                    error_type="UnresolvedRefError",
                    fatal=True,
                ),
            ],
            warnings=[],
            skipped_fields=[],
            meta={"output_schema_version": "v1"},
        )
    return find_or_create_meeting_lifecycle_event(
        MeetingLifecycleEventInput(
            external_id=op.external_id,
            meeting_title=op.meeting_title,
            company_domain=op.company_domain,
            event_subtype=op.event_subtype,
            timestamp=op.timestamp,
            body_json=op.body_json,
            details_line=op.details_line,
            host_person_record_id=host_record_id,
            # Resolve the owner actor from the active token's workspace, so the
            # row is owned correctly in whichever workspace (dev/prod) the token
            # targets. Cached per token; never hardcoded (ai-ica).
            owner_member_id=resolve_owner_member_id(),
        ),
    )


OP_HANDLERS: dict[type, Callable[[Any, LookupTable], ReliabilityEnvelope]] = {
    UpsertPerson: _handle_upsert_person,
    UpsertCompany: _handle_upsert_company,
    UpsertMeeting: _handle_upsert_meeting,
    UpsertNote: _handle_upsert_note,
    UpsertMention: _handle_upsert_mention,
    UpsertTrackingEvent: _handle_upsert_tracking_event,
    EmitMeetingLifecycleEvent: _handle_emit_meeting_lifecycle_event,
}


# ---------- Dispatcher ----------


def _exception_envelope(error: Exception) -> ReliabilityEnvelope:
    """Wrap an uncaught handler exception as a failed envelope.

    Matches the shape Attio Modal wrappers produce via ``error_envelope`` so
    webhook callers always see a dispatcher result body, not a 500.
    """
    return ReliabilityEnvelope(
        success=False,
        partial_success=False,
        action="failed",
        record_id=None,
        errors=[
            ErrorEntry(
                code="handler_exception",
                message=f"{type(error).__name__}: {error}",
                error_type=type(error).__name__,
                fatal=True,
            ),
        ],
        warnings=[],
        skipped_fields=[],
        meta={"output_schema_version": "v1"},
    )


def execute(plan: Iterable[AttioOp]) -> ExecutionResult:
    """Execute a plan op-by-op. Fail-fast on the first failing *required* op.

    Ops marked ``optional`` (see ``UpsertPerson.optional``) are best-effort: a
    failure is recorded as an outcome but does not abort the plan and does not
    flip the overall ``success`` bit. Their record_id is never written to the
    LookupTable, so any downstream ref to them resolves to ``None`` and the
    referring handler degrades gracefully (ai-0ex).
    """
    table = LookupTable()
    outcomes: list[OpOutcome] = []
    for i, op in enumerate(plan):
        handler = OP_HANDLERS.get(type(op))
        if handler is None:
            return ExecutionResult(
                success=False,
                outcomes=outcomes,
                fail_index=i,
                fail_reason=f"unknown_op: {type(op).__name__}",
            )
        try:
            envelope = handler(op, table)
        except Exception as exc:  # noqa: BLE001 — turn any handler crash into a failed outcome
            log(
                "attio.handler_exception",
                op_index=i,
                op_type=type(op).__name__,
                error_type=type(exc).__name__,
                error_msg=str(exc),
                traceback=traceback.format_exc(),
            )
            envelope = _exception_envelope(exc)
        is_optional = bool(getattr(op, "optional", False))
        outcomes.append(
            OpOutcome(
                op_index=i,
                op_type=type(op).__name__,
                success=envelope.success,
                record_id=envelope.record_id,
                envelope=envelope,
                optional=is_optional and not envelope.success,
            ),
        )
        if not envelope.success:
            if is_optional:
                # Best-effort op failed: log, keep going, leave it out of the
                # LookupTable so downstream refs degrade rather than link.
                log(
                    "attio.optional_op_failed",
                    op_index=i,
                    op_type=type(op).__name__,
                    error_codes=[e.code for e in envelope.errors],
                )
                continue
            return ExecutionResult(
                success=False,
                outcomes=outcomes,
                fail_index=i,
                fail_reason="op_failed",
            )
        table.record(op, envelope.record_id)
    return ExecutionResult(success=True, outcomes=outcomes)
