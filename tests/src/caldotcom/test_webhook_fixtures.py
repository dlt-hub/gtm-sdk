"""Per-trigger dispatch tests against the recorded Cal.com fixtures.

Spec at
``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``.

Each cal.com webhook produces:

1. ``UpsertCompany`` for the host's email domain.
2. ``UpsertPerson`` for the host (the lifecycle dispatcher's LookupTable
   resolves the host PersonRef on EmitMeetingLifecycleEvent from this op).
3. ``UpsertMeeting`` — ONLY on ``BOOKING_CREATED``.
4. ``EmitMeetingLifecycleEvent`` — ONE per meeting (NOT per attendee). The
   row's ``external_id`` is ``canonical_meeting_uid(host, start)``; the same
   row is PATCHed by every subsequent webhook for the same meeting,
   advancing ``event_subtype`` and appending to cumulative ``details``.

Departure from plan-02: no more per-attendee rows. Attendee identity lives
in the row's ``body`` (raw JSON) and ``details`` (one-line summary).
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
from src.attio.ops import (
    AttioOp,
    EmitMeetingLifecycleEvent,
    UpsertCompany,
    UpsertMeeting,
    UpsertPerson,
)
from src.caldotcom.webhook.booking import Webhook

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]


def _load(fixture_path: str) -> Webhook:
    payload = orjson.loads((_REPO_ROOT / fixture_path).read_bytes())
    return Webhook.model_validate(payload)


def _find_lifecycle(ops: list[AttioOp]) -> EmitMeetingLifecycleEvent:
    matches = [o for o in ops if isinstance(o, EmitMeetingLifecycleEvent)]
    assert len(matches) == 1, (
        f"expected exactly 1 EmitMeetingLifecycleEvent in plan; got "
        f"{len(matches)} in {[type(o).__name__ for o in ops]}"
    )
    return matches[0]


def _assert_host_upsert_present(ops: list[AttioOp], host_email: str) -> None:
    """Every lifecycle plan must include UpsertCompany + UpsertPerson for the host."""
    company_ops = [o for o in ops if isinstance(o, UpsertCompany)]
    person_ops = [o for o in ops if isinstance(o, UpsertPerson)]
    assert any(o.domain == host_email.split("@")[-1] for o in company_ops), (
        f"expected UpsertCompany for domain of {host_email} in plan; got "
        f"domains {[o.domain for o in company_ops]}"
    )
    assert any(o.email == host_email for o in person_ops), (
        f"expected UpsertPerson for {host_email} in plan; got "
        f"emails {[o.email for o in person_ops]}"
    )


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
            "uid": "redacted_no_show_booking_uid_01",
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
            # MEETING_STARTED and PING remain typed no-ops.
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
    def test_created_emits_host_upsert_meeting_and_lifecycle(self) -> None:
        w = _load("api/samples/caldotcom.booking.created.redacted.json")
        ops = w.attio_get_operations()

        _assert_host_upsert_present(ops, "host@dlthub.com")

        meeting_ops = [o for o in ops if isinstance(o, UpsertMeeting)]
        assert len(meeting_ops) == 1
        expected_ical = canonical_meeting_uid(
            host_email="host@dlthub.com",
            start=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
        )
        assert meeting_ops[0].external_ref.ical_uid == expected_ical

        lifecycle = _find_lifecycle(ops)
        assert lifecycle.event_subtype == "scheduled"
        # external_id == the Meeting's ical_uid: the cross-reference.
        assert lifecycle.external_id == expected_ical
        # The "scheduled" details line names the host and attendees.
        assert "host@dlthub.com" in lifecycle.details_line
        assert "external@example.com" in lifecycle.details_line

    def test_cancelled_emits_one_lifecycle_per_meeting(self) -> None:
        """Per-meeting model: one EmitMeetingLifecycleEvent regardless of attendee count."""
        w = _load("api/samples/caldotcom.booking.cancelled.redacted.json")
        ops = w.attio_get_operations()

        _assert_host_upsert_present(ops, "alex@example.com")
        # No UpsertMeeting on the cancelled path.
        assert not any(isinstance(o, UpsertMeeting) for o in ops)

        lifecycle = _find_lifecycle(ops)
        assert lifecycle.event_subtype == "cancelled"
        # external_id derived from organizer.email + startTime (== OLD time
        # per cal.com semantics, which is what addresses the existing row).
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=datetime(2026, 5, 6, 10, 30, 0, tzinfo=UTC),
        )
        assert lifecycle.external_id == expected
        # Cancellation context surfaces in both body (raw JSON) and details (summary).
        body = json.loads(lifecycle.body_json)
        assert body["cancellationReason"] == "redacted"
        assert body["cancelledBy"] == "alex@example.com"
        assert "alex@example.com" in lifecycle.details_line
        assert "redacted" in lifecycle.details_line

    def test_rescheduled_emits_lifecycle_with_old_and_new_times_in_details(
        self,
    ) -> None:
        w = _load("api/samples/caldotcom.booking.rescheduled.redacted.json")
        ops = w.attio_get_operations()

        _assert_host_upsert_present(ops, "alex@example.com")
        # No UpsertMeeting on reschedule — the existing Meeting record at the
        # OLD ical_uid stays put.
        assert not any(isinstance(o, UpsertMeeting) for o in ops)

        lifecycle = _find_lifecycle(ops)
        assert lifecycle.event_subtype == "rescheduled"
        # external_id pinned to OLD start so this row patches the
        # already-existing tracking_events row (scheduled-state row).
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC),
        )
        assert lifecycle.external_id == expected
        # Old + new start times appear in details_line.
        assert "2026-05-14T10:00:00" in lifecycle.details_line
        assert "2026-05-14T11:00:00" in lifecycle.details_line

    def test_cancelled_resolves_organizerless_payload_via_user_email(self) -> None:
        """Hostless CANCELLED falls back through user/userPrimaryEmail."""
        envelope = orjson.loads(
            (
                _REPO_ROOT / "api/samples/caldotcom.booking.cancelled.redacted.json"
            ).read_bytes(),
        )
        envelope["payload"].pop("organizer", None)
        envelope["payload"]["userPrimaryEmail"] = "alex@example.com"
        w = Webhook.model_validate(envelope)
        assert w.attio_is_valid_webhook() is True

        ops = w.attio_get_operations()
        _assert_host_upsert_present(ops, "alex@example.com")
        lifecycle = _find_lifecycle(ops)
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=datetime(2026, 5, 6, 10, 30, 0, tzinfo=UTC),
        )
        assert lifecycle.external_id == expected

    def test_rescheduled_does_not_emit_upsert_meeting(self) -> None:
        """Critical: reschedule must NOT create a duplicate at the new ical_uid."""
        w = _load("api/samples/caldotcom.booking.rescheduled.redacted.json")
        ops = w.attio_get_operations()
        assert not any(isinstance(o, UpsertMeeting) for o in ops)

    def test_no_show_fetches_booking_then_emits_one_lifecycle(
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

        _assert_host_upsert_present(ops, "alex@example.com")
        lifecycle = _find_lifecycle(ops)
        assert lifecycle.event_subtype == "no_show_attendee"
        # external_id resolved from the fetched booking (alex@example.com + start).
        expected = canonical_meeting_uid(
            host_email="alex@example.com",
            start=fake_booking.start,
        )
        assert lifecycle.external_id == expected
        # No-show attendee email appears in details_line.
        assert "sam@example.com" in lifecycle.details_line

    def test_no_show_with_transient_api_failure_propagates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient Cal.com failures (network/5xx) must propagate so Hookdeck
        retries the webhook."""

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
        with pytest.raises(RuntimeError, match="calcom unreachable"):
            w.attio_get_operations()

    def test_no_show_with_deleted_booking_emits_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """404 (booking deleted) → no host email + no start time → can't compute
        external_id, so skip the whole lifecycle path. Better than writing a
        divergent row that no future webhook for the same meeting will patch."""

        class _NotFoundClient:
            def get_booking(self, _uid: str) -> BookingCreatedPayload | None:
                return None

            def __enter__(self) -> _NotFoundClient:
                return self

            def __exit__(self, *_: Any) -> None:
                return None

        monkeypatch.setattr(
            Webhook,
            "_calcom_client",
            lambda self: _NotFoundClient(),  # noqa: ARG005  # pyright: ignore[reportUnknownLambdaType]
        )
        w = _load("api/samples/caldotcom.booking.no_show_updated.redacted.json")
        ops = w.attio_get_operations()
        assert ops == []

    def test_meeting_ended_without_no_show_emits_completed(self) -> None:
        w = _load("api/samples/caldotcom.meeting.ended.redacted.json")
        ops = w.attio_get_operations()

        _assert_host_upsert_present(ops, "alex@example.com")
        lifecycle = _find_lifecycle(ops)
        # noShowHost=False on this fixture, so event_subtype=completed.
        assert lifecycle.event_subtype == "completed"
        # rating/feedback summary lives in details_line; raw values in body.
        body = json.loads(lifecycle.body_json)
        assert "rating" in body

    def test_meeting_started_emits_nothing(self) -> None:
        w = _load("api/samples/caldotcom.meeting.started.redacted.json")
        assert w.attio_get_operations() == []

    def test_ping_emits_nothing(self) -> None:
        w = _load("api/samples/caldotcom.ping.redacted.json")
        assert w.attio_get_operations() == []


