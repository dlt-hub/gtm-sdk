"""Tests for src/octolens/backfill.py — CSV → webhook mapper + scope predicate."""

from __future__ import annotations

from typing import Any

from src.octolens.backfill import (
    is_dlthub_url,
    build_webhook_payload,
    coerce_view_id,
    include_mention,
    normalize_source,
    split_csv_list,
)
from src.octolens.webhook import Webhook


def _row(**overrides: Any) -> dict[str, Any]:
    """A representative CSV row (Title-Case keys, as csv.DictReader yields)."""
    base = {
        "URL": "https://twitter.com/dltHub/status/123",
        "Title": "",
        "Body": "great data tool",
        "Timestamp": "2026-05-10 11:55:53.000",
        "Image URL": "",
        "Source": "twitter",
        "Source ID": "123",
        "Author": "dltHub",
        "Author Avatar URL": "",
        "Author Profile Link": "https://twitter.com/dltHub",
        "Sentiment": "Positive",
        "Tags": "",
        "Language": "english",
        "Keyword": "dlthub",
        "View ID": "16485",
        "View Name": "For you",
    }
    base.update(overrides)
    return base


# --- include_mention -------------------------------------------------------


def test_include_dlthub_keyword() -> None:
    keep, reason = include_mention(_row(Keyword="dlthub", Body="great tool"))
    assert keep is True
    assert reason == "dlthub-anywhere"


# Neutral URL/author for the dlt/noise cases: the default fixture's URL contains
# "dltHub", which would (correctly) match dlthub-anywhere on its own.
_NEUTRAL = {
    "URL": "https://www.reddit.com/r/dataengineering/comments/x",
    "Author": "someuser",
}


def test_include_dlthub_in_text_without_keyword() -> None:
    keep, reason = include_mention(
        _row(
            Keyword="snowflake",
            Body="you should try dlthub for your ELT",
            **_NEUTRAL,
        ),
    )
    assert keep is True
    assert reason == "dlthub-anywhere"


def test_include_dlt_keyword_with_content_signal() -> None:
    keep, reason = include_mention(
        _row(Keyword="dlt", Body="my dlt pipeline keeps failing on load", **_NEUTRAL),
    )
    assert keep is True
    assert reason == "dlt+signal"


def test_reject_dlt_keyword_noise_without_signal() -> None:
    # The dominant failure mode: Octolens tagged "dlt" on unrelated content.
    keep, reason = include_mention(
        _row(
            Keyword="dlt",
            Title="Mini taco salad",
            Body="best taco bell order",
            URL="https://www.reddit.com/r/tacobell/comments/x",
            Author="foodie",
        ),
    )
    assert keep is False
    assert reason is None


def test_reject_pure_snowflake() -> None:
    keep, _ = include_mention(
        _row(Keyword="snowflake", Body="cold weather today", **_NEUTRAL),
    )
    assert keep is False


def test_include_dlthub_owned_url() -> None:
    # A dlthub-owned URL is an explicit signal even without a keyword/content hit.
    keep, reason = include_mention(
        _row(
            Keyword="snowflake",
            Title="",
            Body="unrelated data engineering chatter",
            URL="https://github.com/dlt-hub/dlt/issues/4002",
            Author="someuser",
        ),
    )
    assert keep is True
    assert reason == "dlthub-anywhere"


def test_reject_incidental_dlthub_in_url() -> None:
    # An incidental "dlthub" in a query string is not a dlthub-owned marker.
    keep, _ = include_mention(
        _row(
            Keyword="snowflake",
            Title="",
            Body="unrelated",
            URL="https://example.com/page?ref=dlthub",
            Author="someuser",
        ),
    )
    assert keep is False


def testis_dlthub_url_matches_owned_properties() -> None:
    assert is_dlthub_url("https://github.com/dlt-hub/dlt/issues/4002")
    assert is_dlthub_url("https://www.dlthub.com/docs")
    assert is_dlthub_url("https://docs.dlthub.com/intro")
    assert is_dlthub_url("https://twitter.com/dltHub/status/123")
    assert is_dlthub_url("https://www.reddit.com/r/dltHub/comments/x/")


def testis_dlthub_url_rejects_lookalikes() -> None:
    # Host parsing (not substring scan) rejects spoofs and path/query look-alikes.
    assert not is_dlthub_url("https://example.com/page?ref=dlthub")
    assert not is_dlthub_url("https://evil.com/dlthub")  # /dlthub only on twitter/x
    assert not is_dlthub_url(
        "https://github.com/dlt-hubble/x",
    )  # prefix is a path segment
    assert not is_dlthub_url(
        "https://dlthub.com.evil.com/",
    )  # suffix must be a real label
    assert not is_dlthub_url("")
    assert not is_dlthub_url(None)


# --- small helpers ---------------------------------------------------------


def test_split_csv_list_multivalue() -> None:
    assert split_csv_list("databricks, dlt") == ["databricks", "dlt"]
    assert split_csv_list("") == []
    assert split_csv_list(None) == []
    assert split_csv_list(" a , , b ") == ["a", "b"]


def test_coerce_view_id() -> None:
    assert coerce_view_id("16485") == 16485
    assert coerce_view_id("all") is None
    assert coerce_view_id("") is None
    assert coerce_view_id(None) is None


def test_normalize_source() -> None:
    assert normalize_source("YouTube") == "youtube"
    assert normalize_source(" Reddit ") == "reddit"
    assert normalize_source(None) == ""


# --- build_webhook_payload -------------------------------------------------


def test_payload_validates_and_stamps_unknown_relevance() -> None:
    payload = build_webhook_payload(
        _row(),
        relevance="unknown",
        source_file="export.csv",
    )
    assert payload["action"] == "mention_created"
    webhook = Webhook.model_validate(payload)
    assert webhook.data.relevance_score == "unknown"
    assert webhook.data.relevance_comment.startswith(
        "Backfilled from Octolens CSV export",
    )
    assert webhook.data.view_keywords == []
    assert webhook.data.bookmarked is False
    # "unknown" is not "low", so the default Attio filter lets it through.
    assert webhook.attio_is_valid_webhook() is True


def test_payload_primary_keyword_prefers_dlt_family() -> None:
    payload = build_webhook_payload(
        _row(Keyword="databricks, dlt"),
        relevance="unknown",
        source_file="export.csv",
    )
    assert payload["data"]["keyword"] == "dlt"
    assert payload["data"]["keywords"] == ["databricks", "dlt"]


def test_payload_splits_tags_and_maps_sentiment() -> None:
    payload = build_webhook_payload(
        _row(Tags="competitor_mention, hiring", Sentiment="Negative"),
        relevance="unknown",
        source_file="export.csv",
    )
    webhook = Webhook.model_validate(payload)
    assert webhook.data.tags == ["competitor_mention", "hiring"]
    assert webhook.data.sentiment_label == "Negative"


def test_payload_youtube_source_validates() -> None:
    payload = build_webhook_payload(
        _row(Source="youtube", URL="https://youtube.com/watch?v=abc"),
        relevance="unknown",
        source_file="export.csv",
    )
    webhook = Webhook.model_validate(payload)
    assert webhook.data.source == "youtube"
