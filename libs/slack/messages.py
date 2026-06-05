"""Idiomatic ``chat.postMessage`` wrapper around ``slack_sdk.WebClient``.

No orchestration here — just the single API call and the bits of its response
callers care about (the message ``ts``, which doubles as a thread anchor).
Threading/broadcast *policy* lives in ``src/slack/`` per the repo's
libs-vs-src split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PostedMessage:
    """Subset of a ``chat.postMessage`` response used downstream."""

    channel: str
    ts: str


def post_message(
    client: Any,
    *,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    reply_broadcast: bool = False,
) -> PostedMessage:
    """Post a message and return its channel + ``ts``.

    ``text`` is always sent as the notification/fallback string (Slack uses it
    for push notifications and accessibility even when ``blocks`` render). When
    ``thread_ts`` is set the message is a threaded reply; ``reply_broadcast``
    additionally surfaces that reply in the main channel — used for urgent
    lifecycle events (cancellations, no-shows).
    """
    response = client.chat_postMessage(
        channel=channel,
        text=text,
        blocks=blocks,
        thread_ts=thread_ts,
        reply_broadcast=reply_broadcast if thread_ts else False,
    )
    return PostedMessage(channel=response["channel"], ts=response["ts"])
