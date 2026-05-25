"""BOOKING_CREATED path tests.

The other 6 trigger types are covered in ``test_webhook_fixtures.py``. This
file focuses on the CREATED branch — participant mapping, RSVP status
translation, and the canonical ical_uid derivation that lets Fathom land on the
same Attio Meeting record.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import orjson

from libs.caldotcom.models import BookingCreatedPayload
from libs.meetings import canonical_meeting_uid
from src.attio.ops import MeetingExternalRef, UpsertMeeting
from src.caldotcom.webhook.booking import Webhook

FIXTURE = Path("api/samples/caldotcom.booking.created.redacted.json")


def _load() -> Webhook:
    payload = orjson.loads(FIXTURE.read_bytes())
    return Webhook.model_validate(payload)


def _mutated_created_webhook(**overrides: object) -> Webhook:
    """Build a CREATED webhook re-validating the payload with field overrides.

    The typed Pydantic payload (post plan-02) is immutable in the sense that
    ``w.payload["x"] = y`` no longer works. Rebuild the envelope dict, mutate
    the payload dict, then re-validate.
    """
    envelope = orjson.loads(FIXTURE.read_bytes())
    payload = envelope["payload"]
    for k, v in overrides.items():
        payload[k] = v
    return Webhook.model_validate(envelope)


def test_attio_get_secret_collection_names_includes_caldotcom() -> None:
    """Plan-02 added the 'caldotcom' secret for the BOOKING_NO_SHOW_UPDATED
    Cal.com API fetch. Both must be present so the Modal app injects both."""
    names = Webhook.attio_get_secret_collection_names()
    assert "attio" in names
    assert "caldotcom" in names


def test_attio_is_valid_webhook_true_for_normal_payload() -> None:
    assert _load().attio_is_valid_webhook() is True


def test_attio_is_valid_webhook_false_with_no_attendees() -> None:
    w = _mutated_created_webhook(attendees=[])
    assert w.attio_is_valid_webhook() is False
    assert "attendees" in w.attio_get_invalid_webhook_error_msg()


def test_attio_get_operations_returns_single_upsert_meeting() -> None:
    plan = _load().attio_get_operations()

    assert len(plan) == 1
    op = plan[0]
    assert isinstance(op, UpsertMeeting)
    assert isinstance(op.external_ref, MeetingExternalRef)
    expected = canonical_meeting_uid(
        host_email="host@dlthub.com",
        start=datetime.fromisoformat("2026-05-20T15:00:00+00:00"),
    )
    assert op.external_ref.ical_uid == expected

    assert op.title == "Discovery call"
    assert op.description == "Customer would like to discuss pricing."
    assert op.is_all_day is False

    emails = [p.email_address for p in op.participants]
    assert "host@dlthub.com" in emails
    assert "external@example.com" in emails

    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].email_address == "host@dlthub.com"

    external = next(
        p for p in op.participants if p.email_address == "external@example.com"
    )
    assert external.status == "accepted"


def test_attio_get_operations_ignores_icsUid_in_favor_of_canonical_uid() -> None:
    with_ics = _load().attio_get_operations()[0]
    without_ics = _mutated_created_webhook(icsUid=None).attio_get_operations()[0]
    assert isinstance(with_ics, UpsertMeeting)
    assert isinstance(without_ics, UpsertMeeting)
    assert with_ics.external_ref.ical_uid == without_ics.external_ref.ical_uid


def test_attio_is_valid_webhook_false_when_host_email_missing() -> None:
    """All host-email fallback sources must be empty for CREATED to fail.

    Regression guard for the broaden-the-gate work (ai-4u6): the previous
    handler had a four-way fallback chain ``hosts → organizer → user →
    userPrimaryEmail`` and real-world Cal.com payloads sometimes omit ``hosts``.
    """
    w = _mutated_created_webhook(
        hosts=[],
        organizer=None,
        user=None,
        userPrimaryEmail=None,
    )
    assert w.attio_is_valid_webhook() is False


def test_attio_creator_email_falls_back_to_organizer() -> None:
    w = _mutated_created_webhook(
        hosts=[],
        organizer={"email": "organizer@example.com"},
    )
    assert w.attio_is_valid_webhook() is True
    op = w.attio_get_operations()[0]
    assert isinstance(op, UpsertMeeting)
    assert op.external_ref.ical_uid == canonical_meeting_uid(
        host_email="organizer@example.com",
        start=datetime.fromisoformat("2026-05-20T15:00:00+00:00"),
    )


def test_unknown_booking_status_does_not_drop_payload() -> None:
    """Cal.com may add new status strings; permissive ``status: str | None``
    plus ``_caldotcom_status_to_attio`` keeps unknowns mapping to ``accepted``
    instead of failing the discriminator and silently dropping the event."""
    w = _mutated_created_webhook(status="rescheduled_pending_review")
    assert w.attio_is_valid_webhook() is True
    op = w.attio_get_operations()[0]
    assert isinstance(op, UpsertMeeting)
    # Attendee statuses fall back to ``accepted`` for unknown values.
    assert all(p.status == "accepted" for p in op.participants if not p.is_organizer)


def test_hostless_cancelled_organizer_mirrors_booking_status() -> None:
    """Hostless CREATED with status=cancelled should map the organizer
    participant to ``declined`` (matching the prior behavior), not the
    hard-coded ``accepted`` that the first fix shipped with."""
    w = _mutated_created_webhook(
        hosts=[],
        organizer={"email": "organizer@example.com"},
        status="cancelled",
    )
    op = w.attio_get_operations()[0]
    assert isinstance(op, UpsertMeeting)
    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].status == "declined"


def test_hostless_created_still_adds_organizer_participant() -> None:
    """Host-less BOOKING_CREATED must still emit an organizer participant.

    Previously, when ``hosts[]`` was empty we walked the fallback chain for
    ``ical_uid`` but emitted no organizer participant — so the Attio Meeting
    record was missing the host. Re-add the organizer using the same fallback
    chain ``creator_email`` uses.
    """
    w = _mutated_created_webhook(
        hosts=[],
        organizer={"email": "organizer@example.com"},
    )
    ops = w.attio_get_operations()
    assert len(ops) == 1
    op = ops[0]
    assert isinstance(op, UpsertMeeting)
    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].email_address == "organizer@example.com"


def test_attio_creator_email_falls_back_to_user_then_userPrimaryEmail() -> None:
    """``user.email`` outranks ``userPrimaryEmail``; both outranked by ``organizer``."""
    w_user = _mutated_created_webhook(
        hosts=[],
        organizer=None,
        user={"email": "user@example.com"},
    )
    assert w_user.attio_is_valid_webhook() is True

    w_primary = _mutated_created_webhook(
        hosts=[],
        organizer=None,
        user=None,
        userPrimaryEmail="primary@example.com",
    )
    assert w_primary.attio_is_valid_webhook() is True


def test_attio_is_valid_webhook_false_when_start_missing() -> None:
    """Pydantic-level validation: ``start`` is required on BOOKING_CREATED."""
    envelope = orjson.loads(FIXTURE.read_bytes())
    envelope["payload"].pop("start", None)
    # Discriminator should still find BOOKING_CREATED, but the missing required
    # ``start`` field raises a ValidationError before Webhook is constructed.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Webhook.model_validate(envelope)


def test_attio_get_operations_marks_absent_attendee_declined() -> None:
    envelope = orjson.loads(FIXTURE.read_bytes())
    envelope["payload"]["attendees"][0]["absent"] = True
    w = Webhook.model_validate(envelope)
    op = w.attio_get_operations()[0]
    assert isinstance(op, UpsertMeeting)
    external = next(
        p for p in op.participants if p.email_address == "external@example.com"
    )
    assert external.status == "declined"


def test_attio_get_operations_maps_cancelled_status_to_declined() -> None:
    """If the BOOKING_CREATED payload arrives with status=cancelled, attendees
    inherit declined. (Edge case — cancellation usually arrives as its own
    BOOKING_CANCELLED webhook, but the field is still on the CREATED model.)
    """
    w = _mutated_created_webhook(status="cancelled")
    op = w.attio_get_operations()[0]
    assert isinstance(op, UpsertMeeting)
    external = next(
        p for p in op.participants if p.email_address == "external@example.com"
    )
    assert external.status == "declined"


def test_payload_is_typed_not_dict() -> None:
    """Sanity: post plan-02, ``w.payload`` is a typed BaseModel, not a dict."""
    w = _load()
    assert isinstance(w.payload, BookingCreatedPayload)
