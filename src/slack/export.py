"""Source-agnostic Slack message dispatcher.

Source webhooks return ``list[SlackMessage]`` from ``slack_get_messages()``;
this module posts each to Slack, threading every message that shares a
``thread_key`` under the first one and broadcasting urgent replies back to the
channel. Imports only ``libs.slack.*`` and ``src.slack.*`` — adding a new
source webhook should require no change here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from libs.logging.structured import log
from libs.slack import post_message
from src.slack.ops import SlackMessage
from src.slack.thread_store import InMemoryThreadStore, ThreadStore


@dataclass
class MessageOutcome:
    thread_key: str
    event_subtype: str
    ts: str | None
    threaded: bool
    broadcast: bool
    ok: bool
    error: str | None = None


@dataclass
class ExecuteResult:
    outcomes: list[MessageOutcome] = field(default_factory=list)

    def body(self) -> str:
        """JSON summary returned to Hookdeck for observability."""
        return json.dumps(
            {
                "posted": sum(1 for o in self.outcomes if o.ok),
                "failed": sum(1 for o in self.outcomes if not o.ok),
                "outcomes": [vars(o) for o in self.outcomes],
            },
            default=str,
            sort_keys=True,
        )


def execute(
    messages: list[SlackMessage],
    *,
    channel: str,
    client: Any,
    thread_store: ThreadStore | None = None,
) -> ExecuteResult:
    """Post ``messages`` to ``channel``, threading by ``thread_key``.

    First message for a ``thread_key`` opens the thread (top-level post) and its
    ``ts`` is recorded in ``thread_store``. Subsequent messages for the same key
    reply in-thread; ``urgent`` replies also broadcast to the channel. When a
    later event arrives but no thread anchor exists (e.g. the opening event was
    never delivered), it falls back to a top-level post so the event is never
    silently dropped.

    Threading is best-effort, not guaranteed, and not idempotent:

    - Non-atomic race: the anchor lookup is a read-modify-write
      (``get`` -> post -> ``set``) and the Modal endpoint runs
      ``@modal.concurrent``. Two events for the same booking arriving close
      together can both see no anchor and each open a separate top-level
      thread. A put-if-absent on the store would be needed to elect a single
      opener deterministically.
    - Redelivery duplicates: there is no idempotency key on the message. A
      Hookdeck **redelivery** of the opening event re-posts it — as a threaded
      reply (the anchor now exists) duplicating the original; a redelivery of a
      later event re-posts that reply. Keying the store on
      ``(thread_key, event_subtype)`` or recording posted event ids would
      suppress this.

    Both are accepted edges for a best-effort notifier (lifecycle events for one
    booking are rarely simultaneous and Hookdeck redelivery is infrequent).
    """
    store = thread_store if thread_store is not None else InMemoryThreadStore()
    result = ExecuteResult()

    for msg in messages:
        anchor = store.get(msg.thread_key)
        is_opening = anchor is None
        try:
            posted = post_message(
                client,
                channel=channel,
                text=msg.text,
                blocks=msg.blocks or None,
                thread_ts=anchor,
                reply_broadcast=msg.urgent and not is_opening,
            )
        except Exception as exc:  # noqa: BLE001 — record per-message, keep going
            log(
                "slack.post_failed",
                thread_key=msg.thread_key,
                event_subtype=msg.event_subtype,
                error_type=type(exc).__name__,
                error_msg=str(exc),
            )
            result.outcomes.append(
                MessageOutcome(
                    thread_key=msg.thread_key,
                    event_subtype=msg.event_subtype,
                    ts=None,
                    threaded=not is_opening,
                    broadcast=msg.urgent and not is_opening,
                    ok=False,
                    error=str(exc),
                ),
            )
            continue

        if is_opening:
            # Anchor the thread on the first message's ts so later lifecycle
            # events reply under it.
            store.set(msg.thread_key, posted.ts)

        log(
            "slack.posted",
            thread_key=msg.thread_key,
            event_subtype=msg.event_subtype,
            threaded=not is_opening,
            broadcast=msg.urgent and not is_opening,
        )
        result.outcomes.append(
            MessageOutcome(
                thread_key=msg.thread_key,
                event_subtype=msg.event_subtype,
                ts=posted.ts,
                threaded=not is_opening,
                broadcast=msg.urgent and not is_opening,
                ok=True,
            ),
        )

    return result
