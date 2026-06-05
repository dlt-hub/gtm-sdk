"""Cal.com booking lifecycle -> Slack Block Kit messages.

Mirrors the Attio op-builder structure in ``booking.py`` but renders Slack
messages instead. Each lifecycle event's ``thread_key`` is the canonical
meeting uid for its ``(host, start)``: BOOKING_CREATED opens the thread and
CANCELLED / RESCHEDULED reply under it because all three key off the booking's
*original* start (CANCELLED/RESCHEDULED carry the old start under
``startTime``).

Caveat — terminal events key off the *current* start: ``MEETING_ENDED`` uses
``payload.startTime`` and the NO_SHOW path uses the fetched ``booking.start``.
For a booking that was rescheduled, that start differs from the original, so
those events will land in their own thread rather than re-joining the
BOOKING_CREATED message. This matches the Attio ``external_id`` behavior and is
accepted; do not assume every event for a booking shares one thread.

Urgent events (cancellations, attendee/host no-shows) set ``urgent=True`` so the
dispatcher broadcasts the threaded reply back into the channel.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from libs.caldotcom import (
    BookingCancelledPayload,
    BookingCreatedPayload,
    BookingNoShowPayload,
    BookingRescheduledPayload,
    MeetingEndedPayload,
)
from libs.meetings import canonical_meeting_uid
from src.slack.ops import SlackMessage

# Emoji per lifecycle subtype — surfaces the event at a glance in the thread.
_EMOJI: dict[str, str] = {
    "scheduled": ":calendar:",
    "rescheduled": ":arrows_counterclockwise:",
    "cancelled": ":x:",
    "no_show_attendee": ":ghost:",
    "no_show_host": ":warning:",
    "completed": ":white_check_mark:",
}


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _attendee_emails(attendees: list[Any]) -> list[str]:
    return [a.email for a in attendees if getattr(a, "email", None)]


def _blocks(
    *,
    subtype: str,
    title: str,
    fields: list[tuple[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(fallback_text, blocks)`` for a lifecycle event.

    ``fallback_text`` is the Slack notification/accessibility string; the blocks
    render a header + a two-column field grid.
    """
    emoji = _EMOJI.get(subtype, ":bell:")
    heading = f"{emoji} {subtype.replace('_', ' ').title()}: {title}"
    # Cap each field's contribution AND the final string so the fallback
    # notification text can't blow past Slack's message size limit on an extreme
    # cancellationReason / ratingFeedback (the section fields are capped below).
    fallback = (heading + " — " + "; ".join(f"{k}: {v[:500]}" for k, v in fields))[
        :3000
    ]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": heading[:150]}},
    ]
    if fields:
        # Slack rejects the whole post if any mrkdwn section field exceeds 2000
        # chars; a long cancellationReason / ratingFeedback would otherwise make
        # chat.postMessage fail. Cap each value (header is already capped above).
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{k}*\n{v}"[:2000]} for k, v in fields
                ],
            },
        )
    return fallback, blocks


def _msg_for_created(payload: BookingCreatedPayload, host_email: str) -> SlackMessage:
    title = payload.title or "Cal.com Booking"
    attendees = ", ".join(_attendee_emails(payload.attendees)) or "(none)"
    fallback, blocks = _blocks(
        subtype="scheduled",
        title=title,
        fields=[
            ("Host", host_email),
            ("When", f"{_fmt_time(payload.start)} → {_fmt_time(payload.end)}"),
            ("Attendees", attendees),
        ],
    )
    return SlackMessage(
        thread_key=canonical_meeting_uid(host_email=host_email, start=payload.start),
        text=fallback,
        blocks=blocks,
        urgent=False,
        event_subtype="scheduled",
    )


def _msg_for_cancelled(
    payload: BookingCancelledPayload,
    host_email: str,
) -> SlackMessage:
    fallback, blocks = _blocks(
        subtype="cancelled",
        title=payload.title or "Cal.com Booking",
        fields=[
            ("Host", host_email),
            ("Cancelled by", payload.cancelledBy or "?"),
            ("Reason", payload.cancellationReason or "(none given)"),
        ],
    )
    return SlackMessage(
        thread_key=canonical_meeting_uid(
            host_email=host_email,
            start=payload.startTime,
        ),
        text=fallback,
        blocks=blocks,
        urgent=True,
        event_subtype="cancelled",
    )


