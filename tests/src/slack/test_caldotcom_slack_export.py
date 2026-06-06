"""Cal.com -> Slack message building + dispatcher threading/broadcast.

Reuses the recorded Cal.com fixtures (``api/samples/caldotcom.*.json``) so the
Slack rendering is exercised against the same payloads as the Attio path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from src.caldotcom.webhook.booking import Webhook
from src.slack.export import execute
from src.slack.ops import SlackMessage
from src.slack.thread_store import InMemoryThreadStore

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]


def _load(fixture_path: str) -> Webhook:
    payload = orjson.loads((_REPO_ROOT / fixture_path).read_bytes())
    return Webhook.model_validate(payload)


def _messages(fixture_path: str) -> list[SlackMessage]:
    return _load(fixture_path).slack_get_messages()


# ---------- message building ----------


def test_created_produces_non_urgent_opening_message() -> None:
    msgs = _messages("api/samples/caldotcom.booking.created.redacted.json")
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.event_subtype == "scheduled"
    assert msg.urgent is False
    assert msg.thread_key  # canonical meeting uid
    # Block Kit header is always present; fallback text is non-empty.
    assert msg.blocks[0]["type"] == "header"
    assert msg.text


def test_created_includes_view_in_calcom_link_button() -> None:
    msgs = _messages("api/samples/caldotcom.booking.created.redacted.json")
    actions = next(
        (b for b in msgs[0].blocks if b["type"] == "actions"),
        None,
    )
    assert actions is not None, "expected an actions block with the deeplink button"
    button = actions["elements"][0]
    assert button["text"]["text"] == "View in Cal.com"
    assert button["url"].endswith("/booking/calcom-booking-abc123")
    assert button["url"].startswith("https://")


def test_real_v2_created_parses_and_renders_with_attendee_timezone() -> None:
    """Regression for the live 422: the real cal.com v2 BOOKING_CREATED payload
    uses startTime/organizer/eventTitle (not start/hosts), and attendees omit
    displayEmail/absent. It must parse and render a 'scheduled' card whose
    Attendees field includes the attendee's time zone."""
    msgs = _messages("api/samples/caldotcom.booking.created.v2.redacted.json")
    assert len(msgs) == 1
    assert msgs[0].event_subtype == "scheduled"
    section = next(b for b in msgs[0].blocks if b["type"] == "section")
    attendees_field = next(
        f["text"] for f in section["fields"] if f["text"].startswith("*Attendees*")
    )
    assert "America/Los_Angeles" in attendees_field
    assert any(b["type"] == "actions" for b in msgs[0].blocks)  # deeplink button


def test_booking_requested_renders_as_requested_and_threads_with_created() -> None:
    created = _messages("api/samples/caldotcom.booking.created.v2.redacted.json")[0]
    requested = _messages("api/samples/caldotcom.booking.requested.v2.redacted.json")
    assert len(requested) == 1
    assert requested[0].event_subtype == "requested"
    assert requested[0].urgent is False
    # Same booking (same host+start) → confirmation threads under the request.
    assert requested[0].thread_key == created.thread_key


def test_cancelled_is_urgent() -> None:
    msgs = _messages("api/samples/caldotcom.booking.cancelled.redacted.json")
    assert len(msgs) == 1
    assert msgs[0].event_subtype == "cancelled"
    assert msgs[0].urgent is True


def test_rescheduled_is_non_urgent() -> None:
    rescheduled = _messages(
        "api/samples/caldotcom.booking.rescheduled.redacted.json",
    )
    assert len(rescheduled) == 1
    assert rescheduled[0].event_subtype == "rescheduled"
    assert rescheduled[0].urgent is False


