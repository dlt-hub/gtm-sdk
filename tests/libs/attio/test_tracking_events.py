from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError


def _valid_kwargs() -> dict[str, Any]:
    return dict(
        external_id="rb2b:abc123",
        name="https://example.test/pricing",
        event_type="rb2b_visit",
        event_timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
        body_json='{"raw": "payload"}',
    )


def test_tracking_event_input_minimal() -> None:
    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(**_valid_kwargs())
    assert i.related_person_record_id is None
    assert i.event_subtype is None


def test_tracking_event_input_with_resolved_person_ref() -> None:
    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(
        **_valid_kwargs(),
        event_subtype="repeat_visit",
        related_person_record_id="pe_123",
    )
    assert i.related_person_record_id == "pe_123"
    assert i.event_subtype == "repeat_visit"


def test_tracking_event_input_forbids_extra() -> None:
    from libs.attio.models import TrackingEventInput

    with pytest.raises(ValidationError):
        TrackingEventInput(**_valid_kwargs(), bogus="x")  # pyright: ignore[reportCallIssue]  # pyrefly: ignore[unexpected-keyword]


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_find_or_create_tracking_event_miss_then_create(
    mock_get_client,
    mock_ensure_options,
) -> None:
    from libs.attio.tracking_events import find_or_create_tracking_event

    client = MagicMock()
    # Query returns zero rows → miss
    client.records.post_v2_objects_object_records_query.return_value.data = []
    # Create returns the new record id
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(**_valid_kwargs())
    env = find_or_create_tracking_event(i)

    assert env.success is True
    assert env.action == "created"
    assert env.record_id == "te_new"

    # Query was called with the external_id filter
    _args, kwargs = client.records.post_v2_objects_object_records_query.call_args
    assert kwargs.get("object") == "tracking_events"


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_find_or_create_tracking_event_hit_then_patch(
    mock_get_client,
    mock_ensure_options,
) -> None:
    from libs.attio.tracking_events import find_or_create_tracking_event

    client = MagicMock()
    # Query returns one row → hit
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    # Patch returns the same record id
    patch_resp = MagicMock()
    patch_resp.data.id.record_id = "te_existing"
    client.records.patch_v2_objects_object_records_record_id_.return_value = patch_resp
    mock_get_client.return_value.__enter__.return_value = client

    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(**_valid_kwargs())
    env = find_or_create_tracking_event(i)

    assert env.success is True
    assert env.action == "updated"
    assert env.record_id == "te_existing"
    client.records.post_v2_objects_object_records.assert_not_called()
    client.records.patch_v2_objects_object_records_record_id_.assert_called_once()


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_find_or_create_tracking_event_sdk_error_returns_envelope(
    mock_get_client,
    mock_ensure_options,
) -> None:
    from libs.attio.tracking_events import find_or_create_tracking_event

    client = MagicMock()
    client.records.post_v2_objects_object_records_query.side_effect = Exception("boom")
    mock_get_client.return_value.__enter__.return_value = client

    from libs.attio.models import TrackingEventInput

    i = TrackingEventInput(**_valid_kwargs())
    env = find_or_create_tracking_event(i)

    assert env.success is False
    assert env.action == "failed"
    assert env.errors and env.errors[0].fatal in (True, False)  # classified, not raised


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_find_or_create_tracking_event_jit_seeds_event_type_and_subtype(
    mock_get_client,
    mock_ensure_options,
) -> None:
    """event_type and event_subtype options self-register on first write so
    new sources never need a manual bootstrap step."""
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.return_value.data = []
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    from libs.attio.models import TrackingEventInput
    from libs.attio.tracking_events import find_or_create_tracking_event

    i = TrackingEventInput(**_valid_kwargs(), event_subtype="repeat_visit")
    find_or_create_tracking_event(i)

    seeded = [c.kwargs for c in mock_ensure_options.call_args_list]
    assert {
        "target_object": "tracking_events",
        "attribute_slug": "event_type",
        "options": ["rb2b_visit"],
    } in seeded
    assert {
        "target_object": "tracking_events",
        "attribute_slug": "event_subtype",
        "options": ["repeat_visit"],
    } in seeded


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_find_or_create_tracking_event_omits_subtype_seed_when_absent(
    mock_get_client,
    mock_ensure_options,
) -> None:
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.return_value.data = []
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    from libs.attio.models import TrackingEventInput
    from libs.attio.tracking_events import find_or_create_tracking_event

    i = TrackingEventInput(**_valid_kwargs())  # no event_subtype
    find_or_create_tracking_event(i)

    seeded_slugs = [
        c.kwargs.get("attribute_slug") for c in mock_ensure_options.call_args_list
    ]
    assert "event_type" in seeded_slugs
    assert "event_subtype" not in seeded_slugs
