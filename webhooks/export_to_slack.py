# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import modal
import orjson
from fastapi import Request
from modal import Image

from libs import infisical
from libs.logging.structured import (
    log,
    set_source,
    webhook_request_context,
)
from libs.slack import get_client
from libs.telemetry import init_log_exporter
from src.secrets_bootstrap import bootstrap_secret, hydrate
from src.slack.export import execute
from src.slack.thread_store import modal_dict_thread_store

# trunk-ignore-begin(ruff/F401,ruff/I001,pyright/reportUnusedImport)
# fmt: off
from src.caldotcom.webhook.booking import (
    Webhook as CaldotcomBookingWebhook,
)

# fmt: on
# trunk-ignore-end(ruff/F401,ruff/I001,pyright/reportUnusedImport)

if TYPE_CHECKING:
    # Type-check stand-in for the deploy-time placeholder; see the identical
    # block in webhooks/export_to_attio.py for the full rationale. The
    # scripts/webhooks-redeploy.py substitution rewrites WebhookModelToReplace
    # to a concrete Webhook class before modal deploy.
    from libs.webhook.protocol import (
        WebhookModelTypeCheckShim as WebhookModelToReplace,
    )


class WebhookModel(WebhookModelToReplace):
    pass


WebhookModel.model_rebuild()

APP_NAME: str = WebhookModel.slack_get_app_name()

set_source(APP_NAME)
init_log_exporter(APP_NAME)

image: Image = modal.Image.debian_slim().uv_pip_install(
    "fastapi[standard]",
    # flatsplode is pulled in transitively: the cal.com Webhook imports
    # src.caldotcom.utils, which imports flatsplode. Without it the container
    # crash-loops on import (ModuleNotFoundError) even though local tests pass
    # (flatsplode is in the project venv). Mirror export_to_attio.py.
    "flatsplode",
    "infisicalsdk>=1.0.16",
    # OpenTelemetry OTLP exporter — init_log_exporter() imports these lazily,
    # but only when telemetry is activated (HYPERDX_API_KEY / *_OTLP_ENDPOINT in
    # the env). Since HYPERDX_API_KEY is shipped via bootstrap_secret(), the
    # container imports them on startup and crash-loops without them. The other
    # webhook handlers omit these only because their running images predate
    # HYPERDX_API_KEY landing in Infisical (their telemetry stays a no-op).
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-semantic-conventions",
    "orjson",
    "slack-sdk>=3.27",
    "uuid7",
)
image = image.add_local_python_source(
    *[
        "libs",
        "src",
    ],
)
app = modal.App(name=APP_NAME, image=image)

# One durable modal.Dict per Slack app keeps booking thread anchors
# (thread_key -> Slack ts) from colliding across sources/apps.
_THREAD_STORE_NAME = f"{APP_NAME}-threads"


def _export(webhook: WebhookModel) -> str:
    payload_bytes = len(orjson.dumps(webhook.model_dump()))
    log("webhook.received", payload_bytes=payload_bytes)
    if not webhook.slack_is_valid_webhook():
        reason = webhook.slack_get_invalid_webhook_error_msg()
        log("webhook.validation_failed", reason=reason)
        return reason
    # SLACK_BOT_TOKEN is bound into libs.slack's api_key_scope by hydrate; the
    # target channel is a plain value fetched directly from the per-source key
    # the Webhook declares (e.g. CALCOM_SLACK_CHANNEL_ID) so each automation
    # posts to its own channel.
    channel_key = WebhookModel.slack_get_channel_secret_name()
    with hydrate("SLACK_BOT_TOKEN"), infisical.fetch(channel_key) as channel:
        messages = webhook.slack_get_messages()
        log("webhook.validated", message_count=len(messages))
        if not messages:
            return "no Slack messages produced for this event"
        result = execute(
            messages,
            channel=channel,
            client=get_client(),
            thread_store=modal_dict_thread_store(_THREAD_STORE_NAME),
        )
        return result.body()


def _handle(webhook: WebhookModel, request: Request) -> str:
    """Webhook request lifecycle. Kept separate from the endpoint so the
    request-context wiring and timing log are reachable from plain-Python
    tests without Modal's local-call machinery."""
    with webhook_request_context(request):
        started = time.perf_counter()
        try:
            body = _export(webhook)
        except Exception as exc:
            log(
                "webhook.completed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="error",
                error_type=type(exc).__name__,
                error_msg=str(exc),
            )
            raise
        log(
            "webhook.completed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            status="ok",
        )
        return body


@app.function(
    secrets=[bootstrap_secret()],
    region="us-east-1",
    enable_memory_snapshot=False,
)
@modal.fastapi_endpoint(method="POST", docs=True)
# trunk-ignore(pyright/reportUntypedFunctionDecorator): Modal decorators are untyped; same as the other webhook handlers
@modal.concurrent(max_inputs=1000)
def web(
    webhook: WebhookModel,
    request: Request,
):  # no return annotation: see export_to_attio.py for the FastAPI/Modal rationale
    return _handle(webhook, request)


@app.local_entrypoint()  # trunk-ignore(pyright/reportUntypedFunctionDecorator): Modal decorators are untyped; same as the other webhook handlers
def local(input_file: str) -> None:
    raw = Path(input_file).read_bytes()
    payload = orjson.loads(raw)
    webhook = WebhookModel.model_validate(payload)
    print(_export(webhook))
