# trunk-ignore-all(ruff/PGH003,trunk/ignore-does-nothing)
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import modal
from modal import Image

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
    # `scripts/deploy-webhook.sh` sed pass rewrites every occurrence of
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

image: Image = modal.Image.debian_slim().uv_pip_install(
    "attio>=0.21.2",
    "fastapi[standard]",
    "flatsplode",
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
    if not webhook.attio_is_valid_webhook():
        return webhook.attio_get_invalid_webhook_error_msg()
    plan = webhook.attio_get_operations()
    return execute(plan).body()


@app.function(
    secrets=[
        modal.Secret.from_name(n)
        for n in WebhookModel.attio_get_secret_collection_names()
    ],
    region="us-east-1",
    enable_memory_snapshot=False,
)
@modal.fastapi_endpoint(method="POST", docs=True)
@modal.concurrent(max_inputs=1000)
def web(
    webhook: WebhookModel,
):  # no return annotation: avoids FastAPI auto-building a response_model that unions with starlette Response and trips its Pydantic validation (modal magic_fastapi_app doesn't let us pass response_model=None)
    return _export(webhook)


@app.local_entrypoint()
def local(input_file: str) -> None:
    import orjson

    raw = Path(input_file).read_bytes()
    payload = orjson.loads(raw)
    webhook = WebhookModel.model_validate(payload)
    print(_export(webhook))
