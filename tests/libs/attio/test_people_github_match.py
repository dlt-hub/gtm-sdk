# pyright: reportPrivateUsage=false
from __future__ import annotations

from unittest.mock import patch

import pytest

from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import SchemaMismatchError
from libs.attio.models import PersonInput, PersonSearchResult
from libs.attio.people import (
    _RACE_RECHECK_ATTEMPTS,
    _search_people_raw,
    upsert_person,
)


class _ErrWithBody(Exception):
    """Mimics the attio SDK's ResponseValidationError carrying a `.body`."""

    def __init__(self, body: str) -> None:
        super().__init__("response validation failed")
        self.body = body


def _envelope(record_id: str, action: str = "created") -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action=action,  # type: ignore[arg-type]
        record_id=record_id,
        warnings=[],
    )


def _hit(record_id: str) -> PersonSearchResult:
    return PersonSearchResult(record_id=record_id)


def _no_sleep(_seconds: float) -> None:
    """Typed no-op stand-in for time.sleep so race-retry tests don't really wait."""
    return None


@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_upsert_person_routes_github_handle_match(mock_add, mock_search) -> None:
    mock_search.return_value = []
    mock_add.return_value = _envelope("rec_new")

    upsert_person(
        PersonInput(github_handle="elviskahoro"),
        matching_attribute="github_handle",
    )

    # First search is the identity lookup; a second search runs post-create to
    # reconcile any concurrent-create race on the non-unique github_handle.
    kwargs = mock_search.call_args_list[0].kwargs
    assert kwargs.get("github_handle") == "elviskahoro"
    # Must NOT pass email or linkedin
    assert kwargs.get("email") is None
    assert kwargs.get("linkedin") is None


@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_upsert_person_email_match_unchanged(mock_add, mock_search) -> None:
    mock_search.return_value = []
    mock_add.return_value = _envelope("rec_new")

    upsert_person(PersonInput(email="a@example.com"), matching_attribute="email")

    # email is server-side unique, so no post-create reconciliation re-search.
    mock_search.assert_called_once()
    kwargs = mock_search.call_args.kwargs
    assert kwargs.get("email") == "a@example.com"


@patch("libs.attio.people.time.sleep", _no_sleep)
@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_no_race_returns_created_clean(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # Identity lookup empty; every post-create re-attempt sees only our record,
    # so after exhausting the retry budget we conclude there was no race.
    mock_search.side_effect = [[], *([[_hit("rec_a")]] * _RACE_RECHECK_ATTEMPTS)]
    mock_add.return_value = _envelope("rec_a")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    assert result.record_id == "rec_a"
    assert result.warnings == []
    mock_update.assert_not_called()
    mock_delete.assert_not_called()


@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_race_loser_retracts_and_merges(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # We created the lexicographically-larger id; a racer already holds the
    # canonical (smaller) id, so we must retract ours and merge onto canonical.
    mock_search.side_effect = [[], [_hit("rec_a"), _hit("rec_b")]]
    mock_add.return_value = _envelope("rec_b")
    mock_update.return_value = _envelope("rec_a", action="updated")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    mock_delete.assert_called_once_with("rec_b")
    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs["record_id"] == "rec_a"
    assert result.record_id == "rec_a"
    assert result.partial_success is True
    assert any(w.code == "upsert_race_resolved_to_existing" for w in result.warnings)


@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_race_winner_keeps_record_and_warns(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # We created the canonical (smaller) id; the racer holds the duplicate and
    # will retract itself. We keep our record, warn, and never delete/update.
    mock_search.side_effect = [[], [_hit("rec_a"), _hit("rec_b")]]
    mock_add.return_value = _envelope("rec_a")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    assert result.record_id == "rec_a"
    assert result.partial_success is True
    assert any(w.code == "upsert_race_duplicate_detected" for w in result.warnings)
    mock_delete.assert_not_called()
    mock_update.assert_not_called()


@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_race_research_failure_is_non_fatal(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # Re-search blows up; the upsert must still return the created envelope.
    mock_search.side_effect = [[], RuntimeError("attio flaked")]
    mock_add.return_value = _envelope("rec_a")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    assert result.record_id == "rec_a"
    mock_delete.assert_not_called()
    mock_update.assert_not_called()


@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_race_merge_failure_keeps_record_no_delete(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # Loser path, but the merge onto canonical fails. We must NOT delete our
    # record (that would lose data) — keep it, flag it, and never delete.
    mock_search.side_effect = [[], [_hit("rec_a"), _hit("rec_b")]]
    mock_add.return_value = _envelope("rec_b")
    mock_update.side_effect = RuntimeError("attio rejected the merge")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    assert result.record_id == "rec_b"  # our record retained
    assert result.partial_success is True
    assert any(w.code == "upsert_race_merge_failed" for w in result.warnings)
    mock_update.assert_called_once()  # merge attempted (before any delete)
    mock_delete.assert_not_called()  # never delete when merge failed


@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_race_cleanup_delete_failure_is_flagged(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # Loser path: merge onto canonical succeeds but the duplicate-retraction
    # delete fails. The envelope must NOT claim the duplicate was retracted —
    # it must surface a distinct cleanup-failure warning instead.
    mock_search.side_effect = [[], [_hit("rec_a"), _hit("rec_b")]]
    mock_add.return_value = _envelope("rec_b")
    mock_update.return_value = _envelope("rec_a", action="updated")
    mock_delete.side_effect = RuntimeError("delete scope missing")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    assert result.record_id == "rec_a"  # merged onto canonical
    assert result.partial_success is True
    codes = {w.code for w in result.warnings}
    assert "upsert_race_cleanup_delete_failed" in codes
    assert "upsert_race_resolved_to_existing" not in codes


@patch("libs.attio.people.time.sleep", _no_sleep)
@patch("libs.attio.people._delete_person")
@patch("libs.attio.people.update_person")
@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_github_create_race_retry_catches_lagging_racer(
    mock_add,
    mock_search,
    mock_update,
    mock_delete,
) -> None:
    # The competing create is not visible on the first re-search (index lag) but
    # appears on the second. The bounded retry must catch it rather than
    # concluding "no race" on the first miss.
    mock_search.side_effect = [
        [],  # initial identity lookup
        [_hit("rec_b")],  # first re-search: racer not yet visible
        [_hit("rec_a"), _hit("rec_b")],  # second re-search: racer now visible
    ]
    mock_add.return_value = _envelope("rec_b")
    mock_update.return_value = _envelope("rec_a", action="updated")

    result = upsert_person(
        PersonInput(github_handle="octocat"),
        matching_attribute="github_handle",
    )

    assert result.record_id == "rec_a"
    mock_delete.assert_called_once_with("rec_b")
    assert any(w.code == "upsert_race_resolved_to_existing" for w in result.warnings)


@patch("libs.attio.people.get_client")
def test_search_people_raw_translates_unknown_filter_attribute(mock_get_client) -> None:
    """Querying people by an undefined slug (github_handle pre-bootstrap) yields
    a `filter_error` the SDK can't unmarshal; _search_people_raw must surface a
    typed SchemaMismatchError, not a raw ResponseValidationError (ai-0ex)."""
    client = mock_get_client.return_value.__enter__.return_value
    client.records.post_v2_objects_object_records_query.side_effect = _ErrWithBody(
        '{"status_code": 400, "type": "invalid_request_error",'
        ' "code": "unknown_filter_attribute_slug", "message": "Unknown attribute'
        ' slug: github_handle"}',
    )

    with pytest.raises(SchemaMismatchError) as exc_info:
        _search_people_raw(github_handle="elviskahoro")

    assert exc_info.value.field == "github_handle"
