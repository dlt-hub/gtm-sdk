"""Tests for the per-meeting ``tracking_events`` lifecycle helper.

Mirrors the mocking pattern in ``test_tracking_events.py`` so the SDK client
boundary is fully stubbed and we test the value-builder + query-then-patch
logic, not the network. Per the spec at
``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``
the helper writes ONE row per meeting (keyed by ``external_id =
canonical_meeting_uid(host, start)``), advances ``event_subtype`` on each
transition, and appends a line to the cumulative ``details`` text.
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
        external_id="ical-uid-abc",
        meeting_title="Acme × dlt pricing call",
        event_subtype="cancelled",
        timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
        body_json='{"reason":"redacted"}',
        details_line="2026-05-14T00:00:00Z cancelled — by host@dlthub.com: reason",
        host_person_record_id="pe_host_1",
        owner_member_id="wm_test_owner",
    )
    base.update(overrides)
    return MeetingLifecycleEventInput(**base)  # type: ignore[arg-type]


def test_input_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MeetingLifecycleEventInput(  # pyright: ignore[reportCallIssue]
            external_id="x",
            meeting_title="x",
            event_subtype="cancelled",
            timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json="{}",
            details_line="x",
            host_person_record_id="pe_x",
            bogus="nope",  # type: ignore[call-arg]  # pyrefly: ignore[unexpected-keyword]
        )


def test_input_rejects_unknown_event_subtype() -> None:
    with pytest.raises(ValidationError):
        MeetingLifecycleEventInput(
            external_id="x",
            meeting_title="x",
            event_subtype="meeting_invented",  # type: ignore[arg-type]
            timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json="{}",
            details_line="x",
            host_person_record_id="pe_x",
        )


def _values_from_call(call: MagicMock) -> dict[str, object]:
    """Reach through the SDK request wrapper to get the underlying values dict."""
    request_obj = call.kwargs["data"]
    values = getattr(request_obj, "values", None) or request_obj.__dict__.get("values")
    assert values is not None
    return values  # type: ignore[no-any-return]


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_miss_then_create_writes_full_value_set(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,
) -> None:
    """First arrival for a meeting → CREATE with every slug populated."""
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

    # event_type AND event_subtype select options were seeded JIT.
    seeded = {
        c.kwargs.get("attribute_slug") for c in mock_ensure_options.call_args_list
    }
    assert seeded == {"event_type", "event_subtype"}

    # Query scoped to the right object + external_id.
    q_args = client.records.post_v2_objects_object_records_query.call_args
    assert q_args.kwargs["object"] == "tracking_events"
    assert q_args.kwargs["filter_"] == {"external_id": "ical-uid-abc"}

    # Every required slug present.
    values = _values_from_call(client.records.post_v2_objects_object_records.call_args)
    for slug in (
        "external_id",
        "name",
        "event_type",
        "event_subtype",
        "body",
        "details",
        "no_show",
        "timestamp",
        "people",
        "owner",
    ):
        assert slug in values, f"missing slug: {slug}"

    # event_type pinned to the namespace value.
    assert values["event_type"] == [{"option": "calcom_meeting"}]
    assert values["event_subtype"] == [{"option": "cancelled"}]
    # name prepends event_subtype.
    assert values["name"] == [{"value": "cancelled Acme × dlt pricing call"}]
    # people slug points at the host record (NOT the legacy `contact` slug).
    assert values["people"] == [
        {"target_object": "people", "target_record_id": "pe_host_1"},
    ]
    assert "contact" not in values
    # no_show false for non-no_show variants.
    assert values["no_show"] == [{"value": False}]
    # owner carries the per-workspace actor id supplied by the orchestration
    # layer (resolved from the token), NOT a hardcoded UUID (ai-ica).
    assert values["owner"] == [
        {
            "referenced_actor_type": "workspace-member",
            "referenced_actor_id": "wm_test_owner",
        },
    ]
    # First arrival → details starts with this transition's line, no prefix.
    assert values["details"] == [
        {"value": "2026-05-14T00:00:00Z cancelled — by host@dlthub.com: reason"},
    ]


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_owner_omitted_when_member_id_unresolved(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """owner_member_id=None → owner slug omitted, not written as an invalid ref.

    Writing an invalid actor reference is what failed the whole prod lifecycle
    write in ai-ica; omitting best-effort owner metadata is the safe fallback.
    """
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.return_value.data = []
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    env = find_or_create_meeting_lifecycle_event(_valid_input(owner_member_id=None))

    assert env.success is True
    values = _values_from_call(client.records.post_v2_objects_object_records.call_args)
    assert "owner" not in values
    # The rest of the row is still written.
    assert values["event_type"] == [{"option": "calcom_meeting"}]


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_hit_then_patch_appends_details_cumulatively(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """Existing row → PATCH with appended details (existing + "\\n" + new line)."""
    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    # Existing details from prior scheduled-state webhook.
    existing.model_dump.return_value = {
        "values": {
            "details": [
                {"value": "2026-05-10T12:00:00Z scheduled — host: a; attendees: b"},
            ],
        },
    }
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

    values = _values_from_call(
        client.records.patch_v2_objects_object_records_record_id_.call_args,
    )
    # Cumulative: prior line newlined to new transition line.
    expected_details = (
        "2026-05-10T12:00:00Z scheduled — host: a; attendees: b"
        "\n2026-05-14T00:00:00Z cancelled — by host@dlthub.com: reason"
    )
    assert values["details"] == [{"value": expected_details}]


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_no_show_variants_set_no_show_true(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """``event_subtype`` ∈ {no_show_attendee, no_show_host} → no_show=True."""
    client = MagicMock()
    client.records.post_v2_objects_object_records_query.return_value.data = []
    create_resp = MagicMock()
    create_resp.data.id.record_id = "te_new"
    client.records.post_v2_objects_object_records.return_value = create_resp
    mock_get_client.return_value.__enter__.return_value = client

    for subtype in ("no_show_attendee", "no_show_host"):
        client.records.post_v2_objects_object_records.reset_mock()
        find_or_create_meeting_lifecycle_event(_valid_input(event_subtype=subtype))
        values = _values_from_call(
            client.records.post_v2_objects_object_records.call_args,
        )
        assert values["no_show"] == [{"value": True}], (
            f"no_show should be True for event_subtype={subtype}"
        )


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_duplicate_retry_is_noop(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """Hookdeck retry → identical details_line already in existing row → noop."""
    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    existing.model_dump.return_value = {
        "values": {
            "details": [
                {
                    "value": (
                        "2026-05-14T00:00:00Z cancelled — by host@dlthub.com: reason"
                    ),
                },
            ],
            "timestamp": [{"value": "2026-05-14T00:00:00+00:00"}],
        },
    }
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    mock_get_client.return_value.__enter__.return_value = client

    env = find_or_create_meeting_lifecycle_event(_valid_input())

    assert env.success is True
    assert env.action == "noop"
    assert env.record_id == "te_existing"
    # No PATCH issued — duplicate is a true no-op.
    client.records.patch_v2_objects_object_records_record_id_.assert_not_called()
    client.records.post_v2_objects_object_records.assert_not_called()
    assert env.meta.get("reason") == "duplicate_details_line"


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_stale_arrival_appends_details_only(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """Late-arriving older webhook → details appended, state NOT reverted.

    Example: a BOOKING_CREATED is delivered AFTER a BOOKING_CANCELLED has
    already been recorded. Without this guard the late CREATED would clobber
    event_subtype back to ``scheduled`` and overwrite ``body`` with the stale
    creation payload.
    """
    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    existing.model_dump.return_value = {
        "values": {
            # Existing row is the LATER cancellation.
            "details": [
                {
                    "value": (
                        "2026-05-15T12:00:00Z cancelled — by host@dlthub.com: reason"
                    ),
                },
            ],
            "timestamp": [{"value": "2026-05-15T12:00:00+00:00"}],
            "event_subtype": [{"option": "cancelled"}],
        },
    }
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    patch_resp = MagicMock()
    patch_resp.data.id.record_id = "te_existing"
    client.records.patch_v2_objects_object_records_record_id_.return_value = patch_resp
    mock_get_client.return_value.__enter__.return_value = client

    # Incoming is an OLDER scheduled-state webhook arriving late.
    env = find_or_create_meeting_lifecycle_event(
        _valid_input(
            event_subtype="scheduled",
            timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            details_line=("2026-05-14T00:00:00Z scheduled — host: a; attendees: b"),
        ),
    )

    assert env.success is True
    assert env.action == "updated"

    # Only `details` was written. event_subtype / body / timestamp / name etc.
    # are absent — the existing newer state is preserved.
    values = _values_from_call(
        client.records.patch_v2_objects_object_records_record_id_.call_args,
    )
    assert set(values.keys()) == {"details"}
    # Late line is appended (the cumulative log shows historical context).
    assert values["details"] == [
        {
            "value": (
                "2026-05-15T12:00:00Z cancelled — by host@dlthub.com: reason"
                "\n2026-05-14T00:00:00Z scheduled — host: a; attendees: b"
            ),
        },
    ]


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_substring_match_does_not_count_as_duplicate(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """A new details_line that is a SUBSTRING of an existing line must NOT
    trigger the duplicate-retry guard — that would silently drop a real
    transition. Line equality, not substring containment, is the rule.
    """
    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    # Long existing line that happens to contain the short new line as a
    # substring (e.g. a fuller details entry that quotes the short variant).
    existing.model_dump.return_value = {
        "values": {
            "details": [
                {
                    "value": (
                        "2026-05-14T00:00:00Z cancelled — by host@dlthub.com: "
                        "rescheduling conflict with another meeting that "
                        "matters more"
                    ),
                },
            ],
            "timestamp": [{"value": "2026-05-14T00:00:00+00:00"}],
        },
    }
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    patch_resp = MagicMock()
    patch_resp.data.id.record_id = "te_existing"
    client.records.patch_v2_objects_object_records_record_id_.return_value = patch_resp
    mock_get_client.return_value.__enter__.return_value = client

    # New line is a strict substring of the existing line but NOT a full-line
    # match.
    env = find_or_create_meeting_lifecycle_event(
        _valid_input(
            details_line="2026-05-14T00:00:00Z cancelled — by host@dlthub.com: rescheduling",
        ),
    )

    # Must NOT noop. PATCH should fire and details should grow.
    assert env.action == "updated"
    values = _values_from_call(
        client.records.patch_v2_objects_object_records_record_id_.call_args,
    )
    assert "details" in values


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_naive_existing_timestamp_does_not_crash_stale_check(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """Attio's date attribute may emit naive ISO strings (no offset / date-only).
    The helper must normalize to tz-aware UTC before comparing to the
    tz-aware input timestamp; the prior implementation raised TypeError
    on the naive-vs-aware comparison.
    """
    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    # Date-only string — fromisoformat returns a NAIVE datetime.
    existing.model_dump.return_value = {
        "values": {
            "details": [{"value": "prior line"}],
            "timestamp": [{"value": "2026-05-14"}],
        },
    }
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    patch_resp = MagicMock()
    patch_resp.data.id.record_id = "te_existing"
    client.records.patch_v2_objects_object_records_record_id_.return_value = patch_resp
    mock_get_client.return_value.__enter__.return_value = client

    # tz-aware input — the comparison must not raise.
    env = find_or_create_meeting_lifecycle_event(
        _valid_input(timestamp=datetime(2026, 5, 15, tzinfo=timezone.utc)),
    )
    assert env.success is True


@patch("libs.attio.tracking_events.ensure_select_options")
@patch("libs.attio.tracking_events.get_client")
def test_embedded_newlines_in_details_line_still_dedupe(
    mock_get_client: MagicMock,
    mock_ensure_options: MagicMock,  # noqa: ARG001
) -> None:
    """Cal.com free-form fields (cancellationReason etc.) can carry embedded
    newlines. The helper collapses them to single spaces before storing AND
    before comparing — so a retry with the same multi-line input still
    dedupes against the previously stored (collapsed) line.
    """
    multi_line = (
        "2026-05-14T00:00:00Z cancelled — by host@dlthub.com:\n"
        "scheduling conflict\n"
        "with another meeting"
    )
    # What the helper should have stored on the first arrival (newlines
    # collapsed to single spaces).
    collapsed = (
        "2026-05-14T00:00:00Z cancelled — by host@dlthub.com: "
        "scheduling conflict with another meeting"
    )

    client = MagicMock()
    existing = MagicMock()
    existing.id.record_id = "te_existing"
    existing.model_dump.return_value = {
        "values": {
            "details": [{"value": collapsed}],
            "timestamp": [{"value": "2026-05-14T00:00:00+00:00"}],
        },
    }
    client.records.post_v2_objects_object_records_query.return_value.data = [existing]
    mock_get_client.return_value.__enter__.return_value = client

    # Retry comes in with the original multi-line details_line.
    env = find_or_create_meeting_lifecycle_event(_valid_input(details_line=multi_line))

    assert env.action == "noop"
    client.records.patch_v2_objects_object_records_record_id_.assert_not_called()
    client.records.post_v2_objects_object_records.assert_not_called()


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
