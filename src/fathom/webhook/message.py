"""Placeholder ETL contract for a future Fathom message webhook.

Fathom currently only delivers recording webhooks (see ``call.py``). This
module exists so ``webhooks/export_to_gcp_etl.py``'s eager import resolves.
Selecting this class as the active provider raises immediately — there is
no validated payload shape to ingest yet.
"""

from typing import Any

from pydantic import BaseModel

from libs.dlt.bucket_naming import etl_bucket_name, raw_bucket_name
from libs.fathom import Webhook as FathomWebhook


class Webhook(FathomWebhook):
    """Stub. Not implemented — see module docstring."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-gcp-202605260000"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return etl_bucket_name(source="fathom", entity_plural="messages")

    @staticmethod
    def raw_get_bucket_name() -> str:
        return raw_bucket_name(source="fathom", entity_plural="messages")

    @staticmethod
    def raw_get_app_name() -> str:
        from libs.dlt.filesystem_gcp import CloudGoogle

        return CloudGoogle.clean_bucket_name(bucket_name=Webhook.raw_get_bucket_name())

    # Raw passthrough has no per-source invariants — see caldotcom/booking.py.
    def raw_is_valid_webhook(self) -> bool:
        return True

    def raw_get_invalid_webhook_error_msg(self) -> str:
        return "raw passthrough accepts any payload; should not be reachable"

    @staticmethod
    def storage_get_app_name() -> str:
        return Webhook.etl_get_bucket_name()

    @staticmethod
    def storage_get_base_model_type() -> type[BaseModel] | None:
        return None

    @staticmethod
    def lance_get_project_name() -> str:
        raise NotImplementedError("Fathom message ETL is not implemented")

    @staticmethod
    def lance_get_base_model_type() -> str:
        raise NotImplementedError("Fathom message ETL is not implemented")

    def etl_is_valid_webhook(self) -> bool:
        return False

    def etl_get_invalid_webhook_error_msg(self) -> str:
        return "Fathom message webhook ETL is not implemented"

    def etl_get_json(self, storage: Any = None) -> str:
        raise NotImplementedError("Fathom message ETL is not implemented")

    def etl_get_file_name(self) -> str:
        raise NotImplementedError("Fathom message ETL is not implemented")

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        raise NotImplementedError("Fathom message ETL is not implemented")

    # --- Attio export contract ---

    @staticmethod
    def required_api_keys() -> list[str]:
        return ["ATTIO_API_KEY"]

    @staticmethod
    def optional_api_keys() -> list[str]:
        return []

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-fathom-messages"

    # --- Slack export contract (not implemented for this source) ---
    # Present so the WebhookModelProtocol conformance test passes; this source
    # is never wired into webhooks/export_to_slack.py.
    @staticmethod
    def slack_get_app_name() -> str:
        return "export-to-slack-from-fathom-messages"

    def slack_is_valid_webhook(self) -> bool:
        return False

    def slack_get_invalid_webhook_error_msg(self) -> str:
        return "Slack export is not supported for Fathom messages"

    def slack_get_messages(self) -> list[Any]:
        return []

    def attio_is_valid_webhook(self) -> bool:
        # Fathom "messages" are action-items / one-line follow-ups that don't
        # cleanly map to Attio yet. Returning False keeps the contract uniform
        # so the dispatcher can be pointed at this webhook without crashing,
        # but no Attio writes happen.
        return False

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return (
            "Fathom messages are not currently exported to Attio "
            "(UpsertNote mapping deferred)"
        )

    def attio_get_operations(self) -> list[Any]:
        return []
