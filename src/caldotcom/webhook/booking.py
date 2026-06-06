"""Webhook ETL contract for Cal.com booking ingestion.

Cal.com ships 7 trigger types across 4 payload shapes. The
``libs.caldotcom.models`` discriminated union parses each into a typed payload
variant; this module dispatches on the variant and emits the right Attio op.

Attio constraints (probed 2026-05-25 — see plan-02): ``/v2/meetings/`` is
append-only. Meeting state changes (cancel / reschedule / no-show / rating)
have nowhere to land on the Meeting record itself.

Per the spec at
``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``
each webhook produces:

1. ``UpsertCompany`` for the host's email domain.
2. ``UpsertPerson`` for the host (matching by email; the libs adapter links
   to the company auto-created in step 1).
3. ``UpsertMeeting`` (only on ``BOOKING_CREATED`` — the Meeting record itself
   is still created once and never mutated).
4. ``EmitMeetingLifecycleEvent`` — ONE per meeting (NOT per attendee). The
   dispatcher's ``LookupTable`` resolves the ``host`` PersonRef from step 2,
   and a single ``tracking_events`` row per meeting is PATCHed in place as
   the meeting transitions through states (scheduled → cancelled, etc.). The
   row's ``details`` field accrues a one-line history of every transition;
   the ``body`` field always holds the latest raw webhook payload.

This is a deliberate departure from plan-02, which wrote one row per
(meeting × attendee) linked to each attendee. The new per-meeting model uses
fewer rows, makes status filterable as a typed field, and is what the user
wants the Attio UI to surface. Attendee identity is preserved in
``body`` (raw payload) and ``details`` (e.g. "attendees: alice, bob").
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
    PersonRef,
    UpsertCompany,
    UpsertMeeting,
    UpsertPerson,
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


# --- Per-trigger op builders ---
#
# Cal.com semantics confirmed 2026-05-25:
#     BookingRescheduledPayload.startTime = OLD pre-reschedule time.
#     BookingRescheduledPayload.rescheduleStartTime = NEW post-reschedule time.
# Both CANCELLED and RESCHEDULED therefore use ``startTime`` to compute the
# row's external_id (the OLD ical_uid that matches the row the scheduled-state
# webhook created).


def _host_upsert_ops(host_email: str) -> list[AttioOp]:
    """Emit UpsertCompany + UpsertPerson for the meeting host.

    These run BEFORE EmitMeetingLifecycleEvent in the plan so the dispatcher's
    LookupTable can resolve the lifecycle event's ``host`` PersonRef. Both ops
    are idempotent — they no-op when the records already exist.

    Returns ``[]`` when ``host_email`` is empty so the caller can skip the
    whole lifecycle path cleanly.
    """
    if not host_email:
        return []
    domain = host_email.split("@")[-1] if "@" in host_email else ""
    ops: list[AttioOp] = []
    if domain:
        ops.append(UpsertCompany(domain=domain))
    ops.append(
        UpsertPerson(
            matching_attribute="email",
            email=host_email,
            company_domain=domain or None,
        ),
    )
    return ops


def _host_person_ref(host_email: str) -> PersonRef:
    return PersonRef(attribute="email", value=host_email)


def _details_line(timestamp: datetime, event_subtype: str, summary: str) -> str:
    """Single-line transition entry for the cumulative ``details`` field.

    Format: ``"<ISO-Z timestamp> <event_subtype> — <summary>"``. ``summary`` is
    variant-specific (see per-_ops_for_* helpers). Stable string format so the
    Attio UI shows a readable history.
    """
    iso = timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"{iso} {event_subtype} — {summary}"


def _ops_for_created(
    payload: BookingCreatedPayload,
    created_at: datetime,
) -> list[AttioOp]:
    """Emit UpsertCompany + UpsertPerson (host) + UpsertMeeting + lifecycle event.

    ``BOOKING_CREATED`` is the only variant that still emits ``UpsertMeeting`` —
    the Attio Meeting record is created once on first arrival and never
    mutated (Attio's /v2/meetings/ is append-only). The new lifecycle row
    sits alongside it.
    """
    host_email = payload.creator_email()
    if host_email is None:
        # Gate should have caught this; defensive fallback to avoid silent failure.
        return []

    ical_uid = canonical_meeting_uid(host_email=host_email, start=payload.start)
    title = payload.title or "CALCOM Booking"
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

    attendee_summary = ", ".join(_attendee_emails(payload.attendees)) or "(none)"
    summary = f"host: {host_email}; attendees: {attendee_summary}"

    return [
        *_host_upsert_ops(host_email),
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
        EmitMeetingLifecycleEvent(
            external_id=ical_uid,
            meeting_title=title,
            event_subtype="scheduled",
            timestamp=created_at,
            body_json=json.dumps(
                payload.model_dump(mode="json"),
                default=str,
                sort_keys=True,
            ),
            details_line=_details_line(created_at, "scheduled", summary),
            host=_host_person_ref(host_email),
        ),
    ]


def _attendee_emails(
    attendees: list[BookingAttendee] | list[MutationAttendee] | list[NoShowAttendee],
) -> list[str]:
    return [a.email for a in attendees if getattr(a, "email", None)]


def _payload_title(payload: Any) -> str:
    """Pull a cal.com title off any variant; fall back to a generic label.

    The various payload shapes carry ``title`` on most paths; the no-show slim
    payload doesn't, and the meeting-ended flat shape also lacks it. The
    fallback is fine — the row's `name` slug still scans cleanly with
    "<event_subtype> Cal.com meeting".
    """
    return getattr(payload, "title", None) or "Cal.com meeting"


def _ops_for_cancelled(
    payload: BookingCancelledPayload,
    created_at: datetime,
) -> list[AttioOp]:
    host_email = payload.creator_email()
    if host_email is None:
        return []
    ical_uid = canonical_meeting_uid(host_email=host_email, start=payload.startTime)
    summary = f"by {payload.cancelledBy}: {payload.cancellationReason}"
    return [
        *_host_upsert_ops(host_email),
        EmitMeetingLifecycleEvent(
            external_id=ical_uid,
            meeting_title=_payload_title(payload),
            event_subtype="cancelled",
            timestamp=created_at,
            body_json=json.dumps(
                payload.model_dump(mode="json"),
                default=str,
                sort_keys=True,
            ),
            details_line=_details_line(created_at, "cancelled", summary),
            host=_host_person_ref(host_email),
        ),
    ]


def _ops_for_rescheduled(
    payload: BookingRescheduledPayload,
    created_at: datetime,
) -> list[AttioOp]:
    """Emit lifecycle event; do NOT re-upsert the meeting.

    Re-POSTing with the new start would create a duplicate Attio Meeting
    record at ``canonical_meeting_uid(host, rescheduleStartTime)``. Since Attio
    has no PATCH on meetings, the only honest behavior is to leave the original
    record at its old time and capture the reschedule in the lifecycle row.
    The row's ``external_id`` is keyed off the OLD start so the same row that
    captured the scheduled-state is now patched with the rescheduled-state.
    """
    host_email = payload.creator_email()
    if host_email is None:
        return []
    ical_uid = canonical_meeting_uid(host_email=host_email, start=payload.startTime)
    new_start = (
        payload.rescheduleStartTime.isoformat()
        if payload.rescheduleStartTime is not None
        else "?"
    )
    summary = (
        f"old start {payload.startTime.isoformat()}; "
        f"new start {new_start}; by {payload.rescheduledBy}"
    )
    return [
        *_host_upsert_ops(host_email),
        EmitMeetingLifecycleEvent(
            external_id=ical_uid,
            meeting_title=_payload_title(payload),
            event_subtype="rescheduled",
            timestamp=created_at,
            body_json=json.dumps(
                payload.model_dump(mode="json"),
                default=str,
                sort_keys=True,
            ),
            details_line=_details_line(created_at, "rescheduled", summary),
            host=_host_person_ref(host_email),
        ),
    ]


def _ops_for_no_show(
    payload: BookingNoShowPayload,
    created_at: datetime,
    client: CalcomClient,
) -> list[AttioOp]:
    """Fetch the underlying booking to learn host email + start, then emit.

    The no-show webhook payload only carries ``bookingUid`` + ``attendees[email,
    noShow]``; insufficient for ``canonical_meeting_uid``.

    Failure modes:
      * 404 (booking deleted) → ``get_booking`` returns ``None``. We can't
        compute the row's ``external_id`` without host + start, so we skip
        emission entirely. Loss is preferable to writing a divergent row that
        no future webhook for the same meeting will patch.
      * 5xx / network / parse error → exception propagates so Hookdeck retries
        the webhook on a transient outage.
    """
    no_show_emails = [a.email for a in payload.attendees if a.noShow]
    if not no_show_emails:
        return []

    booking = client.get_booking(payload.bookingUid)
    if booking is None:
        # 404'd. Skip — see docstring.
        return []
    host_email = booking.creator_email()
    if host_email is None:
        return []

    ical_uid = canonical_meeting_uid(host_email=host_email, start=booking.start)
    summary = f"attendees marked no-show: {', '.join(no_show_emails)}"
    return [
        *_host_upsert_ops(host_email),
        EmitMeetingLifecycleEvent(
            external_id=ical_uid,
            meeting_title=_payload_title(booking),
            event_subtype="no_show_attendee",
            timestamp=created_at,
            body_json=json.dumps(
                payload.model_dump(mode="json"),
                default=str,
                sort_keys=True,
            ),
            details_line=_details_line(created_at, "no_show_attendee", summary),
            host=_host_person_ref(host_email),
        ),
    ]


def _ops_for_meeting_ended(
    payload: MeetingEndedPayload,
    created_at: datetime,
) -> list[AttioOp]:
    """Emit ``no_show_host`` when ``noShowHost`` else ``completed`` (with rating)."""
    host_email = payload.userPrimaryEmail
    if not host_email:
        return []
    ical_uid = canonical_meeting_uid(host_email=host_email, start=payload.startTime)
    if payload.noShowHost:
        event_subtype: Literal["no_show_host", "completed"] = "no_show_host"
        summary = "host did not attend"
    else:
        event_subtype = "completed"
        rating = payload.rating if payload.rating is not None else "?"
        feedback = payload.ratingFeedback or "(no feedback)"
        summary = f"rating {rating}: {feedback}"
    return [
        *_host_upsert_ops(host_email),
        EmitMeetingLifecycleEvent(
            external_id=ical_uid,
            meeting_title=_payload_title(payload),
            event_subtype=event_subtype,
            timestamp=created_at,
            body_json=json.dumps(
                payload.model_dump(mode="json"),
                default=str,
                sort_keys=True,
            ),
            details_line=_details_line(created_at, event_subtype, summary),
            host=_host_person_ref(host_email),
        ),
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
        return ["devx-gcp-202605260000"]

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
    def required_api_keys() -> list[str]:
        # CALCOM_API_KEY is NOT declared here — only the BOOKING_NO_SHOW_UPDATED
        # branch touches Cal.com's API. Declaring it would force the other
        # Cal.com event types (BOOKING_CREATED/CANCELLED/RESCHEDULED/MEETING_*)
        # to fail when CALCOM_API_KEY is missing or rotated, even though
        # they never reach `_calcom_client()`. ``_calcom_client()`` fetches
        # the key lazily inside the NO_SHOW branch. See ``optional_api_keys``
        # below for the deploy-time preflight that still guards it.
        return ["ATTIO_API_KEY"]

    @staticmethod
    def optional_api_keys() -> list[str]:
        # Declared here, not in ``required_api_keys``, so the deploy-time
        # preflight catches a missing/rotated CALCOM_API_KEY while the
        # non-NO_SHOW branches stay decoupled from Cal.com key health at
        # request time. ``_calcom_client()`` still fetches lazily inside
        # the BOOKING_NO_SHOW_UPDATED branch.
        #
        # Slack keys (SLACK_BOT_TOKEN + the per-source channel key from
        # slack_get_channel_secret_name()) are deliberately NOT listed here:
        # this method is shared by every handler's preflight (Attio/GCS/Slack),
        # so adding them would gate the Attio/GCS deploys on Slack secrets they
        # never touch. The Slack deploy gets its own preflight, scoped to
        # ``export_to_slack``, in ``scripts/webhooks-redeploy.py``.
        return ["CALCOM_API_KEY"]

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-calcom-bookings"

    def attio_is_valid_webhook(self) -> bool:
        return _validation_result(self.payload)[0]

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return _validation_result(self.payload)[1]

    def _calcom_client(self) -> CalcomClient:
        """Build a Cal.com API client. Lazy on CALCOM_API_KEY.

        Reached only on the BOOKING_NO_SHOW_UPDATED path (see
        ``attio_get_operations``), so the Cal.com key fetch happens only
        when the API actually needs to be called. Keeps the other Cal.com
        event types (BOOKING_CREATED/CANCELLED/RESCHEDULED/MEETING_*)
        unaffected by Cal.com key health — they don't reach this method.

        Override-friendly for tests: replace ``_calcom_client`` on a
        subclass to skip the network entirely.
        """
        from libs import infisical

        with infisical.fetch("CALCOM_API_KEY") as api_key:
            return CalcomClient(api_key=api_key)

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

    # --- Slack export contract ---

    @staticmethod
    def slack_get_app_name() -> str:
        return "export-to-slack-from-calcom-bookings"

    @staticmethod
    def slack_get_channel_secret_name() -> str:
        # Per-automation channel: the Slack handler fetches THIS Infisical key
        # for the target channel id, so each source posts to its own channel
        # (a future source declares its own key, e.g. FATHOM_SLACK_CHANNEL_ID).
        return "CALCOM_SLACK_CHANNEL_ID"

    def slack_is_valid_webhook(self) -> bool:
        # Same gate as Attio: PING / MEETING_STARTED are non-actionable.
        return _validation_result(self.payload)[0]

    def slack_get_invalid_webhook_error_msg(self) -> str:
        return _validation_result(self.payload)[1]

    def slack_get_messages(self) -> list[Any]:
        """Build Slack Block Kit messages for this booking lifecycle event.

        Each event threads under the booking's BOOKING_CREATED message; urgent
        events (cancel/no-show) broadcast back to the channel. The NO_SHOW path
        opens a Cal.com client lazily — see ``_calcom_client``.
        """
        from src.caldotcom.webhook.slack_export import messages_for_payload

        return messages_for_payload(
            self.payload,
            calcom_client_factory=self._calcom_client,
        )


# Keep the legacy import path working for callers that still reference
# ``BookingAttendee`` / ``BookingHost`` directly. They're re-exported from
# ``libs.caldotcom`` already; this is here for explicitness in static analysis.
__all__ = ["BookingAttendee", "BookingHost", "Webhook"]
