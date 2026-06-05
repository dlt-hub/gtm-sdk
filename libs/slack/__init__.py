"""Slack SDK adapter. Wraps ``slack_sdk`` with idiomatic Python types.

No cross-lib imports, no orchestration — see ``AGENTS.md`` code-placement
rules. Threading/broadcast policy lives in ``src/slack/``.
"""

from __future__ import annotations

from libs.slack.client import SlackAuthError, api_key_scope, get_client
from libs.slack.messages import PostedMessage, post_message

__all__ = [
    "PostedMessage",
    "SlackAuthError",
    "api_key_scope",
    "get_client",
    "post_message",
]
