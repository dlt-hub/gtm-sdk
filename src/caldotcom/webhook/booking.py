"""Webhook ETL contract for Cal.com booking ingestion.

Cal.com ships 7 trigger types across 4 payload shapes. The
``libs.caldotcom.models`` discriminated union parses each into a typed payload
variant; this module dispatches on the variant and emits the right Attio op.

Attio constraint (probed 2026-05-25 — see plan-02): the ``/v2/meetings/``
resource is append-only. PATCH / PUT / DELETE all return 404. Only
``BOOKING_CREATED`` results in a meeting record write (``UpsertMeeting``); all
state-change triggers (CANCELLED / RESCHEDULED / NO_SHOW / MEETING_ENDED) emit
``EmitMeetingLifecycleEvent`` audit rows on ``tracking_events`` linked to each
attendee's Person record.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from uuid_extensions import uuid7

from libs.caldotcom import (
    BookingCancelledPayload,
    BookingCreatedPayload,
    BookingNoShowPayload,
    BookingRescheduledPayload,
    MeetingEndedPayload,
    MeetingStartedPayload,
    PingPayload,
    Webhook as CalcomWebhook,
)
from libs.caldotcom.client import CalcomClient
from libs.caldotcom.models import (
    BookingAttendee,
    BookingHost,
    MutationAttendee,
    NoShowAttendee,
)
from libs.dlt.bucket_naming import etl_bucket_name, raw_bucket_name
from libs.meetings import canonical_meeting_uid
from src.attio.ops import (
    AttioOp,
    EmitMeetingLifecycleEvent,
    MeetingExternalRef,
    MeetingParticipant,
    UpsertMeeting,
)
from src.caldotcom.utils import (
    generate_gcs_filename,
    webhook_to_jsonl,
)

# Cal.com Booking has a single top-level RSVP status that applies to the
# booking as a whole, plus a per-attendee `absent` boolean. Map to Attio's
# four-value enum: accepted | tentative | declined | pending.
_CALCOM_BOOKING_STATUS_TO_ATTIO: dict[
    str,
    Literal["accepted", "tentative", "declined", "pending"],
] = {
    "accepted": "accepted",
    "pending": "pending",
    "cancelled": "declined",
    "rejected": "declined",
}


def _caldotcom_status_to_attio(
    booking_status: str | None,
    *,
    absent: bool = False,
) -> Literal["accepted", "tentative", "declined", "pending"]:
    if absent:
        return "declined"
    if booking_status is None:
        return "accepted"
    normalized = booking_status.lower()
    return _CALCOM_BOOKING_STATUS_TO_ATTIO.get(normalized, "accepted")


# --- ical_uid derivation ---


def _ical_uid_for_old_state(
    payload: (
        BookingCreatedPayload
        | BookingCancelledPayload
        | BookingRescheduledPayload
        | MeetingStartedPayload
        | MeetingEndedPayload
    ),
) -> str | None:
    """Return the canonical_meeting_uid of the EXISTING Attio meeting record.

    For CREATED that's the new meeting we're about to upsert. For mutation
    triggers it's the record the original booking was upserted as. Returns
    None when the payload lacks the fields needed (host email + start time).

    Cal.com semantics confirmed 2026-05-25:
        BookingRescheduledPayload.startTime = OLD pre-reschedule time.
        BookingRescheduledPayload.rescheduleStartTime = NEW post-reschedule time.
    So both CANCELLED and RESCHEDULED can use ``startTime`` to address the
    existing record.
    """
    if isinstance(payload, BookingCreatedPayload):
        host_email = payload.creator_email()
        if host_email is None:
            return None
        return canonical_meeting_uid(host_email=host_email, start=payload.start)

    if isinstance(payload, (BookingCancelledPayload, BookingRescheduledPayload)):
        host_email = payload.creator_email()
        if host_email is None:
            return None
        return canonical_meeting_uid(
            host_email=host_email,
            start=payload.startTime,
        )

    # MEETING_STARTED / MEETING_ENDED carry the host email as userPrimaryEmail.
    return canonical_meeting_uid(
        host_email=payload.userPrimaryEmail,
        start=payload.startTime,
    )


# --- Per-trigger op builders ---


def _ops_for_created(
    payload: BookingCreatedPayload,
    _created_at: datetime,
) -> list[AttioOp]:
    """Find-or-create the Attio Meeting record. Unchanged semantics."""
    host_email = payload.creator_email()
    if host_email is None:
        # Gate should have caught this; defensive fallback to avoid silent failure.
        return []

    ical_uid = canonical_meeting_uid(host_email=host_email, start=payload.start)
    title = payload.title or "Cal.com booking"
    description = payload.additionalNotes or payload.description or title
    booking_status = payload.status

    participants: list[MeetingParticipant] = []
    for a in payload.attendees:
        participants.append(
            MeetingParticipant(
                email_address=a.email,
                is_organizer=False,
                status=_caldotcom_status_to_attio(
                    booking_status,
                    absent=bool(a.absent),
                ),
            ),
        )
    if payload.hosts:
        for h in payload.hosts:
            participants.append(
                MeetingParticipant(
                    email_address=h.email,
                    is_organizer=True,
                    status="accepted",
                ),
            )
    else:
        # Host-less BOOKING_CREATED (older webhook versions, certain team
        # configs): fall back to the same chain ``creator_email`` walked so the
        # Attio Meeting still has a single organizer participant. Use the
        # mapped booking status here (not hard-coded ``accepted``) so a
        # cancelled/rejected booking with no ``hosts[]`` doesn't show the
        # organizer as accepted in Attio.
        participants.append(
            MeetingParticipant(
                email_address=host_email,
                is_organizer=True,
                status=_caldotcom_status_to_attio(booking_status),
            ),
        )

    return [
        UpsertMeeting(
            external_ref=MeetingExternalRef(
                ical_uid=ical_uid,
                provider="google",
                is_recurring=False,
            ),
            title=title,
            description=description,
            start=payload.start,
            end=payload.end,
            is_all_day=False,
            participants=participants,
        ),
    ]


def _attendee_emails(
    attendees: list[BookingAttendee] | list[MutationAttendee] | list[NoShowAttendee],
) -> list[str]:
    return [a.email for a in attendees if getattr(a, "email", None)]


def _lifecycle_event(
    *,
    event_type: Literal[
        "meeting_cancelled",
        "meeting_rescheduled",
        "meeting_no_show",
        "meeting_no_show_host",
        "meeting_ended",
    ],
    name: str,
    timestamp: datetime,
    booking_uid: str,
    attendee_email: str,
    ical_uid: str | None,
    body: dict[str, Any],
) -> EmitMeetingLifecycleEvent:
    # Include ical_uid in the body for cross-reference even though Attio has
    # no foreign key to link the tracking_events row to the Meeting record.
    body_with_uid = {"meeting_ical_uid": ical_uid, **body}
    return EmitMeetingLifecycleEvent(
        external_id=f"caldotcom:{event_type}:{booking_uid}:{attendee_email}",
        event_type=event_type,
        name=name,
        timestamp=timestamp,
        body_json=json.dumps(body_with_uid, default=str, sort_keys=True),
        attendee_email=attendee_email,
        meeting_ical_uid=ical_uid,
    )


def _ops_for_cancelled(
    payload: BookingCancelledPayload,
    created_at: datetime,
) -> list[AttioOp]:
    ical_uid = _ical_uid_for_old_state(payload)
    body = {
        "reason": payload.cancellationReason,
        "cancelled_by": payload.cancelledBy,
        "original_start": payload.startTime.isoformat(),
        "original_end": payload.endTime.isoformat(),
        "calcom_ical_uid": payload.iCalUID,
        "organizer_email": payload.creator_email(),
    }
    return [
        _lifecycle_event(
            event_type="meeting_cancelled",
            name="CALCOM Booking cancelled",
            timestamp=created_at,
            booking_uid=payload.uid,
            attendee_email=email,
            ical_uid=ical_uid,
            body=body,
        )
        for email in _attendee_emails(payload.attendees)
    ]


def _ops_for_rescheduled(
    payload: BookingRescheduledPayload,
    created_at: datetime,
) -> list[AttioOp]:
    """Emit lifecycle events; do NOT re-upsert the meeting.

    Re-POSTing with the new start would produce a duplicate Attio Meeting
    record at ``canonical_meeting_uid(host, rescheduleStartTime)``. Since Attio
    has no PATCH on meetings, the only honest behavior is to leave the original
    record at its old time and capture the reschedule in the audit log.
    """
    ical_uid = _ical_uid_for_old_state(payload)
    body = {
        "old_start": payload.startTime.isoformat(),
        "old_end": payload.endTime.isoformat(),
        "new_start": (
            payload.rescheduleStartTime.isoformat()
            if payload.rescheduleStartTime is not None
            else None
        ),
        "new_end": (
            payload.rescheduleEndTime.isoformat()
            if payload.rescheduleEndTime is not None
            else None
        ),
        "rescheduled_by": payload.rescheduledBy,
        "reschedule_uid": payload.rescheduleUid,
        "calcom_ical_uid": payload.iCalUID,
        "organizer_email": payload.creator_email(),
    }
    return [
        _lifecycle_event(
            event_type="meeting_rescheduled",
            name="CALCOM Booking rescheduled",
            timestamp=created_at,
            booking_uid=payload.uid,
            attendee_email=email,
            ical_uid=ical_uid,
            body=body,
        )
        for email in _attendee_emails(payload.attendees)
    ]


def _ops_for_no_show(
    payload: BookingNoShowPayload,
    created_at: datetime,
    client: CalcomClient,
) -> list[AttioOp]:
    """Fetch the underlying booking to learn host email + start, then emit.

    The webhook payload only carries ``bookingUid`` + ``attendees[email,
    noShow]``; insufficient for canonical_meeting_uid.

    Failure modes:
      * 404 (booking deleted) → ``get_booking`` returns ``None``. Emit a
        partial lifecycle row anyway — there's no way to "retry to success"
        for a deleted booking, and the audit value (someone marked an
        attendee no-show) is what we care about.
      * 5xx / network / parse error → let the exception propagate so the
        webhook returns a failure and Hookdeck retries. Better to retry a
        transient outage than to write a partial row that can never be
        re-linked to its meeting.
    """
    no_show_emails = [a.email for a in payload.attendees if a.noShow]
    if not no_show_emails:
        return []

    # Errors propagate by design — see docstring. The only non-error "miss" is
    # a 404, which ``get_booking`` returns as ``None``.
    booking = client.get_booking(payload.bookingUid)

    if booking is not None:
        ical_uid = _ical_uid_for_old_state(booking)
        # Use the same fallback chain as BOOKING_CREATED so meetings booked
        # without ``hosts[]`` still resolve to the organizer's email in the
        # audit row body.
        organizer_email = booking.creator_email()
        start_iso = booking.start.isoformat()
    else:
        ical_uid = None
        organizer_email = None
        start_iso = None

    return [
        _lifecycle_event(
            event_type="meeting_no_show",
            name="CALCOM Booking attendee no-show",
            timestamp=created_at,
            booking_uid=payload.bookingUid,
            attendee_email=email,
            ical_uid=ical_uid,
            body={
                "original_start": start_iso,
                "organizer_email": organizer_email,
                "booking_lookup_succeeded": booking is not None,
            },
        )
        for email in no_show_emails
    ]


def _ops_for_meeting_ended(
    payload: MeetingEndedPayload,
    created_at: datetime,
) -> list[AttioOp]:
    """Emit ``meeting_no_show_host`` when ``noShowHost`` else ``meeting_ended``."""
    ical_uid = _ical_uid_for_old_state(payload)
    event_type: Literal["meeting_no_show_host", "meeting_ended"] = (
        "meeting_no_show_host" if payload.noShowHost else "meeting_ended"
    )
    name = (
        "CALCOM Booking ended — host no-show"
        if payload.noShowHost
        else "CALCOM Booking ended"
    )
    body = {
        "noShowHost": payload.noShowHost,
        "rating": payload.rating,
        "ratingFeedback": payload.ratingFeedback,
        "original_start": payload.startTime.isoformat(),
        "organizer_email": payload.userPrimaryEmail,
    }
    return [
        _lifecycle_event(
            event_type=event_type,
            name=name,
            timestamp=created_at,
            booking_uid=payload.uid,
            attendee_email=email,
            ical_uid=ical_uid,
            body=body,
        )
        for email in _attendee_emails(payload.attendees)
    ]


# --- Per-trigger validation ---


def _validation_result(payload: Any) -> tuple[bool, str]:
    """Per-variant gate check. Returns ``(is_valid, error_message)``."""
    if isinstance(payload, BookingCreatedPayload):
        ok = (
            bool(payload.uid)
            and bool(payload.attendees)
            and bool(payload.creator_email())
        )
        return ok, (
            ""
            if ok
            else "BOOKING_CREATED missing uid/attendees or no host email "
            "(hosts/organizer/user/userPrimaryEmail)"
        )
    if isinstance(payload, (BookingCancelledPayload, BookingRescheduledPayload)):
        ok = (
            bool(payload.uid)
            and bool(payload.attendees)
            and bool(payload.creator_email())
        )
        return ok, (
            ""
            if ok
            else f"{type(payload).__name__} missing uid/attendees or no host "
            "email (organizer/user/userPrimaryEmail)"
        )
    if isinstance(payload, BookingNoShowPayload):
        ok = bool(payload.bookingUid) and any(a.noShow for a in payload.attendees)
        return ok, (
            ""
            if ok
            else "BOOKING_NO_SHOW_UPDATED missing bookingUid or no attendee with noShow=true"
        )
    if isinstance(payload, MeetingEndedPayload):
        ok = bool(payload.userPrimaryEmail) and bool(payload.attendees)
        return ok, ("" if ok else "MEETING_ENDED missing userPrimaryEmail/attendees")
    if isinstance(payload, MeetingStartedPayload):
        return False, "MEETING_STARTED is not Attio-actionable in this iteration"
    if isinstance(payload, PingPayload):
        return False, "PING is a connectivity check, not an Attio-actionable event"
    return False, f"unknown Cal.com payload variant: {type(payload).__name__}"


class Webhook(CalcomWebhook):
    """Webhook subclass implementing ETL contract for Cal.com bookings."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-gcp-202605111323"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return etl_bucket_name(source="calcom", entity_plural="bookings")

    @staticmethod
    def raw_get_bucket_name() -> str:
        return raw_bucket_name(source="calcom", entity_plural="bookings")

    @staticmethod
    def raw_get_app_name() -> str:
        from libs.dlt.filesystem_gcp import CloudGoogle

        return CloudGoogle.clean_bucket_name(bucket_name=Webhook.raw_get_bucket_name())

    # Raw passthrough has no per-source invariants — it lands the JSON body
    # untouched. Trivial implementations satisfy the symmetric-triple contract
    # the registry sync expects without inventing fake validity rules.
    def raw_is_valid_webhook(self) -> bool:
        return True

    def raw_get_invalid_webhook_error_msg(self) -> str:
        return "raw passthrough accepts any payload; should not be reachable"

    @staticmethod
    def storage_get_app_name() -> str:
        return Webhook.etl_get_bucket_name()

    @staticmethod
    def storage_get_base_model_type() -> type[BaseModel] | None:
        return None

    @staticmethod
    def lance_get_project_name() -> str:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    @staticmethod
    def lance_get_base_model_type() -> str:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    def etl_is_valid_webhook(self) -> bool:
        return True

    def etl_get_invalid_webhook_error_msg(self) -> str:
        return "This webhook family does not support ETL output"

    def _booking_id(self) -> str:
        # Variants store the booking identifier under different fields. Resolve
        # in priority order: uid (CREATED/CANCELLED/RESCHEDULED/MEETING_*),
        # bookingUid (NO_SHOW), fallback uuid7 (PING).
        cached = getattr(self, "_cached_booking_id", None)
        if cached is not None:
            return cached
        payload = self.payload
        uid: Any = getattr(payload, "uid", None) or getattr(payload, "bookingUid", None)
        if not uid:
            uid = uuid7()
        booking_id = str(uid)
        object.__setattr__(self, "_cached_booking_id", booking_id)
        return booking_id

    def etl_get_json(self, storage: Any = None) -> str:
        return webhook_to_jsonl(self.model_dump(mode="json"), self._booking_id())

    def etl_get_file_name(self) -> str:
        return generate_gcs_filename(
            self.createdAt,
            self.triggerEvent,
            self._booking_id(),
        )

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    # --- Attio export contract ---

    @staticmethod
    def attio_get_secret_collection_names() -> list[str]:
        # ``caldotcom`` carries CALCOM_API_KEY used by BOOKING_NO_SHOW_UPDATED.
        # Must be created in Modal (dlthub workspace) — see plan-02 deploy notes.
        return ["attio", "caldotcom"]

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-calcom-bookings"

    def attio_is_valid_webhook(self) -> bool:
        return _validation_result(self.payload)[0]

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return _validation_result(self.payload)[1]

    def _calcom_client(self) -> CalcomClient:
        """Build a Cal.com API client from env. Override-friendly for tests."""
        return CalcomClient.from_env()

    def attio_get_operations(self) -> list[AttioOp]:
        payload = self.payload
        if isinstance(payload, BookingCreatedPayload):
            return _ops_for_created(payload, self.createdAt)
        if isinstance(payload, BookingCancelledPayload):
            return _ops_for_cancelled(payload, self.createdAt)
        if isinstance(payload, BookingRescheduledPayload):
            return _ops_for_rescheduled(payload, self.createdAt)
        if isinstance(payload, BookingNoShowPayload):
            with self._calcom_client() as client:
                return _ops_for_no_show(payload, self.createdAt, client)
        if isinstance(payload, MeetingEndedPayload):
            return _ops_for_meeting_ended(payload, self.createdAt)
        # MEETING_STARTED / PING are typed no-ops — validation gates them
        # before this point. The fall-through also catches any future variant
        # we add to the union but forget to wire here.
        return []


# Keep the legacy import path working for callers that still reference
# ``BookingAttendee`` / ``BookingHost`` directly. They're re-exported from
# ``libs.caldotcom`` already; this is here for explicitness in static analysis.
__all__ = ["BookingAttendee", "BookingHost", "Webhook"]
