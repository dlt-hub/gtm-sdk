from __future__ import annotations

from src.rb2b.utils import split_rb2b_tags


def test_split_rb2b_tags_none_returns_empty() -> None:
    assert split_rb2b_tags(None) == []


def test_split_rb2b_tags_empty_string_returns_empty() -> None:
    assert split_rb2b_tags("") == []


def test_split_rb2b_tags_whitespace_only_returns_empty() -> None:
    assert split_rb2b_tags("   ") == []


def test_split_rb2b_tags_all_empty_tokens_returns_empty() -> None:
    assert split_rb2b_tags(",,,") == []


def test_split_rb2b_tags_single_tag() -> None:
    assert split_rb2b_tags("product") == ["product"]


def test_split_rb2b_tags_multi_tag() -> None:
    assert split_rb2b_tags("b2b,enterprise") == ["b2b", "enterprise"]


def test_split_rb2b_tags_strips_whitespace_around_tokens() -> None:
    assert split_rb2b_tags("  b2b  ,  enterprise  ") == ["b2b", "enterprise"]


def test_split_rb2b_tags_drops_trailing_comma() -> None:
    assert split_rb2b_tags("b2b,enterprise,") == ["b2b", "enterprise"]


def test_split_rb2b_tags_dedupes_case_insensitively_keeps_first_casing() -> None:
    assert split_rb2b_tags("b2b,B2B,b2b ") == ["b2b"]
    assert split_rb2b_tags("Sales,sales,SALES") == ["Sales"]
