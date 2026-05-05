"""Modal webhook handlers for Cal.com booking ingestion."""

import json
import logging

from fastapi import HTTPException

from src.app import app, image
from src.caldotcom.webhook import Webhook
from src.caldotcom.utils import write_to_gcs

logger = logging.getLogger(__name__)


@app.function(image=image)
async def export_to_gcp_etl(payload: dict) -> dict:
    """
    Validate, flatten, and archive Cal.com booking webhook to GCS ETL bucket.

    Args:
        payload: BookingOutput_2024_08_13 JSON payload

    Returns:
        Status dict with file location

    Raises:
        HTTPException: 422 for validation/contract errors, 500 for GCS failures
    """
    try:
        # Validate and instantiate Webhook
        webhook = Webhook(**payload)
    except Exception as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=422, detail=f"Invalid booking payload: {str(e)}")

    # Check if this webhook family has ETL support
    if not webhook.etl_is_valid_webhook():
        logger.warning(f"Raw-only webhook family: {webhook.etl_get_invalid_webhook_error_msg()}")
        raise HTTPException(
            status_code=422,
            detail={
                "status": "raw_only_family",
                "reason": webhook.etl_get_invalid_webhook_error_msg(),
            },
        )

    try:
        # Generate JSONL and filename
        jsonl_content = webhook.etl_get_json()
        filename = webhook.etl_get_file_name()
        bucket = webhook.etl_get_bucket_name()

        # Write to GCS
        write_to_gcs(bucket, filename, jsonl_content)
        logger.info(f"Wrote ETL output to gs://{bucket}/{filename}")

        return {
            "status": "success",
            "bucket": bucket,
            "file": filename,
            "booking_uid": webhook.uid,
        }
    except Exception as e:
        logger.error(f"GCS write error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write to GCS: {str(e)}")


@app.function(image=image)
async def export_to_gcp_raw(payload: dict) -> dict:
    """
    Archive raw, unmodified Cal.com booking webhook payload to GCS raw bucket.

    Args:
        payload: Raw booking payload as dict

    Returns:
        Status dict with file location

    Raises:
        HTTPException: 500 for GCS failures
    """
    try:
        # Generate filename from payload if possible
        uid = payload.get("uid", "unknown")
        import uuid
        from datetime import datetime
        from src.caldotcom.utils import clean_timestamp, clean_string

        start_str = payload.get("start", datetime.utcnow().isoformat())
        if isinstance(start_str, str):
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        else:
            start = start_str

        title = payload.get("title", "booking")
        timestamp = clean_timestamp(start)
        clean_title = clean_string(title)
        filename = f"{timestamp}-{uid}-{clean_title}.jsonl"

        # Write raw payload as JSONL
        raw_content = json.dumps(payload) + "\n"
        bucket = "devx-caldotcom-booking-raw"
        write_to_gcs(bucket, filename, raw_content)
        logger.info(f"Wrote raw payload to gs://{bucket}/{filename}")

        return {
            "status": "success",
            "bucket": bucket,
            "file": filename,
            "booking_uid": uid,
        }
    except Exception as e:
        logger.error(f"GCS write error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write to GCS: {str(e)}")
