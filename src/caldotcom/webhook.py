"""Webhook ETL contract for Cal.com booking ingestion."""

from typing import Any

from libs.caldotcom import Booking
from src.caldotcom.utils import (
    booking_to_jsonl,
    generate_gcs_filename,
)


class Webhook(Booking):
    """Webhook subclass implementing ETL contract for Cal.com bookings."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        """Return Modal secret collection names needed for GCS access."""
        return ["devx-growth-gcp"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        """Return GCS bucket for ETL outputs."""
        return "devx-caldotcom-booking-etl"

    @staticmethod
    def storage_get_app_name() -> str:
        """Return app name for storage (same as ETL bucket)."""
        return Webhook.etl_get_bucket_name()

    @staticmethod
    def storage_get_base_model_type() -> None:
        """Return base model type for storage (None for Phase 1)."""
        return None

    @staticmethod
    def lance_get_project_name() -> str:
        """Raise NotImplementedError (Phase 2 feature)."""
        raise NotImplementedError("LanceDB integration is Phase 2+")

    @staticmethod
    def lance_get_base_model_type() -> str:
        """Raise NotImplementedError (Phase 2 feature)."""
        raise NotImplementedError("LanceDB integration is Phase 2+")

    def etl_is_valid_webhook(self) -> bool:
        """Return True: BOOKING family has meaningful ETL representation."""
        return True

    def etl_get_invalid_webhook_error_msg(self) -> str:
        """Return error message (not used for BOOKING family)."""
        return "This webhook family does not support ETL output"

    def etl_get_json(self, storage=None) -> str:
        """Convert booking to flattened JSONL with booking_uid and row ids."""
        return booking_to_jsonl(self.model_dump(), self.uid)

    def etl_get_file_name(self) -> str:
        """Generate GCS filename."""
        return generate_gcs_filename(self.start, self.uid, self.title)

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        """Raise NotImplementedError (Phase 2 feature)."""
        raise NotImplementedError("LanceDB integration is Phase 2+")