def test_lifecycle_events_for_one_booking_share_a_thread_key() -> None:
    """The headline threading property: a CREATED event and a later CANCELLED
    event for the *same* booking must collide on ``thread_key`` so the cancel
    threads under the original message. Both key off ``canonical_meeting_uid``
    of the same host + start, so they must match exactly. (The recorded
    fixtures are different bookings, hence this builds matching payloads.)"""
    from datetime import UTC, datetime

    from libs.caldotcom.models import (
        BookingCancelledPayload,
        BookingCreatedPayload,
        BookingRescheduledPayload,
    )
    from src.caldotcom.webhook.slack_export import messages_for_payload

    def _unused_client_factory() -> object:
        raise AssertionError("client factory must not be used for created/cancelled")

    host = "host@example.com"
    start = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
    created = BookingCreatedPayload.model_validate(
        {
            "triggerEvent": "BOOKING_CREATED",
            "uid": "bk-1",
            "start": start.isoformat(),
            "end": start.isoformat(),
            "organizer": {"email": host},
        },
    )
    cancelled = BookingCancelledPayload.model_validate(
        {
            "triggerEvent": "BOOKING_CANCELLED",
            "uid": "bk-1",
            # CANCELLED carries the OLD start under startTime (see booking.py).
            "startTime": start.isoformat(),
            "endTime": start.isoformat(),
            "organizer": {"email": host},
        },
    )

    # RESCHEDULED carries the OLD start under startTime (rescheduleStartTime is
    # the NEW start) — the most counterintuitive keying in the module — so it
    # too must collide with CREATED on thread_key.
    rescheduled = BookingRescheduledPayload.model_validate(
        {
            "triggerEvent": "BOOKING_RESCHEDULED",
            "uid": "bk-1",
            "startTime": start.isoformat(),
            "endTime": start.isoformat(),
            "rescheduleStartTime": datetime(2026, 3, 8, 15, 0, tzinfo=UTC).isoformat(),
            "organizer": {"email": host},
        },
    )

    created_msg = messages_for_payload(
        created,
        calcom_client_factory=_unused_client_factory,
    )[0]
    cancelled_msg = messages_for_payload(
        cancelled,
        calcom_client_factory=_unused_client_factory,
    )[0]
    rescheduled_msg = messages_for_payload(
        rescheduled,
        calcom_client_factory=_unused_client_factory,
    )[0]
    assert created_msg.thread_key == cancelled_msg.thread_key
    assert created_msg.thread_key == rescheduled_msg.thread_key


def test_no_show_fetches_booking_then_emits_urgent_message() -> None:
    from datetime import UTC, datetime

    from src.caldotcom.webhook.slack_export import messages_for_payload

    wh = _load("api/samples/caldotcom.booking.no_show_updated.redacted.json")

    class _StubBooking:
        start = datetime(2026, 1, 1, tzinfo=UTC)

        def creator_email(self) -> str:
            return "host@example.com"

    class _FakeFactory:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_booking(self, uid):
            return _StubBooking()

    msgs = messages_for_payload(wh.payload, calcom_client_factory=_FakeFactory)
    assert len(msgs) == 1
    assert msgs[0].event_subtype == "no_show_attendee"
    assert msgs[0].urgent is True


def test_meeting_ended_completed_is_non_urgent_with_rating() -> None:
    from datetime import UTC, datetime

    from libs.caldotcom.models import MeetingEndedPayload
    from src.caldotcom.webhook.slack_export import messages_for_payload

    payload = MeetingEndedPayload.model_validate(
        {
            "triggerEvent": "MEETING_ENDED",
            "uid": "bk-ended",
            "startTime": datetime(2026, 3, 1, 15, 0, tzinfo=UTC).isoformat(),
            "endTime": datetime(2026, 3, 1, 15, 30, tzinfo=UTC).isoformat(),
            "userPrimaryEmail": "host@example.com",
            "attendees": [{"email": "guest@acme.com"}],
            "rating": 5,
            "ratingFeedback": "great call",
            "noShowHost": False,
        },
    )
    msgs = messages_for_payload(payload, calcom_client_factory=None)
    assert len(msgs) == 1
    assert msgs[0].event_subtype == "completed"
    assert msgs[0].urgent is False
    assert "great call" in msgs[0].text


def test_meeting_ended_no_show_host_is_urgent() -> None:
    from datetime import UTC, datetime

    from libs.caldotcom.models import MeetingEndedPayload
    from src.caldotcom.webhook.slack_export import messages_for_payload

    payload = MeetingEndedPayload.model_validate(
        {
            "triggerEvent": "MEETING_ENDED",
            "uid": "bk-ended-noshow",
            "startTime": datetime(2026, 3, 1, 15, 0, tzinfo=UTC).isoformat(),
            "endTime": datetime(2026, 3, 1, 15, 30, tzinfo=UTC).isoformat(),
            "userPrimaryEmail": "host@example.com",
            "attendees": [{"email": "guest@acme.com"}],
            "noShowHost": True,
        },
    )
    msgs = messages_for_payload(payload, calcom_client_factory=None)
    assert len(msgs) == 1
    assert msgs[0].event_subtype == "no_show_host"
    assert msgs[0].urgent is True


