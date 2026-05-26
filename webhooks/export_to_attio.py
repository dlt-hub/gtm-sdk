# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

import os
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import modal
import orjson
from fastapi import Request
from modal import Image

from libs import infisical
from libs.attio import client as attio_client
from libs.caldotcom import client as caldotcom_client
from libs.logging.structured import (
    log,
    set_source,
    webhook_request_context,
)
from src.attio.export import execute

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
    # `scripts/redeploy_webhook.py` substitution pass rewrites every occurrence of
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


def _bootstrap_secret() -> modal.Secret:
    """Build an inline Modal Secret carrying Infisical bootstrap creds.

    Values come from the deploy-time shell env (populated by sourcing
    ``.env.local`` and running under ``infisical run --env=<env>``). Values
    are transmitted to Modal as a server-side secret object — they do NOT
    appear in image layers or build logs (validated 2026-05-18 during the
    ``ai-2aw`` ``from_dict`` probe; see ``modal-never-use-image-env-for-secrets-values``).

    Missing creds are NOT raised here. This function runs at module-import
    time (it's the value passed to ``@app.function(secrets=[...])``), so
    raising would break tests that load this module without a real Infisical
    environment. ``scripts/deploy-webhook.sh`` preflights the bootstrap env
    before deploy; at runtime, ``infisical.fetch_all`` will raise
    ``InfisicalAuthError`` if the token is empty.
    """
    payload: dict[str, str | None] = {
        "INFISICAL_TOKEN": os.environ.get("INFISICAL_TOKEN", ""),
        "INFISICAL_PROJECT_ID": os.environ.get("INFISICAL_PROJECT_ID", ""),
    }
    for opt in ("INFISICAL_HOST", "INFISICAL_ENV"):
        v = os.environ.get(opt, "").strip()
        if v:
            payload[opt] = v
    return modal.Secret.from_dict(payload)


# Maps each declared ``required_api_keys()`` name to the lib's
# ``api_key_scope`` context manager. New lib clients that read from a
# contextvar (the same pattern as ``libs.attio.client``) get an entry here
# so the dispatcher knows how to activate them. Keys declared by a
# webhook but missing from this map are silently skipped — they are
# resolved into the ``infisical.fetch_all`` dict but not bound into any
# lib scope (use ``api_key=`` arg paths directly if needed).
_KEY_SCOPES: dict[str, Callable[[str], AbstractContextManager[None]]] = {
    "ATTIO_API_KEY": attio_client.api_key_scope,
    "CALCOM_API_KEY": caldotcom_client.api_key_scope,
}


@contextmanager
def _activate_key_scopes(resolved: dict[str, str]) -> Generator[None, None, None]:
    with ExitStack() as stack:
        for name, value in resolved.items():
            scope_fn = _KEY_SCOPES.get(name)
            if scope_fn is None:
                continue
            stack.enter_context(scope_fn(value))
        yield


def _export(webhook: WebhookModel) -> str:
    payload_bytes = len(orjson.dumps(webhook.model_dump()))
    log("webhook.received", payload_bytes=payload_bytes)
    if not webhook.attio_is_valid_webhook():
        reason = webhook.attio_get_invalid_webhook_error_msg()
        log("webhook.validation_failed", reason=reason)
        return reason
    required = WebhookModel.required_api_keys()
    with (
        infisical.fetch_all(required) as resolved,
        _activate_key_scopes(resolved),
    ):
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
    secrets=[_bootstrap_secret()],
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
