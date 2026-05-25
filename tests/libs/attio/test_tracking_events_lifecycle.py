"""Tests for the meeting-lifecycle ``tracking_events`` helper.

Mirrors the mocking pattern in ``test_tracking_events.py`` so the SDK client
boundary is fully stubbed and we test the narrow value-builder + query-then-
patch logic, not the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from libs.attio.models import MeetingLifecycleEventInput
from libs.attio.tracking_events import find_or_create_meeting_lifecycle_event


def _valid_input(**overrides: object) -> MeetingLifecycleEventInput:
    base: dict[str, object] = dict(
        external_id="caldotcom:meeting_cancelled:bk_1:attendee@example.com",
        name="Cal.com booking cancelled",
        event_type="meeting_cancelled",
        timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
        body_json='{"reason":"redacted"}',
        contact_person_record_id="pe_attendee_1",
    )
    base.update(overrides)
    return MeetingLifecycleEventInput(**base)  # type: ignore[arg-type]


def test_input_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MeetingLifecycleEventInput(  # pyright: ignore[reportCallIssue]
            external_id="x",
            name="x",
            event_type="meeting_cancelled",
            timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json="{}",
            bogus="nope",  # type: ignore[call-arg]  # pyrefly: ignore[unexpected-keyword]
        )


def test_input_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        MeetingLifecycleEventInput(
            external_id="x",
            name="x",
            event_type="meeting_invented",  # type: ignore[arg-type]
            timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json="{}",
        )


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_miss_then_create_seeds_option_and_writes(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,
) -> None:
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.return_value.data = []
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    env = find_or_create_meeting_lifecycle_event(_valid_input())

    assert env.success is True
    assert env.action == "created"
    assert env.record_id == "te_new"

    # Option vocabulary was seeded JIT.
    mock_ensure_options.assert_called_once()
    _args, kwargs = mock_ensure_options.call_args
    assert kwargs.get("attribute_slug") == "event_type"
    assert kwargs.get("options") == ["meeting_cancelled"]

    # Query was scoped to the right object + external_id.
    q_args = client.records.post_v2_objects_object_records_query.call_args
    assert q_args.kwargs["object"] == "tracking_events"
    assert q_args.kwargs["filter_"] == {
        "external_id": "caldotcom:meeting_cancelled:bk_1:attendee@example.com",
    }


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_hit_then_patch_returns_updated(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    patch_resp = MagicMock()
    patch_resp.data.id.record_id = "te_existing"
    client.records.patch_v2_objects_object_records_record_id_.return_value = patch_resp
    mock_get_client.return_value.__enter__.return_value = client

    env = find_or_create_meeting_lifecycle_event(_valid_input())

    assert env.success is True
    assert env.action == "updated"
    assert env.record_id == "te_existing"
    client.records.post_v2_objects_object_records.assert_not_called()
    client.records.patch_v2_objects_object_records_record_id_.assert_called_once()


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_contact_none_writes_without_contact_field(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """Attendee not in Attio → write the audit row with contact omitted."""
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.return_value.data = []
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    env = find_or_create_meeting_lifecycle_event(
        _valid_input(contact_person_record_id=None),
    )

    assert env.success is True

    # The create call carries values WITHOUT a "contact" key.
    create_call = client.records.post_v2_objects_object_records.call_args
    request_obj = create_call.kwargs["data"]
    # The SDK wraps values in a request type — reach through to the underlying
    # values dict. Real SDK object exposes ``.values`` as kwarg-mirroring attr.
    values = getattr(request_obj, "values", None) or request_obj.__dict__.get("values")
    assert values is not None
    assert "contact" not in values
    # And the other slugs are present.
    for slug in ("name", "event_type", "external_id", "body", "timestamp"):
        assert slug in values, f"missing slug: {slug}"


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_sdk_failure_returns_failed_envelope(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.side_effect = Exception(
        "boom",
    )
    mock_get_client.return_value.__enter__.return_value = client

    env = find_or_create_meeting_lifecycle_event(_valid_input())

    assert env.success is False
    assert env.action == "failed"
    assert env.errors
