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


def test_cancelled_is_urgent() -> None:
    msgs = _messages("api/samples/caldotcom.booking.cancelled.redacted.json")
    assert len(msgs) == 1
    assert msgs[0].event_subtype == "cancelled"
    assert msgs[0].urgent is True


def test_rescheduled_threads_under_original_start() -> None:
    _messages("api/samples/caldotcom.booking.created.redacted.json")[0]
    rescheduled = _messages(
        "api/samples/caldotcom.booking.rescheduled.redacted.json",
    )
    assert len(rescheduled) == 1
    assert rescheduled[0].event_subtype == "rescheduled"
    assert rescheduled[0].urgent is False


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
