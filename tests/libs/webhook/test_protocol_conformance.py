"""Verify each source's ``Webhook`` class satisfies ``WebhookModelProtocol``.

Catches missing contract methods at ``pytest`` time instead of at
``modal deploy`` time when the placeholder substitution lands in an image
build. Add a new source's import here before wiring it into the
``webhooks/`` handlers.
"""

from __future__ import annotations

import inspect

import pytest

from libs.webhook.protocol import WebhookModelProtocol
from src.caldotcom.webhook.booking import Webhook as CaldotcomBookingWebhook
from src.fathom.webhook.call import Webhook as FathomCallWebhook
from src.fathom.webhook.message import Webhook as FathomMessageWebhook
from src.octolens.webhook.mention import Webhook as OctolensMentionWebhook
from src.rb2b.webhook.visit import Webhook as Rb2bVisitWebhook

SOURCES = [
    pytest.param(CaldotcomBookingWebhook, id="caldotcom-booking"),
    pytest.param(FathomCallWebhook, id="fathom-call"),
    pytest.param(FathomMessageWebhook, id="fathom-message"),
    pytest.param(OctolensMentionWebhook, id="octolens-mention"),
    pytest.param(Rb2bVisitWebhook, id="rb2b-visit"),
]


# Method names declared on WebhookModelProtocol that must be callable on every
# source. Derived from the Protocol itself so adding a method there
# automatically tightens the test — no manual sync needed. Filters out dunders
# and any Protocol-internal attributes.
PROTOCOL_METHODS: tuple[str, ...] = tuple(
    sorted(name for name in vars(WebhookModelProtocol) if not name.startswith("_")),
)


@pytest.mark.parametrize("webhook_cls", SOURCES)
def test_webhook_satisfies_protocol(webhook_cls: type) -> None:
    # `issubclass(..., runtime_checkable Protocol)` only checks attribute
    # *names* exist — it does not check callability or signature. A class
    # that accidentally shadowed `attio_get_operations` with a non-callable
    # attribute would still pass. Walk the Protocol's methods directly and
    # verify each is callable with a compatible signature on the source.
    assert issubclass(webhook_cls, WebhookModelProtocol), (
        f"{webhook_cls.__module__}.{webhook_cls.__qualname__} is missing one "
        f"or more methods required by WebhookModelProtocol. Compare its "
        f"surface against libs/webhook/protocol.py."
    )

    for method_name in PROTOCOL_METHODS:
        member = getattr(webhook_cls, method_name, None)
        assert callable(member), (
            f"{webhook_cls.__qualname__}.{method_name} exists but is not "
            f"callable (got {type(member).__name__!r}). The Protocol contract "
            f"expects a method."
        )

        proto_sig = inspect.signature(getattr(WebhookModelProtocol, method_name))
        actual_sig = inspect.signature(member)
        proto_param_count = len(proto_sig.parameters)
        actual_param_count = len(actual_sig.parameters)
        assert proto_param_count == actual_param_count, (
            f"{webhook_cls.__qualname__}.{method_name} parameter count "
            f"({actual_param_count}) does not match Protocol "
            f"({proto_param_count}). Expected {proto_sig}, got {actual_sig}."
        )
