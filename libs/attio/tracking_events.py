from __future__ import annotations

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
    """Seed select-option titles the payload references just-in-time.

    ``event_type`` and ``event_subtype`` are both open-ended selects per
    their workspace descriptions ("Expand as new types are tracked.",
    "Kept open-ended for future subtypes."). New sources (rb2b, fathom,
    caldotcom lifecycle...) self-register their option titles on first
    write rather than requiring a manual bootstrap step.
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


def find_or_create_meeting_lifecycle_event(
    input: MeetingLifecycleEventInput,
) -> ReliabilityEnvelope:
    """Idempotently upsert a ``tracking_events`` row for a Cal.com lifecycle event.

    Writes only the slugs that exist on the live workspace schema:
    ``name``, ``event_type``, ``external_id``, ``body``, ``timestamp``, ``contact``.

    The legacy ``find_or_create_tracking_event`` writes ``captured_url`` etc.
    which don't exist in this workspace (see plan-02 side-finding). This helper
    bypasses that bug by emitting a narrower value-dict.

    Idempotency: ``external_id`` is non-unique in schema, so we query-then-patch
    (mirroring the existing pattern). The event_type select option is seeded
    just-in-time so the first ``meeting_cancelled`` event also creates the
    option.
    """
    try:
        # Seed the select option if it's new — otherwise the write 400s with
        # "Cannot find option value ...".
        ensure_select_options(
            target_object=_OBJECT,
            attribute_slug="event_type",
            options=[input.event_type],
        )

        values: dict[str, Any] = {
            "name": [{"value": input.name}],
            "event_type": [{"option": input.event_type}],
            "external_id": [{"value": input.external_id}],
            "body": [{"value": input.body_json}],
            "timestamp": [{"value": input.timestamp.isoformat()}],
        }
        if input.contact_person_record_id is not None:
            values["contact"] = [
                {
                    "target_object": "people",
                    "target_record_id": input.contact_person_record_id,
                },
            ]

        with get_client() as client:
            query_response = client.records.post_v2_objects_object_records_query(
                object=_OBJECT,
                filter_={"external_id": input.external_id},
            )
            existing = list(query_response.data or [])

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
