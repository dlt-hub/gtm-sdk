# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import modal
import orjson
from fastapi import Request
from modal import Image

from libs.attio.preflight import assert_attio_token_scopes
from libs.logging.structured import (
    log,
    set_source,
    webhook_request_context,
)
from libs.telemetry import init_log_exporter
from src.attio.export import execute
from src.secrets_bootstrap import bootstrap_secret, hydrate

# trunk-ignore-begin(ruff/F401,ruff/I001,pyright/reportUnusedImport)
# fmt: off
from src.caldotcom.webhook.booking import (
    Webhook as CaldotcomBookingWebhook,
)
from src.fathom.webhook.call import (
    Webhook as FathomCallWebhook,
)
from src.fathom.webhook.message import (
    Webhook as FathomMessageWebhook,
)
from src.octolens.webhook import (
    Webhook as OctolensWebhook,
)
from src.rb2b.webhook.visit import (
    Webhook as Rb2bVisitWebhook,
)

# fmt: on
# trunk-ignore-end(ruff/F401,ruff/I001,pyright/reportUnusedImport)

if TYPE_CHECKING:
    # Type-check stand-in for the deploy-time placeholder. The
    # `scripts/webhooks-redeploy.py` substitution pass rewrites every occurrence of
    # `WebhookModelToReplace` to a concrete `Webhook` class before
    # `modal deploy`. The TYPE_CHECKING block is skipped at runtime, so the
    # rewritten image inherits from the real Pydantic Webhook subclass; for
    # pyright/ruff in the source tree it aliases this shim, which exposes
    # both the Pydantic surface (`model_rebuild`, `model_validate`) and the
    # WebhookModelProtocol contract. Eliminates the F821 suppression that
    # was structural before this Protocol landed.
    from libs.webhook.protocol import (
        WebhookModelTypeCheckShim as WebhookModelToReplace,
    )


class WebhookModel(WebhookModelToReplace):
    pass


WebhookModel.model_rebuild()

APP_NAME: str = WebhookModel.attio_get_app_name()

# Set the `source` contextvar once per container so every log line emitted
# from this webhook is filterable by `source=<app-name>` in Modal logs.
set_source(APP_NAME)

# Initialize the OTLP log exporter so structured events also ship to whatever
# sink the container's OTEL env vars point at (HyperDX/Datadog/Grafana/etc.).
# No-op if no OTLP env vars are set — Modal stdout capture stays the
# always-on transport.
init_log_exporter(APP_NAME)

image: Image = modal.Image.debian_slim().uv_pip_install(
    "attio>=0.21.2",
    "fastapi[standard]",
    "flatsplode",
    "infisicalsdk>=1.0.16",
    "orjson",
    "uuid7",
)
image = image.add_local_python_source(
    *[
        "libs",
        "src",
    ],
)
app = modal.App(name=APP_NAME, image=image)


def _export(webhook: WebhookModel) -> str:
    payload_bytes = len(orjson.dumps(webhook.model_dump()))
    log("webhook.received", payload_bytes=payload_bytes)
    if not webhook.attio_is_valid_webhook():
        reason = webhook.attio_get_invalid_webhook_error_msg()
        log("webhook.validation_failed", reason=reason)
        return reason
    required = WebhookModel.required_api_keys()
    with hydrate(*required):
        # Fail fast with an actionable message if the Attio token lacks a scope
        # the writer path needs, instead of surfacing Attio's opaque
        # "...does not exist or you do not have permission..." four ops deep
        # inside a write (ai-ica). Cheap: one cached /v2/self per token.
        #
        # NOTE: this uses the default profile, where `object_configuration:
        # read-write` is RECOMMENDED (warn), not REQUIRED (raise) — deliberately.
        # A webhook token only needs `record_permission:read-write` at runtime
        # once the tracking_events schema is pre-bootstrapped (the closed
        # meeting vocabulary is seeded, so JIT ensure_select_options becomes a
        # no-op GET). Hard-requiring schema-mutation scope here would force every
        # webhook token to carry the power to rewrite the workspace schema —
        # against least privilege. The residual case (a genuinely new, unseeded
        # option, e.g. a new `source` emitter, on a restricted token) is no
        # longer opaque: classify_error tags it `insufficient_scope` with
        # remediation. The bootstrap script, which DOES mutate schema, requires
        # the stronger scope explicitly.
        if "ATTIO_API_KEY" in required:
            assert_attio_token_scopes()
        plan = webhook.attio_get_operations()
        log("webhook.validated", op_count=len(plan))
        return execute(plan).body()


def _handle(webhook: WebhookModel, request: Request) -> str:
    """Webhook request lifecycle.

    Kept separate from the `@modal.fastapi_endpoint`-decorated `web` so the
    request-context wiring and `webhook.completed` timing are reachable from
    plain-Python tests without going through Modal's local-call machinery.
    """
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
@modal.concurrent(max_inputs=1000)
def web(
    webhook: WebhookModel,
    request: Request,
):  # no return annotation: avoids FastAPI auto-building a response_model that unions with starlette Response and trips its Pydantic validation (modal magic_fastapi_app doesn't let us pass response_model=None)
    return _handle(webhook, request)


@app.local_entrypoint()
def local(input_file: str) -> None:
    raw = Path(input_file).read_bytes()
    payload = orjson.loads(raw)
    webhook = WebhookModel.model_validate(payload)
    print(_export(webhook))
