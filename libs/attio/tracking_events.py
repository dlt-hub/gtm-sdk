from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from libs.attio.attributes import ensure_select_options
from libs.attio.client import get_client
from libs.attio.contracts import ErrorEntry, ReliabilityEnvelope
from libs.attio.errors import classify_error
from libs.attio.models import MeetingLifecycleEventInput, TrackingEventInput
from libs.attio.sdk_boundary import (
    build_patch_record_request,
    build_post_record_request,
)
from libs.attio.values import build_tracking_event_values

_OBJECT = "tracking_events"
_MULTISELECT_FIELDS: tuple[str, ...] = ("tags",)

# Workspace-member UUID for Elvis. Hardcoded as the ``owner`` actor on every
# cal.com meeting-lifecycle row. Confirmed by the user 2026-05-25; the prod
# people-records audit returned a different UUID (587ae272-...) which belongs
# to some other actor — do NOT use that one. See spec at
# design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md.
_LIFECYCLE_OWNER_ACTOR_UUID = "663f9ad9-6704-5aff-be6d-48edb58bd12c"

# The single ``event_type`` value carrying every meeting-lifecycle row.
# Differentiates from ``rb2b_visit`` / ``form_submission`` etc. on the same
# object. Kept as a module constant so the helper, dispatcher, and bootstrap
# stay in lockstep.
_LIFECYCLE_EVENT_TYPE = "calcom_meeting"


def find_or_create_tracking_event(input: TrackingEventInput) -> ReliabilityEnvelope:
    """Idempotently upsert a tracking_events row keyed by `external_id`.

    external_id is not unique in the live schema (see AI-277), so this uses
    query-then-create-or-patch instead of the SDK's native assert path.
    """
    try:
        _ensure_option_vocabulary(input)
        with get_client() as client:
            query_response = client.records.post_v2_objects_object_records_query(
                object=_OBJECT,
                filter_={"external_id": input.external_id},
            )
            existing = list(query_response.data or [])
            values = build_tracking_event_values(input)

            if existing:
                record_id = existing[0].id.record_id
                client.records.patch_v2_objects_object_records_record_id_(
                    object=_OBJECT,
                    record_id=record_id,
                    data=build_patch_record_request(values),
                )
                action = "updated"
            else:
                create_response = client.records.post_v2_objects_object_records(
                    object=_OBJECT,
                    data=build_post_record_request(values),
                )
                record_id = create_response.data.id.record_id
                action = "created"
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(exc)

    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action=action,  # type: ignore[arg-type]
        record_id=record_id,
        warnings=[],
        skipped_fields=[],
        errors=[],
        meta={
            "output_schema_version": "v1",
            "tracking_event": input.model_dump(mode="json"),
        },
    )


def _ensure_option_vocabulary(input: TrackingEventInput) -> None:
    """Seed select option titles the payload references just-in-time.

    Bootstrap is the primary path for adding options; this defense-in-depth
    layer means a new ``source``, ``event_type``, ``event_subtype``, or
    ``tags`` value never silently fails Attio's "option does not exist"
    validation. Idempotent: ``ensure_select_options`` is a no-op when the
    option already exists. See ai-ztm.
    """
    ensure_select_options(
        target_object=_OBJECT,
        attribute_slug="event_type",
        options=[input.event_type],
    )
    if input.event_subtype is not None:
        ensure_select_options(
            target_object=_OBJECT,
            attribute_slug="event_subtype",
            options=[input.event_subtype],
        )
    ensure_select_options(
        target_object=_OBJECT,
        attribute_slug="source",
        options=[input.source],
    )
    if input.tags:
        ensure_select_options(
            target_object=_OBJECT,
            attribute_slug="tags",
            options=list(input.tags),
        )


