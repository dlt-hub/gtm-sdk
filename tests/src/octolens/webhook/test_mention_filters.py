"""Tests for the composable webhook filter framework."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from libs.octolens import Mention
from src.octolens.webhook.mention import (
    DEFAULT_FILTERS,
    RelevanceScoreFilter,
    Webhook,
    WebhookFilters,
)

SAMPLES_DIR = Path(__file__).resolve().parents[4] / "api" / "samples"
REDDIT_LOW_SAMPLE = "octolens.mention.created.reddit.redacted.json"


def _load(filename: str) -> dict[str, Any]:
    return json.loads((SAMPLES_DIR / filename).read_text())


def test_default_filter_drops_low_relevance_for_attio_only() -> None:
    webhook = Webhook.model_validate(_load(REDDIT_LOW_SAMPLE))
    assert webhook.data.relevance_score == "low"
    assert webhook.attio_is_valid_webhook() is False
    # Filters do NOT apply to the ETL path — raw export captures every mention.
    assert webhook.etl_is_valid_webhook() is True
    assert webhook.attio_get_operations() == []
    assert "drop-low-relevance" in webhook.attio_get_invalid_webhook_error_msg()


def test_filter_keeps_medium_and_high() -> None:
    payload = _load(REDDIT_LOW_SAMPLE)
    payload["data"]["relevanceScore"] = "medium"
    assert Webhook.model_validate(payload).attio_is_valid_webhook() is True

    payload["data"]["relevanceScore"] = "high"
    assert Webhook.model_validate(payload).attio_is_valid_webhook() is True


def test_filters_serialize_to_json_array() -> None:
    dumped = DEFAULT_FILTERS.model_dump()
    assert isinstance(dumped, list)
    assert dumped == [
        {
            "name": "drop-low-relevance",
            "type": "relevance_score",
            "excluded_scores": ["low"],
        },
    ]

    roundtrip = WebhookFilters.model_validate_json(DEFAULT_FILTERS.model_dump_json())
    assert isinstance(roundtrip.root[0], RelevanceScoreFilter)
    assert roundtrip.root[0].excluded_scores == ["low"]


def test_unknown_source_rejected() -> None:
    payload = _load(REDDIT_LOW_SAMPLE)["data"]
    payload["source"] = "tiktok"
    with pytest.raises(ValidationError):
        Mention.model_validate(payload)


def test_unknown_relevance_score_rejected() -> None:
    payload = _load(REDDIT_LOW_SAMPLE)["data"]
    payload["relevanceScore"] = "critical"
    with pytest.raises(ValidationError):
        Mention.model_validate(payload)
