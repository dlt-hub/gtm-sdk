"""Slack SDK client builder.

Wraps the official ``slack_sdk.WebClient``. Key resolution order for
``get_client()`` mirrors ``libs/attio/client.py``:

1. Explicit ``token`` argument (tests, one-off scripts).
2. The contextvar set by :func:`api_key_scope` — the webhook endpoint opens
   this scope after fetching the bot token from Infisical.
3. ``os.environ["SLACK_BOT_TOKEN"]`` — for any long-running process that binds
   a named Modal Secret.

If none resolve, raise :class:`SlackAuthError` naming all three paths.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slack_sdk import WebClient

# Whole seconds — slack_sdk's WebClient.timeout is typed int. Parsing as int
# (rather than float + int()) avoids silently truncating a sub-second override
# like "0.5" to 0, which slack_sdk treats as "no timeout". A non-integer value
# fails loudly here, which is the right behavior for a misconfiguration.
SLACK_OP_TIMEOUT_SECONDS: int = int(
    os.environ.get("SLACK_OP_TIMEOUT_SECONDS", "10"),
)

_api_key_var: ContextVar[str | None] = ContextVar(
    "slack_bot_token",
    default=None,
)


class SlackAuthError(RuntimeError):
    """Raised when no Slack bot token can be resolved."""


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Slack bot token for this context.

    Used by the webhook flow: the endpoint resolves the token via Infisical
    once per request, then opens this scope before posting. The scope is reset
    on exit so concurrent requests in the same Modal container
    (``@modal.concurrent``) do not see each other's tokens.
    """
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


def get_client(token: str | None = None) -> WebClient:
    """Build a ``slack_sdk.WebClient``. See module docstring for resolution."""
    resolved = (
        token or _api_key_var.get() or os.environ.get("SLACK_BOT_TOKEN", "")
    ).strip()
    if not resolved:
        raise SlackAuthError(
            "Slack bot token not resolved. Provide one of: "
            "(1) explicit token= argument, "
            "(2) call inside libs.slack.client.api_key_scope(...), "
            "(3) set SLACK_BOT_TOKEN in the process environment.",
        )
    # Local import keeps slack_sdk off the import path for callers that only
    # need api_key_scope (e.g. src.secrets_bootstrap wiring KEY_SCOPES).
    from slack_sdk import WebClient

    return WebClient(token=resolved, timeout=SLACK_OP_TIMEOUT_SECONDS)