def find_or_create_meeting_lifecycle_event(
    input: MeetingLifecycleEventInput,
) -> ReliabilityEnvelope:
    """Upsert the single ``tracking_events`` row representing one cal.com meeting.

    One row per meeting (keyed by ``external_id``). Every cal.com webhook for
    that meeting PATCHes this row, advancing ``event_subtype`` and appending a
    line to the cumulative ``details`` text. Plan-02's per-(meeting × attendee)
    audit-log model is gone — see the spec for the rationale.

    Writes the slugs confirmed present on prod (and bootstrapped onto dev):

    - ``external_id`` — meeting's ical_uid (idempotency key)
    - ``name`` — ``"<event_subtype> <meeting_title>"``
    - ``event_type`` — always ``"calcom_meeting"``
    - ``event_subtype`` — current state
    - ``body`` — raw webhook JSON (overwritten each transition)
    - ``details`` — existing details + ``"\\n"`` + new transition line
    - ``no_show`` — True when state ∈ {no_show_attendee, no_show_host}
    - ``timestamp`` — webhook createdAt (overwritten)
    - ``people`` — host's Person record id
    - ``owner`` — Elvis's workspace-member UUID

    Idempotency: ``external_id`` is non-unique in schema (see ai-277), so we
    query-then-patch instead of using the SDK's native assert path.

    The ``event_subtype`` select option is seeded just-in-time so the helper is
    robust against the bootstrap not having been re-run yet. Same for
    ``event_type:calcom_meeting``.

    Resilience to webhook delivery anomalies (flagged by roborev on the
    initial implementation):

    - **Duplicate retry**: Hookdeck retries on 4xx/5xx. A retried delivery
      arrives with an identical ``details_line`` (same timestamp + state +
      summary). We dedupe by substring-checking the existing ``details``
      field. Exact-match → no-op write returning ``"noop"``.

    - **Out-of-order delivery**: cal.com or the network can deliver a webhook
      whose ``timestamp`` precedes a transition we already wrote (e.g. a late
      ``BOOKING_CREATED`` arriving after ``BOOKING_CANCELLED``). We compare
      ``input.timestamp`` to the stored row's timestamp. Older →
      append to ``details`` only (preserving historical context) and leave
      ``event_subtype`` / ``body`` / ``timestamp`` at their newer values.
      Same-or-newer → full state write.
    """
    try:
        # Seed the select options just in time. Bootstrap is the primary path;
        # this is defense-in-depth so the helper works even on a workspace
        # where the bootstrap hasn't been re-run since this code landed.
        ensure_select_options(
            target_object=_OBJECT,
            attribute_slug="event_type",
            options=[_LIFECYCLE_EVENT_TYPE],
        )
        ensure_select_options(
            target_object=_OBJECT,
            attribute_slug="event_subtype",
            options=[input.event_subtype],
        )

        no_show = input.event_subtype in ("no_show_attendee", "no_show_host")
        name = f"{input.event_subtype} {input.meeting_title}"
        # Cal.com free-form fields (cancellationReason, ratingFeedback, meeting
        # title, etc.) can carry embedded newlines. Collapse them so every
        # stored transition is exactly one physical line — both for readability
        # and so the line-equality dedupe below stays correct (roborev round 3
        # caught the splitlines() boundary bug).
        details_line = _collapse_to_single_line(input.details_line)

        with get_client() as client:
            query_response = client.records.post_v2_objects_object_records_query(
                object=_OBJECT,
                filter_={"external_id": input.external_id},
            )
            existing = list(query_response.data or [])

            existing_details = ""
            existing_timestamp: datetime | None = None
            if existing:
                existing_details = _extract_text_slug(existing[0], "details")
                existing_timestamp = _extract_date_slug(existing[0], "timestamp")

            # Duplicate retry: an existing entry in the cumulative details
            # already EQUALS this transition. The forward storage invariant
            # is: every entry is exactly one physical line (enforced by
            # ``_collapse_to_single_line`` on write), separated by ``\n``. So
            # ``splitlines()`` returns the canonical list of stored entries
            # and line-equality is exact for any data this helper produced.
            #
            # This codebase has no legacy lifecycle rows (plan-02's helper
            # never wrote successfully against prod due to a slug mismatch,
            # and dev was empty at the time of this rewrite). If hypothetical
            # legacy rows with embedded newlines ever surface, dedupe may
            # miss against them on the first retry and append a copy; the
            # second retry then dedupes against that. Acceptable.
            if details_line in existing_details.splitlines():
                return ReliabilityEnvelope(
                    success=True,
                    partial_success=False,
                    action="noop",
                    record_id=existing[0].id.record_id,
                    warnings=[],
                    skipped_fields=[],
                    errors=[],
                    meta={
                        "output_schema_version": "v1",
                        "reason": "duplicate_details_line",
                        "meeting_lifecycle_event": input.model_dump(mode="json"),
                    },
                )

            new_details = (
                f"{existing_details}\n{details_line}"
                if existing_details
                else details_line
            )

            # Out-of-order: incoming webhook precedes the stored transition.
            # Write the historical line to details ONLY; leave the row's
            # event_subtype / body / timestamp at the (newer) values they
            # already hold so a late BOOKING_CREATED can't clobber a recorded
            # cancellation.
            is_stale = (
                existing_timestamp is not None and input.timestamp < existing_timestamp
            )

            if is_stale:
                values: dict[str, Any] = {
                    "details": [{"value": new_details}],
                }
            else:
                values = {
                    "external_id": [{"value": input.external_id}],
                    "name": [{"value": name}],
                    "event_type": [{"option": _LIFECYCLE_EVENT_TYPE}],
                    "event_subtype": [{"option": input.event_subtype}],
                    "body": [{"value": input.body_json}],
                    "details": [{"value": new_details}],
                    "no_show": [{"value": no_show}],
                    "timestamp": [{"value": input.timestamp.isoformat()}],
                    "people": [
                        {
                            "target_object": "people",
                            "target_record_id": input.host_person_record_id,
                        },
                    ],
                    "owner": [
                        {
                            "referenced_actor_type": "workspace-member",
                            "referenced_actor_id": _LIFECYCLE_OWNER_ACTOR_UUID,
                        },
                    ],
                }

            if existing:
                record_id = existing[0].id.record_id
                client.records.patch_v2_objects_object_records_record_id_(
                    object=_OBJECT,
                    record_id=record_id,
                    data=build_patch_record_request(values),
                )
                action = "updated"
            else:
                create_response = client.records.post_v2_objects_object_records(
                    object=_OBJECT,
                    data=build_post_record_request(values),
                )
                record_id = create_response.data.id.record_id
                action = "created"
    except Exception as exc:  # noqa: BLE001
        return _error_envelope(exc)

    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action=action,  # type: ignore[arg-type]
        record_id=record_id,
        warnings=[],
        skipped_fields=[],
        errors=[],
        meta={
            "output_schema_version": "v1",
            "meeting_lifecycle_event": input.model_dump(mode="json"),
        },
    )


