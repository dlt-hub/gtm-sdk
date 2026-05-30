from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from libs.attio.mentions import upsert_mention
from libs.attio.models import MentionInput


def _sample(
    action: str = "mention_created",
    *,
    relevance_score: str | None = None,
    relevance_comment: str | None = None,
) -> MentionInput:
    return MentionInput(
        mention_url="https://reddit.com/r/x/comments/abc",
        last_action=action,  # type: ignore[arg-type]
        source_platform="reddit",
        source_id="abc",
        mention_body="hello",
        mention_timestamp=datetime(2026, 5, 10, 11, 55, 53),
        author_handle="u",
        primary_keyword="kw",
        relevance_score=relevance_score,
        relevance_comment=relevance_comment,
    )


def _mock_client_with_response(record_id: str) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    response = MagicMock()
    response.data = MagicMock()
    response.data.id.record_id = record_id
    client.records.put_v2_objects_object_records.return_value = response
    return client


def test_upsert_mention_calls_assert_endpoint() -> None:
    client = _mock_client_with_response("rec-1")
    with (
        patch("libs.attio.mentions.get_client", return_value=client),
        patch(
            "libs.attio.mentions.ensure_select_options",
        ),
    ):
        envelope = upsert_mention(_sample())
    client.records.put_v2_objects_object_records.assert_called_once()
    _, kwargs = client.records.put_v2_objects_object_records.call_args
    assert kwargs["object"] == "social_mention"
    assert kwargs["matching_attribute"] == "mention_url"
    assert envelope.success is True
    assert envelope.record_id == "rec-1"


def test_upsert_mention_update_path_preserves_source_identity() -> None:
    """Regression for AI-290.

    The assert endpoint creates the record on the first delivery the system
    processes for a `mention_url`. If that first delivery happens to be a
    `mention_updated` (e.g. the create event was dropped or replayed
    out of order), the new record must still carry source_platform /
    source_id — otherwise it lands without its required identity fields.
    """
    client = _mock_client_with_response("rec-1")
    with (
        patch("libs.attio.mentions.get_client", return_value=client),
        patch(
            "libs.attio.mentions.ensure_select_options",
        ),
    ):
        upsert_mention(_sample(action="mention_updated"))
    _, kwargs = client.records.put_v2_objects_object_records.call_args
    data_obj = kwargs["data"]
    # data_obj is the assert-request Pydantic model from the Attio SDK;
    # `values` is accessible as a pydantic field.
    values = data_obj.values
    assert "source_platform" in values
    assert "source_id" in values


# --- No-downgrade rule: a backfill "unknown" must never write relevance -------
#
# "unknown" means "no opinion": the writer drops relevance_score /
# relevance_comment from the assert entirely, so an existing live score is left
# intact and a new record is left unscored. This is race-free (no read), unlike
# a read-then-conditional-write. These mocks assert the PUT payload; real
# round-trip preservation is covered by
# tests/integration/test_attio_mention_writer_live.py.


def _put_values(client: MagicMock) -> Any:
    _, kwargs = client.records.put_v2_objects_object_records.call_args
    return kwargs["data"].values


def test_backfill_unknown_omits_relevance_from_assert() -> None:
    client = _mock_client_with_response("rec-1")
    with (
        patch("libs.attio.mentions.get_client", return_value=client),
        patch("libs.attio.mentions.ensure_select_options"),
    ):
        upsert_mention(_sample(relevance_score="unknown", relevance_comment="backfill"))
    values = _put_values(client)
    assert "relevance_score" not in values
    assert "relevance_comment" not in values
    # Race-free: the unknown path never reads the existing record.
    client.records.post_v2_objects_object_records_query.assert_not_called()


def test_live_score_writes_relevance() -> None:
    client = _mock_client_with_response("rec-1")
    with (
        patch("libs.attio.mentions.get_client", return_value=client),
        patch("libs.attio.mentions.ensure_select_options"),
    ):
        upsert_mention(_sample(relevance_score="medium", relevance_comment="real"))
    assert "relevance_score" in _put_values(client)
    client.records.post_v2_objects_object_records_query.assert_not_called()
