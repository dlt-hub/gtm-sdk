"""Tests for libs/parsers/countries.py — country name → ISO-3166-1 alpha-2."""

from __future__ import annotations

import pytest

from libs.parsers.countries import country_name_to_iso2


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # ISO names resolved by pycountry.lookup
        ("United States", "US"),
        ("India", "IN"),
        ("United Kingdom", "GB"),
        ("Canada", "CA"),
        ("Germany", "DE"),
        ("France", "FR"),
        # Informal aliases
        ("USA", "US"),
        ("UK", "GB"),
        # Already-ISO passthrough (case-insensitive)
        ("US", "US"),
        ("us", "US"),
        # Whitespace tolerance
        ("  Germany  ", "DE"),
    ],
)
def test_known_countries_normalize_to_iso2(name: str, expected: str) -> None:
    assert country_name_to_iso2(name) == expected


@pytest.mark.parametrize("name", ["Atlantis", "", "   ", None])
def test_unknown_or_empty_returns_none(name: str | None) -> None:
    assert country_name_to_iso2(name) is None