def _collapse_to_single_line(text: str) -> str:
    """Collapse embedded newlines + whitespace runs into single spaces.

    Cal.com free-form fields can carry ``\\n`` characters (cancellation
    reasons users typed across two lines, rating feedback with bullet
    points, etc.). Stored ``details_line`` values must be exactly one
    physical line so the line-equality dedupe in the lifecycle helper
    works correctly — roborev round 3 caught the boundary case where a
    transition with embedded newlines would be broken into pieces by
    ``splitlines()`` and the same webhook retry would no longer dedupe.
    """
    return " ".join(text.split())


def _extract_text_slug(record: Any, slug: str) -> str:
    """Return the text value of ``slug`` on an Attio record, or "" if absent.

    Attio returns multi-value attributes as a list of dicts even for
    single-value text fields. The first entry's ``value`` is the active text.
    Defensive against missing slug / empty list / non-dict shape — used by the
    lifecycle helper to read existing ``details`` before computing cumulative
    output.
    """
    dump = record.model_dump() if hasattr(record, "model_dump") else dict(record)
    values = (dump.get("values") or {}).get(slug) or []
    if not values:
        return ""
    first = values[0]
    if isinstance(first, dict):
        return first.get("value") or ""
    return ""


def _extract_date_slug(record: Any, slug: str) -> datetime | None:
    """Return the date/timestamp value of ``slug`` parsed as a tz-aware UTC datetime.

    Attio's ``date`` attribute on the live ``tracking_events`` schema stores
    values as ISO-8601 strings. The wire format can be date-only (``YYYY-MM-DD``)
    or datetime (with or without timezone offset). We normalize all three to
    tz-aware UTC so comparisons against ``input.timestamp`` (always tz-aware)
    don't raise ``TypeError`` for the naive-vs-aware case.

    Returns ``None`` for missing slug / empty list / unparseable input — the
    caller falls back to treating the row as having no recorded timestamp.
    """
    raw = _extract_text_slug(record, slug)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Date-only ISO strings parse as naive; same for datetime strings without an
    # offset. Force UTC so the caller can compare against tz-aware input.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _error_envelope(error: Exception) -> ReliabilityEnvelope:
    classified = classify_error(error, strict=False)
    return ReliabilityEnvelope(
        success=False,
        partial_success=False,
        action="failed",
        record_id=None,
        warnings=[],
        skipped_fields=[],
        errors=[
            ErrorEntry(
                code=classified.code,
                message=classified.message,
                error_type=classified.error_type,
                fatal=classified.fatal,
                field=classified.field,
            ),
        ],
        meta={"output_schema_version": "v1"},
    )
