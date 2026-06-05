"""Source-agnostic Slack message operations.

A source webhook's ``slack_get_messages()`` returns ``list[SlackMessage]``;
``src.slack.export.execute`` turns each into a ``chat.postMessage`` call,
applying threading + urgent-broadcast policy. Mirrors the ``src.attio.ops`` /
``src.attio.export`` split so adding a new source requires no change to the
dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SlackMessage:
    """One Slack post in a per-booking thread.

    - ``thread_key`` groups every lifecycle event for one booking into a single
      Slack thread. The first message for a key starts the thread (a top-level
      channel post); later messages for the same key reply inside it.
    - ``text`` is the fallback/notification string (required by Slack even when
      ``blocks`` render).
    - ``blocks`` is the Block Kit payload.
    - ``urgent`` broadcasts a threaded reply back into the channel so it isn't
      missed (cancellations, no-shows). Ignored for the thread-opening message,
      which is already a channel post.
    - ``event_subtype`` is carried for logging/observability only.
    """

    thread_key: str
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    urgent: bool = False
    event_subtype: str = ""
