# pyright: reportPrivateUsage=false
from __future__ import annotations

from unittest.mock import patch

import pytest

from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import SchemaMismatchError
from libs.attio.models import PersonInput
from libs.attio.people import (
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


@patch("libs.attio.people._search_people_raw")
@patch("libs.attio.people.add_person")
def test_upsert_person_routes_github_handle_match(mock_add, mock_search) -> None:
    mock_search.return_value = []
    mock_add.return_value = _envelope("rec_new")

    upsert_person(
        PersonInput(github_handle="elviskahoro"),
        matching_attribute="github_handle",
    )

    # A single identity lookup on github_handle only — no post-create
    # reconciliation re-search (that machinery was dropped in ai-icn).
    mock_search.assert_called_once()
    kwargs = mock_search.call_args.kwargs
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

    mock_search.assert_called_once()
    kwargs = mock_search.call_args.kwargs
    assert kwargs.get("email") == "a@example.com"


@patch("libs.attio.people.get_client")
def test_search_people_raw_translates_unknown_filter_attribute(mock_get_client) -> None:
    """Querying people by an undefined slug (the `github` slug if archived/absent)
    yields a `filter_error` the SDK can't unmarshal; _search_people_raw must
    surface a typed SchemaMismatchError, not a raw ResponseValidationError
    (ai-0ex). The github handle is searched via the `github` Attio slug (ai-0jg)."""
    client = mock_get_client.return_value.__enter__.return_value
    client.records.post_v2_objects_object_records_query.side_effect = _ErrWithBody(
        '{"status_code": 400, "type": "invalid_request_error",'
        ' "code": "unknown_filter_attribute_slug", "message": "Unknown attribute'
        ' slug: github"}',
    )

    with pytest.raises(SchemaMismatchError) as exc_info:
        _search_people_raw(github_handle="elviskahoro")

    assert exc_info.value.field == "github"
