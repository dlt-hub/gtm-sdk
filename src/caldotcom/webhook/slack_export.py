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

import os
from datetime import datetime
from typing import Any

from libs.caldotcom import (
    BookingCancelledPayload,
    BookingCreatedPayload,
    BookingNoShowPayload,
    BookingRequestedPayload,
    BookingRescheduledPayload,
    MeetingEndedPayload,
)
from libs.meetings import canonical_meeting_uid
from src.slack.ops import SlackMessage

# cal.com booking-detail page base. Defaults to cal.com cloud; override via env
# for a self-hosted instance or org domain. The deployed Modal container only
# sees this if it's injected (bootstrap secret / Modal env) — the default is
# correct for cal.com cloud, so no config is needed there.
CALCOM_APP_BASE_URL: str = os.environ.get(
    "CALCOM_APP_BASE_URL",
    "https://app.cal.com",
).rstrip("/")


def _booking_url(uid: str | None) -> str | None:
    """cal.com bookings deeplink for a booking uid, or None if absent.

    Uses the ``/bookings/?uid=`` list route rather than ``/booking/<uid>``: cal.com
    redirects it to the correct tab (upcoming / unconfirmed / past / cancelled)
    based on the booking's current state, so one link works for every lifecycle
    event (a requested booking lands on 'unconfirmed', a cancelled one on
    'cancelled', etc.)."""
    return f"{CALCOM_APP_BASE_URL}/bookings/?uid={uid}" if uid else None


# Emoji per lifecycle subtype — surfaces the event at a glance in the thread.
_EMOJI: dict[str, str] = {
    "requested": ":hourglass_flowing_sand:",
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


def _attendees_with_tz(attendees: list[Any]) -> str:
    """Render attendees as ``email (timezone)`` so the on-call host sees the
    attendee's local time zone at a glance. Falls back to just the email when
    cal.com omits the time zone."""
    parts: list[str] = []
    for a in attendees:
        email = getattr(a, "email", None)
        if not email:
            continue
        tz = getattr(a, "timeZone", None)
        parts.append(f"{email} ({tz})" if tz else email)
    return ", ".join(parts) or "(none)"


def _blocks(
    *,
    subtype: str,
    title: str,
    fields: list[tuple[str, str]],
    booking_url: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(fallback_text, blocks)`` for a lifecycle event.

    ``fallback_text`` is the Slack notification/accessibility string; the blocks
    render a header + a two-column field grid, plus a "View in Cal.com" link
    button when ``booking_url`` is set (a plain URL button — no interactivity
    backend needed).
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
    if booking_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View in Cal.com"},
                        "url": booking_url,
                    },
                ],
            },
        )
    return fallback, blocks


def _msg_for_booking(
    payload: BookingCreatedPayload,
    host_email: str,
    *,
    subtype: str,
) -> SlackMessage:
    """Shared builder for BOOKING_CREATED ('scheduled') and BOOKING_REQUESTED
    ('requested') — identical payload shape, only the lifecycle label differs."""
    title = payload.title or "Cal.com Booking"
    fallback, blocks = _blocks(
        subtype=subtype,
        title=title,
        fields=[
            ("Host", host_email),
            ("When", f"{_fmt_time(payload.start)} → {_fmt_time(payload.end)}"),
            ("Attendees", _attendees_with_tz(payload.attendees)),
        ],
        booking_url=_booking_url(payload.uid),
    )
    return SlackMessage(
        thread_key=canonical_meeting_uid(host_email=host_email, start=payload.start),
        text=fallback,
        blocks=blocks,
        urgent=False,
        event_subtype=subtype,
        mention_email=host_email,
    )


def _msg_for_created(payload: BookingCreatedPayload, host_email: str) -> SlackMessage:
    return _msg_for_booking(payload, host_email, subtype="scheduled")


def _msg_for_requested(payload: BookingCreatedPayload, host_email: str) -> SlackMessage:
    # BOOKING_REQUESTED shares BookingCreatedPayload's shape (subclass), so the
    # same builder works; the thread_key (canonical uid off start) matches the
    # eventual BOOKING_CREATED, so the confirmation threads under the request.
    return _msg_for_booking(payload, host_email, subtype="requested")


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
        booking_url=_booking_url(payload.uid),
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
        mention_email=host_email,
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
        booking_url=_booking_url(payload.uid),
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
        mention_email=host_email,
    )


def _msg_for_no_show(
    host_email: str,
    start: datetime,
    no_show_emails: list[str],
    booking_uid: str | None,
) -> SlackMessage:
    fallback, blocks = _blocks(
        subtype="no_show_attendee",
        title="Cal.com Booking",
        fields=[
            ("Host", host_email),
            ("No-show attendees", ", ".join(no_show_emails) or "(none)"),
        ],
        booking_url=_booking_url(booking_uid),
    )
    return SlackMessage(
        thread_key=canonical_meeting_uid(host_email=host_email, start=start),
        text=fallback,
        blocks=blocks,
        urgent=True,
        event_subtype="no_show_attendee",
        mention_email=host_email,
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
        booking_url=_booking_url(payload.uid),
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
        mention_email=host_email,
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
    # BookingRequestedPayload subclasses BookingCreatedPayload, so it MUST be
    # checked first or a requested booking would render as 'scheduled'.
    if isinstance(payload, BookingRequestedPayload):
        host = payload.creator_email()
        return [_msg_for_requested(payload, host)] if host else []
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
        return [
            _msg_for_no_show(host, booking.start, no_show_emails, payload.bookingUid),
        ]
    if isinstance(payload, MeetingEndedPayload):
        host = payload.userPrimaryEmail
        return [_msg_for_meeting_ended(payload, host)] if host else []
    # MEETING_STARTED / PING are gated out by validation.
    return []
