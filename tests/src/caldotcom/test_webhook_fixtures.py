"""Per-trigger dispatch tests against the recorded Cal.com fixtures.

Plan-02 contract:

- ``BOOKING_CREATED`` -> ``[UpsertMeeting]`` (one find-or-create per webhook)
- ``BOOKING_CANCELLED`` / ``BOOKING_RESCHEDULED`` / ``MEETING_ENDED`` ->
  one ``EmitMeetingLifecycleEvent`` per attendee
- ``BOOKING_NO_SHOW_UPDATED`` -> one ``EmitMeetingLifecycleEvent`` per attendee
  with ``noShow=true``, after a Cal.com API fetch (mocked in tests)
- ``MEETING_STARTED`` / ``PING`` -> ``[]`` (typed no-ops, valid=False)

Attio Meeting API is append-only (see plan-02 — PATCH/DELETE return 404), so
lifecycle events go to ``tracking_events`` linked to each attendee's Person.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from libs.caldotcom.models import BookingCreatedPayload
from libs.meetings import canonical_meeting_uid
from src.attio.ops import EmitMeetingLifecycleEvent, UpsertMeeting
from src.caldotcom.webhook.booking import Webhook

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]


def _load(fixture_path: str) -> Webhook:
    payload = orjson.loads((_REPO_ROOT / fixture_path).read_bytes())
    return Webhook.model_validate(payload)


class _FakeCalcomClient:
    """Stand-in for ``CalcomClient`` in NO_SHOW tests."""

    def __init__(self, response: BookingCreatedPayload | None) -> None:
        self._response = response

    def get_booking(self, booking_uid: str) -> BookingCreatedPayload | None:  # noqa: ARG002
        return self._response

    def __enter__(self) -> _FakeCalcomClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def _fake_booking_for_no_show() -> BookingCreatedPayload:
    """Synthesized BOOKING_CREATED payload matching the no_show fixture's bookingUid."""
    return BookingCreatedPayload.model_validate(
        {
            "triggerEvent": "BOOKING_CREATED",
            "uid": "wjvt1yoCAGK6KmaLnmszng",
            "start": "2026-05-11T07:00:00.000Z",
            "end": "2026-05-11T07:30:00.000Z",
            "title": "Discovery call",
            "status": "accepted",
            "hosts": [
                {
                    "id": 1,
                    "name": "Host",
                    "email": "alex@example.com",
                    "displayEmail": "alex@example.com",
                    "username": "alex-host",
                    "timeZone": "UTC",
                },
            ],
            "attendees": [
                {
                    "name": "Sam Attendee",
                    "email": "sam@example.com",
                    "displayEmail": "sam@example.com",
                    "timeZone": "UTC",
                    "absent": False,
                },
            ],
            "bookingFieldsResponses": {},
        },
    )


class TestGateSweepAcrossFixtures:
    @pytest.mark.parametrize(
        ("fixture", "expected_valid"),
        [
            ("api/samples/caldotcom.booking.created.redacted.json", True),
            ("api/samples/caldotcom.booking.cancelled.redacted.json", True),
            ("api/samples/caldotcom.booking.rescheduled.redacted.json", True),
            ("api/samples/caldotcom.booking.no_show_updated.redacted.json", True),
            ("api/samples/caldotcom.meeting.ended.redacted.json", True),
            # Plan-02: MEETING_STARTED and PING are typed no-ops.
            ("api/samples/caldotcom.meeting.started.redacted.json", False),
            ("api/samples/caldotcom.ping.redacted.json", False),
        ],
    )
    def test_attio_is_valid_webhook(
        self,
        fixture: str,
        expected_valid: bool,
    ) -> None:
        w = _load(fixture)
        assert w.attio_is_valid_webhook() is expected_valid

    def test_meeting_started_reason_is_specific(self) -> None:
        w = _load("api/samples/caldotcom.meeting.started.redacted.json")
        assert "MEETING_STARTED" in w.attio_get_invalid_webhook_error_msg()

    def test_ping_reason_is_specific(self) -> None:
        w = _load("api/samples/caldotcom.ping.redacted.json")
        assert "PING" in w.attio_get_invalid_webhook_error_msg()