def _msg_for_rescheduled(
    payload: BookingRescheduledPayload,
    host_email: str,
) -> SlackMessage:
    fallback, blocks = _blocks(
        subtype="rescheduled",
        title=payload.title or "Cal.com Booking",
        fields=[
            ("Host", host_email),
            ("Old start", _fmt_time(payload.startTime)),
            ("New start", _fmt_time(payload.rescheduleStartTime)),
            ("By", payload.rescheduledBy or "?"),
        ],
    )
    return SlackMessage(
        # Keyed off the OLD start so it threads under the original booking.
        thread_key=canonical_meeting_uid(
            host_email=host_email,
            start=payload.startTime,
        ),
        text=fallback,
        blocks=blocks,
        urgent=False,
        event_subtype="rescheduled",
    )


def _msg_for_no_show(
    host_email: str,
    start: datetime,
    no_show_emails: list[str],
) -> SlackMessage:
    fallback, blocks = _blocks(
        subtype="no_show_attendee",
        title="Cal.com Booking",
        fields=[
            ("Host", host_email),
            ("No-show attendees", ", ".join(no_show_emails) or "(none)"),
        ],
    )
    return SlackMessage(
        thread_key=canonical_meeting_uid(host_email=host_email, start=start),
        text=fallback,
        blocks=blocks,
        urgent=True,
        event_subtype="no_show_attendee",
    )


def _msg_for_meeting_ended(
    payload: MeetingEndedPayload,
    host_email: str,
) -> SlackMessage:
    if payload.noShowHost:
        subtype = "no_show_host"
        fields = [("Host", host_email), ("Detail", "Host did not attend")]
        urgent = True
    else:
        subtype = "completed"
        rating = payload.rating if payload.rating is not None else "?"
        fields = [
            ("Host", host_email),
            ("Rating", str(rating)),
            ("Feedback", payload.ratingFeedback or "(none)"),
        ]
        urgent = False
    fallback, blocks = _blocks(
        subtype=subtype,
        title=payload.title or "Cal.com Booking",
        fields=fields,
    )
    return SlackMessage(
        thread_key=canonical_meeting_uid(
            host_email=host_email,
            start=payload.startTime,
        ),
        text=fallback,
        blocks=blocks,
        urgent=urgent,
        event_subtype=subtype,
    )


def messages_for_payload(
    payload: Any,
    *,
    calcom_client_factory: Any,
) -> list[SlackMessage]:
    """Dispatch one parsed Cal.com payload to a list of Slack messages.

    ``calcom_client_factory`` is a zero-arg callable returning a context-manager
    :class:`CalcomClient`; only the NO_SHOW path opens it (the slim payload must
    fetch the underlying booking for host email + start). Mirrors
    ``Webhook._calcom_client`` so tests can inject a fake.
    """
    if isinstance(payload, BookingCreatedPayload):
        host = payload.creator_email()
        return [_msg_for_created(payload, host)] if host else []
    if isinstance(payload, BookingCancelledPayload):
        host = payload.creator_email()
        return [_msg_for_cancelled(payload, host)] if host else []
    if isinstance(payload, BookingRescheduledPayload):
        host = payload.creator_email()
        return [_msg_for_rescheduled(payload, host)] if host else []
    if isinstance(payload, BookingNoShowPayload):
        no_show_emails = [a.email for a in payload.attendees if a.noShow and a.email]
        if not no_show_emails:
            return []
        with calcom_client_factory() as client:
            booking = client.get_booking(payload.bookingUid)
        if booking is None:
            return []
        host = booking.creator_email()
        if not host:
            return []
        return [_msg_for_no_show(host, booking.start, no_show_emails)]
    if isinstance(payload, MeetingEndedPayload):
        host = payload.userPrimaryEmail
        return [_msg_for_meeting_ended(payload, host)] if host else []
    # MEETING_STARTED / PING are gated out by validation.
    return []