class TestCrossTriggerInvariant:
    """The audit story only works if CREATED and a later CANCELLED for the same
    logical meeting (same host email + start) produce the SAME
    ``external_id`` on the lifecycle event — that's how the cancelled-state
    webhook PATCHes the row the scheduled-state webhook created.
    """

    def test_created_and_cancelled_share_external_id_when_host_and_start_match(
        self,
    ) -> None:
        host = "alex@example.com"
        start = datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC)

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

        created_ops = Webhook.model_validate(created_envelope).attio_get_operations()
        cancelled_ops = Webhook.model_validate(
            cancelled_envelope,
        ).attio_get_operations()

        created_lifecycle = _find_lifecycle(created_ops)
        cancelled_lifecycle = _find_lifecycle(cancelled_ops)
        assert created_lifecycle.external_id == cancelled_lifecycle.external_id


def test_real_v2_created_produces_full_attio_plan() -> None:
    """Regression for the live BOOKING_CREATED 422: the real cal.com v2 payload
    (startTime/organizer/eventTitle, attendees without displayEmail/absent) must
    parse and produce the full Attio plan, not just for Slack. Before the model
    fix this raised a Pydantic ValidationError (missing ``start``)."""
    wh = _load("api/samples/caldotcom.booking.created.v2.redacted.json")
    assert wh.attio_is_valid_webhook(), wh.attio_get_invalid_webhook_error_msg()
    ops = wh.attio_get_operations()
    kinds = [type(o).__name__ for o in ops]
    assert "UpsertMeeting" in kinds
    _assert_host_upsert_present(ops, "attendee@example.com")
    _find_lifecycle(ops)


def test_real_v2_requested_is_valid_attio_noop() -> None:
    """BOOKING_REQUESTED parses and is a valid Attio webhook but writes nothing
    (the Attio meeting is created on confirmation, BOOKING_CREATED)."""
    wh = _load("api/samples/caldotcom.booking.requested.v2.redacted.json")
    assert wh.attio_is_valid_webhook()
    assert wh.attio_get_operations() == []