class TestOperationDispatch:
    def test_created_emits_upsert_meeting(self) -> None:
        w = _load("api/samples/caldotcom.booking.created.redacted.json")
        ops = w.attio_get_operations()
        assert len(ops) == 1
        assert isinstance(ops[0], UpsertMeeting)
        # Canonical ical_uid derived from host email + start.
        expected = canonical_meeting_uid(
            host_email="host@dlthub.com",
            start=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
        )
        assert ops[0].external_ref.ical_uid == expected

    def test_cancelled_emits_lifecycle_event_per_attendee(self) -> None:
        w = _load("api/samples/caldotcom.booking.cancelled.redacted.json")
        ops = w.attio_get_operations()
        # One attendee in fixture -> exactly one lifecycle op.
        assert len(ops) == 1
        op = ops[0]
        assert isinstance(op, EmitMeetingLifecycleEvent)
        assert op.event_type == "meeting_cancelled"
        assert op.attendee_email == "sam@example.com"
        # ical_uid derived from organizer.email + startTime (== OLD time).
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=datetime(2026, 5, 6, 10, 30, 0, tzinfo=UTC),
        )
        assert op.meeting_ical_uid == expected
        body = json.loads(op.body_json)
        assert body["reason"] == "redacted"
        assert body["cancelled_by"] == "alex@example.com"

    def test_rescheduled_emits_lifecycle_event_with_old_and_new_times(self) -> None:
        w = _load("api/samples/caldotcom.booking.rescheduled.redacted.json")
        ops = w.attio_get_operations()
        assert len(ops) == 1
        op = ops[0]
        assert isinstance(op, EmitMeetingLifecycleEvent)
        assert op.event_type == "meeting_rescheduled"
        # ical_uid uses startTime (OLD pre-reschedule time) per Cal.com semantics.
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC),
        )
        assert op.meeting_ical_uid == expected
        body = json.loads(op.body_json)
        # Both times present and distinct.
        assert body["old_start"].startswith("2026-05-14T10:00:00")
        assert body["new_start"].startswith("2026-05-14T11:00:00")
        assert body["rescheduled_by"] == "sam@example.com"

    def test_rescheduled_does_not_emit_upsert_meeting(self) -> None:
        """Critical: reschedule must NOT create a duplicate at the new ical_uid."""
        w = _load("api/samples/caldotcom.booking.rescheduled.redacted.json")
        ops = w.attio_get_operations()
        assert not any(isinstance(o, UpsertMeeting) for o in ops)

    def test_no_show_fetches_booking_then_emits_per_no_show_attendee(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_booking = _fake_booking_for_no_show()
        monkeypatch.setattr(
            Webhook,
            "_calcom_client",
            lambda self: _FakeCalcomClient(fake_booking),  # noqa: ARG005  # pyright: ignore[reportUnknownLambdaType]
        )
        w = _load("api/samples/caldotcom.booking.no_show_updated.redacted.json")
        ops = w.attio_get_operations()
        # Fixture has one attendee with noShow=true.
        assert len(ops) == 1
        op = ops[0]
        assert isinstance(op, EmitMeetingLifecycleEvent)
        assert op.event_type == "meeting_no_show"
        assert op.attendee_email == "sam@example.com"
        # ical_uid resolved from the fetched booking (alex@example.com + start).
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=fake_booking.start,
        )
        assert op.meeting_ical_uid == expected
        body = json.loads(op.body_json)
        assert body["booking_lookup_succeeded"] is True

    def test_no_show_with_api_failure_still_emits_with_null_ical(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If Cal.com API errors, emit events anyway (audit > silent drop)."""

        class _FailingClient:
            def get_booking(self, _uid: str) -> BookingCreatedPayload | None:
                raise RuntimeError("calcom unreachable")

            def __enter__(self) -> _FailingClient:
                return self

            def __exit__(self, *_: Any) -> None:
                return None

        monkeypatch.setattr(
            Webhook,
            "_calcom_client",
            lambda self: _FailingClient(),  # noqa: ARG005  # pyright: ignore[reportUnknownLambdaType]
        )
        w = _load("api/samples/caldotcom.booking.no_show_updated.redacted.json")
        ops = w.attio_get_operations()
        assert len(ops) == 1
        op = ops[0]
        assert isinstance(op, EmitMeetingLifecycleEvent)
        assert op.meeting_ical_uid is None
        body = json.loads(op.body_json)
        assert body["booking_lookup_succeeded"] is False

    def test_meeting_ended_without_no_show_emits_meeting_ended(self) -> None:
        w = _load("api/samples/caldotcom.meeting.ended.redacted.json")
        ops = w.attio_get_operations()
        # Two attendees in the fixture -> two lifecycle ops.
        assert len(ops) == 2
        assert all(isinstance(o, EmitMeetingLifecycleEvent) for o in ops)
        types = {o.event_type for o in ops if isinstance(o, EmitMeetingLifecycleEvent)}
        assert types == {"meeting_ended"}

    def test_meeting_started_emits_nothing(self) -> None:
        w = _load("api/samples/caldotcom.meeting.started.redacted.json")
        assert w.attio_get_operations() == []

    def test_ping_emits_nothing(self) -> None:
        w = _load("api/samples/caldotcom.ping.redacted.json")
        assert w.attio_get_operations() == []


class TestCrossTriggerInvariant:
    """The audit story only works if CREATED and a later CANCELLED for the same
    logical meeting (same host email + start) produce the SAME ``meeting_ical_uid``.
    """

    def test_created_and_cancelled_share_ical_uid_when_host_and_start_match(
        self,
    ) -> None:
        host = "alex@example.com"
        start = datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC)

        # Synthesize a CREATED webhook with the same host + start as a later CANCELLED.
        created_envelope = {
            "triggerEvent": "BOOKING_CREATED",
            "createdAt": "2026-05-25T10:00:00.000Z",
            "payload": {
                "uid": "bk-shared",
                "start": start.isoformat(),
                "end": "2026-06-01T15:30:00.000Z",
                "status": "accepted",
                "hosts": [
                    {
                        "id": 1,
                        "name": "Alex Host",
                        "email": host,
                        "displayEmail": host,
                        "username": "alex-host",
                        "timeZone": "UTC",
                    },
                ],
                "attendees": [
                    {
                        "name": "Sam",
                        "email": "sam@example.com",
                        "displayEmail": "sam@example.com",
                        "timeZone": "UTC",
                        "absent": False,
                    },
                ],
                "bookingFieldsResponses": {},
            },
        }
        cancelled_envelope = {
            "triggerEvent": "BOOKING_CANCELLED",
            "createdAt": "2026-05-25T11:00:00.000Z",
            "payload": {
                "uid": "bk-shared",
                "startTime": start.isoformat(),
                "endTime": "2026-06-01T15:30:00.000Z",
                "organizer": {"id": 1, "name": "Alex", "email": host},
                "attendees": [{"email": "sam@example.com"}],
                "cancellationReason": "test",
                "cancelledBy": host,
            },
        }

        created = Webhook.model_validate(created_envelope).attio_get_operations()
        cancelled = Webhook.model_validate(cancelled_envelope).attio_get_operations()

        assert isinstance(created[0], UpsertMeeting)
        assert isinstance(cancelled[0], EmitMeetingLifecycleEvent)
        assert created[0].external_ref.ical_uid == cancelled[0].meeting_ical_uid
