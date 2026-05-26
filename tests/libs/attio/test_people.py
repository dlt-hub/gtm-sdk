# pyright: reportPrivateUsage=false
"""Unit tests for libs/attio/people.py helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@patch("libs.attio.people.get_client")
def test_get_person_values_email_match_uses_email_only(mock_get_client) -> None:
    """T6: When matching_attribute=email, helper filters by email_addresses only,
    even if linkedin/github_handle are also passed."""
    mock_client = MagicMock()
    mock_client.records.post_v2_objects_object_records_query.return_value.data = []
    mock_get_client.return_value.__enter__.return_value = mock_client

    from libs.attio.people import get_person_values

    get_person_values(
        matching_attribute="email",
        email="x@y.com",
        linkedin="https://linkedin.com/in/foo",
        github_handle="foo",
    )

    call = mock_client.records.post_v2_objects_object_records_query.call_args
    filter_ = call.kwargs["filter_"]
    assert filter_ == {"email_addresses": "x@y.com"}, (
        f"Expected email_addresses-only filter, got {filter_}"
    )


@patch("libs.attio.people.get_client")
def test_get_person_values_github_handle_match(mock_get_client) -> None:
    """T7: When matching_attribute=github_handle, helper filters by github_handle
    and returns the record's values dict on hit."""
    mock_record = MagicMock()
    mock_record.values = {"title": [{"value": "CTO"}]}
    mock_client = MagicMock()
    mock_client.records.post_v2_objects_object_records_query.return_value.data = [
        mock_record,
    ]
    mock_get_client.return_value.__enter__.return_value = mock_client

    from libs.attio.people import get_person_values

    result = get_person_values(
        matching_attribute="github_handle",
        email=None,
        linkedin=None,
        github_handle="octocat",
    )

    call = mock_client.records.post_v2_objects_object_records_query.call_args
    assert call.kwargs["filter_"] == {"github_handle": "octocat"}
    assert result == {"title": [{"value": "CTO"}]}


@patch("libs.attio.people.get_client")
def test_get_person_values_raises_when_required_identifier_missing(
    mock_get_client,
) -> None:
    """T8: ValueError when matching_attribute is set but the corresponding
    identifier is None. Attio client must not be invoked."""
    from libs.attio.people import get_person_values

    with pytest.raises(ValueError, match="github_handle"):
        get_person_values(
            matching_attribute="github_handle",
            email="x@y.com",
            linkedin="https://linkedin.com/in/foo",
            github_handle=None,
        )

    mock_get_client.assert_not_called()


@patch("libs.attio.people.get_client")
def test_get_person_values_linkedin_expands_url_variants(mock_get_client) -> None:
    """T9: When matching_attribute=linkedin, the read filter must expand to the
    same URL variants the write path searches across. Otherwise a non-canonical
    URL on the op can miss the existing record on the read side while the write
    still hits it — silently bypassing merge_only_if_empty protection.

    Regression test for the Codex review on the ai-805 push: raw equality on
    linkedin would leave a read/write mismatch for variant URLs.
    """
    mock_client = MagicMock()
    mock_client.records.post_v2_objects_object_records_query.return_value.data = []
    mock_get_client.return_value.__enter__.return_value = mock_client

    from libs.attio.people import _linkedin_url_variants, get_person_values

    input_url = "https://linkedin.com/in/foo"
    get_person_values(
        matching_attribute="linkedin",
        email=None,
        linkedin=input_url,
        github_handle=None,
    )

    call = mock_client.records.post_v2_objects_object_records_query.call_args
    filter_ = call.kwargs["filter_"]

    expected_variants = _linkedin_url_variants(input_url)
    assert len(expected_variants) > 1, (
        "test setup expects a non-canonical URL that expands to >1 variant"
    )
    assert filter_ == {
        "$or": [{"linkedin": v} for v in expected_variants],
    }, f"Expected $or-of-variants filter, got {filter_}"