def test_long_field_values_are_truncated_to_slack_limit() -> None:
    """A long cancellationReason must not exceed Slack's 2000-char per-field
    cap (which would make chat.postMessage reject the whole post)."""
    from datetime import UTC, datetime

    from libs.caldotcom.models import BookingCancelledPayload
    from src.caldotcom.webhook.slack_export import messages_for_payload

    def _unused() -> object:
        raise AssertionError("client factory must not be used for cancelled")

    host = "host@example.com"
    cancelled = BookingCancelledPayload.model_validate(
        {
            "triggerEvent": "BOOKING_CANCELLED",
            "uid": "bk-long",
            "startTime": datetime(2026, 3, 1, 15, 0, tzinfo=UTC).isoformat(),
            "endTime": datetime(2026, 3, 1, 15, 0, tzinfo=UTC).isoformat(),
            "organizer": {"email": host},
            "cancellationReason": "x" * 5000,
        },
    )
    msg = messages_for_payload(cancelled, calcom_client_factory=_unused)[0]
    section = next(b for b in msg.blocks if b["type"] == "section")
    assert all(len(f["text"]) <= 2000 for f in section["fields"])


def test_ping_and_meeting_started_produce_no_messages() -> None:
    assert _messages("api/samples/caldotcom.ping.redacted.json") == []
    assert _messages("api/samples/caldotcom.meeting.started.redacted.json") == []


# ---------- dispatcher threading / broadcast ----------


class _FakeSlackClient:
    """Records chat_postMessage calls; returns incrementing ts values."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    def chat_postMessage(self, **kwargs):  # noqa: N802 — matches slack_sdk
        self._n += 1
        self.calls.append(kwargs)
        return {"channel": kwargs["channel"], "ts": f"{self._n}.000"}


def test_first_event_opens_thread_then_later_events_reply() -> None:
    client = _FakeSlackClient()
    store = InMemoryThreadStore()
    channel = "C123"

    opening = SlackMessage(thread_key="bk1", text="created", event_subtype="scheduled")
    result = execute([opening], channel=channel, client=client, thread_store=store)
    assert result.outcomes[0].ok
    assert result.outcomes[0].threaded is False
    assert client.calls[0]["thread_ts"] is None

    # A later event for the same booking replies in-thread under the first ts.
    reply = SlackMessage(
        thread_key="bk1",
        text="cancelled",
        urgent=True,
        event_subtype="cancelled",
    )
    execute([reply], channel=channel, client=client, thread_store=store)
    assert client.calls[1]["thread_ts"] == "1.000"
    # Urgent reply broadcasts back to the channel.
    assert client.calls[1]["reply_broadcast"] is True


def test_opening_message_never_broadcasts_even_if_urgent() -> None:
    client = _FakeSlackClient()
    store = InMemoryThreadStore()
    # No prior thread anchor: an urgent event with no opener falls back to a
    # top-level post and must NOT set reply_broadcast (no thread to broadcast).
    msg = SlackMessage(
        thread_key="orphan",
        text="x",
        urgent=True,
        event_subtype="cancelled",
    )
    execute([msg], channel="C1", client=client, thread_store=store)
    assert client.calls[0]["thread_ts"] is None
    assert client.calls[0]["reply_broadcast"] is False


def test_out_of_order_fallback_becomes_thread_root() -> None:
    """Documented best-effort edge: if a non-opening event arrives with no
    stored anchor (opener undelivered / out-of-order), it posts top-level and
    becomes the thread root, so a later event threads under it."""
    client = _FakeSlackClient()
    store = InMemoryThreadStore()

    # Cancellation arrives first (no anchor yet) → top-level post, becomes root.
    early = SlackMessage(
        thread_key="bk9",
        text="cancelled-first",
        urgent=True,
        event_subtype="cancelled",
    )
    execute([early], channel="C1", client=client, thread_store=store)
    assert client.calls[0]["thread_ts"] is None
    # Urgent but opening → must not broadcast (no thread to broadcast into).
    assert client.calls[0]["reply_broadcast"] is False

    # The real BOOKING_CREATED arrives later → threads under the fallback root.
    late = SlackMessage(
        thread_key="bk9",
        text="created-late",
        event_subtype="scheduled",
    )
    execute([late], channel="C1", client=client, thread_store=store)
    assert client.calls[1]["thread_ts"] == "1.000"


def test_post_failure_is_recorded_and_does_not_abort_batch() -> None:
    class _Boom(_FakeSlackClient):
        def chat_postMessage(self, **kwargs):  # noqa: N802
            if kwargs["text"] == "boom":
                raise RuntimeError("slack down")
            return super().chat_postMessage(**kwargs)

    client = _Boom()
    store = InMemoryThreadStore()
    msgs = [
        SlackMessage(thread_key="a", text="boom", event_subtype="scheduled"),
        SlackMessage(thread_key="b", text="ok", event_subtype="scheduled"),
    ]
    result = execute(msgs, channel="C1", client=client, thread_store=store)
    assert result.outcomes[0].ok is False
    assert result.outcomes[0].error
    assert result.outcomes[1].ok is True
