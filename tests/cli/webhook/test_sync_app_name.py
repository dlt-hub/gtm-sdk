"""`app_name_for(export_to_slack, ...)` skips sources that don't support Slack.

Only caldotcom is wired into webhooks/export_to_slack.py; the other four
sources carry stub slack_* methods to satisfy the protocol. Without the
sentinel skip, `gtm webhook sync` would emit four phantom undeployed Slack apps
in the registry.
"""

from __future__ import annotations

import pytest

from cli.webhook.sync import app_name_for
from src.caldotcom.webhook.booking import Webhook as CaldotcomBookingWebhook
from src.fathom.webhook.call import Webhook as FathomCallWebhook
from src.fathom.webhook.message import Webhook as FathomMessageWebhook
from src.octolens.webhook.mention import Webhook as OctolensMentionWebhook
from src.rb2b.webhook.visit import Webhook as Rb2bVisitWebhook

_UNSUPPORTED = [
    pytest.param(FathomCallWebhook, id="fathom-call"),
    pytest.param(FathomMessageWebhook, id="fathom-message"),
    pytest.param(OctolensMentionWebhook, id="octolens-mention"),
    pytest.param(Rb2bVisitWebhook, id="rb2b-visit"),
]


def test_caldotcom_resolves_slack_app_name() -> None:
    assert (
        app_name_for("export_to_slack", CaldotcomBookingWebhook)
        == "export-to-slack-from-calcom-bookings"
    )


@pytest.mark.parametrize("model", _UNSUPPORTED)  # pyright: ignore[reportUntypedFunctionDecorator]
def test_unsupported_sources_skipped_for_slack(model: type) -> None:
    assert app_name_for("export_to_slack", model) is None
