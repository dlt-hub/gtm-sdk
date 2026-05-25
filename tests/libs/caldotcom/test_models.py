"""Discriminated-union parsing tests against the 7 sample Cal.com payloads.

Each ``api/samples/caldotcom.*.redacted.json`` is a real recorded shape (with
PII stripped). These tests assert that the envelope unwraps the flat /
wrapped variants correctly and dispatches to the right payload subclass.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from libs.caldotcom import (
    BookingCancelledPayload,
    BookingCreatedPayload,
    BookingNoShowPayload,
    BookingRescheduledPayload,
    MeetingEndedPayload,
    MeetingStartedPayload,
    PingPayload,
    Webhook,
)

SAMPLES_DIR = Path("api/samples")


def _load(name: str) -> Webhook:
    path = SAMPLES_DIR / name
    return Webhook.model_validate(orjson.loads(path.read_bytes()))


# fmt: off
_CASES: list[tuple[str, str, type]] = [
    ("caldotcom.booking.created.redacted.json",         "BOOKING_CREATED",          BookingCreatedPayload),
    ("caldotcom.booking.cancelled.redacted.json",       "BOOKING_CANCELLED",        BookingCancelledPayload),
    ("caldotcom.booking.rescheduled.redacted.json",     "BOOKING_RESCHEDULED",      BookingRescheduledPayload),
    ("caldotcom.booking.no_show_updated.redacted.json", "BOOKING_NO_SHOW_UPDATED",  BookingNoShowPayload),
    ("caldotcom.meeting.started.redacted.json",         "MEETING_STARTED",          MeetingStartedPayload),
    ("caldotcom.meeting.ended.redacted.json",           "MEETING_ENDED",            MeetingEndedPayload),
    ("caldotcom.ping.redacted.json",                    "PING",                     PingPayload),
]
# fmt: on


@pytest.mark.parametrize(("sample", "trigger", "expected_cls"), _CASES)
def test_sample_dispatches_to_expected_variant(
    sample: str,
    trigger: str,
    expected_cls: type,
) -> None:
    w = _load(sample)
    assert w.triggerEvent == trigger
    assert type(w.payload) is expected_cls


def test_created_payload_carries_hosts_and_attendees() -> None:
    w = _load("caldotcom.booking.created.redacted.json")
    p = w.payload
    assert isinstance(p, BookingCreatedPayload)
    assert p.uid == "calcom-booking-abc123"
    assert [h.email for h in p.hosts] == ["host@dlthub.com"]
    assert [a.email for a in p.attendees] == ["external@example.com"]
    assert p.icsUid == "ical-evt-abc123@cal.com"


def test_cancelled_payload_carries_organizer_and_reason() -> None:
    w = _load("caldotcom.booking.cancelled.redacted.json")
    p = w.payload
    assert isinstance(p, BookingCancelledPayload)
    assert p.uid == "7NTwtb1h8SnDMGJbGWNVXg"
    assert p.organizer.email == "alex@example.com"
    assert p.cancellationReason == "redacted"
    assert p.cancelledBy == "alex@example.com"
    assert p.iCalUID == "7NTwtb1h8SnDMGJbGWNVXg@Cal.com"
    assert [a.email for a in p.attendees] == ["sam@example.com"]


def test_rescheduled_payload_has_both_old_and_new_times() -> None:
    """Confirmed semantics: ``startTime`` = OLD, ``rescheduleStartTime`` = NEW."""
    w = _load("caldotcom.booking.rescheduled.redacted.json")
    p = w.payload
    assert isinstance(p, BookingRescheduledPayload)
    # startTime is the OLD pre-reschedule time (2026-05-14T10:00:00Z).
    assert p.startTime.isoformat().startswith("2026-05-14T10:00:00")
    # rescheduleStartTime is the NEW post-reschedule time (2026-05-14T11:00:00Z).
    assert p.rescheduleStartTime is not None
    assert p.rescheduleStartTime.isoformat().startswith("2026-05-14T11:00:00")
    assert p.organizer.email == "alex@example.com"
    assert p.rescheduledBy == "sam@example.com"


def test_no_show_payload_is_slim() -> None:
    w = _load("caldotcom.booking.no_show_updated.redacted.json")
    p = w.payload
    assert isinstance(p, BookingNoShowPayload)
    assert p.bookingUid == "wjvt1yoCAGK6KmaLnmszng"
    assert (
        p.attendees
        == [
            # NoShowAttendee model; pydantic equality through .model_dump
        ]
        or [a.model_dump() for a in p.attendees]
        == [
            {"email": "sam@example.com", "noShow": True},
        ]
    )


def test_meeting_ended_flat_shape_lifts_into_payload() -> None:
    """MEETING_ENDED ships fields flat at the envelope; validator normalizes."""
    w = _load("caldotcom.meeting.ended.redacted.json")
    p = w.payload
    assert isinstance(p, MeetingEndedPayload)
    assert p.uid == "vR6cS6aLNKNqRRcKEciviX"
    assert p.userPrimaryEmail == "alex@example.com"
    assert p.noShowHost is False
    assert p.rating is None
    assert [a.email for a in p.attendees] == ["sam@example.com", "jamie@example.com"]
    assert p.iCalUID == "7HyZhszqGYkZhwF8mYbuJG@Cal.com"


def test_meeting_started_flat_shape_lifts_into_payload() -> None:
    w = _load("caldotcom.meeting.started.redacted.json")
    p = w.payload
    assert isinstance(p, MeetingStartedPayload)
    assert p.userPrimaryEmail == "alex@example.com"


def test_ping_accepts_any_shape() -> None:
    w = _load("caldotcom.ping.redacted.json")
    assert isinstance(w.payload, PingPayload)


def test_hookdeck_wrapped_body_unwraps() -> None:
    """Hookdeck delivers as ``{"body": "<json string>"}``; validator unwraps."""
    inner = orjson.loads(
        (SAMPLES_DIR / "caldotcom.booking.created.redacted.json").read_bytes(),
    )
    wrapped = {"body": orjson.dumps(inner).decode("utf-8")}
    w = Webhook.model_validate(wrapped)
    assert isinstance(w.payload, BookingCreatedPayload)


def test_discriminator_rejects_unknown_trigger() -> None:
    """Unknown triggerEvent should fail the union; safety net for new Cal.com triggers."""
    with pytest.raises(ValidationError):
        Webhook.model_validate(
            {
                "triggerEvent": "BOOKING_PAYMENT_INITIATED",
                "createdAt": "2026-05-25T00:00:00Z",
                "payload": {"uid": "x"},
            },
        )
