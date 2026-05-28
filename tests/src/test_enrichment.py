"""Tests for src/enrichment.py — Harvest → Attio PersonInput conversion.

Note: tests/src/attio/test_enrichment.py covers the unrelated
src/attio/enrichment.py company-domain backfill orchestrator. This file targets
the Harvest LinkedIn enrichment path in the top-level src/enrichment.py module.
"""

from __future__ import annotations

from src.enrichment import HarvestProfile, profile_to_person_input


def test_known_country_writes_iso2_country_code() -> None:
    """Harvest's free-text country normalizes to ISO-2 so primary_location can
    be written downstream (see ai-862 / ai-sfp)."""
    profile = HarvestProfile(
        firstName="Ada",
        lastName="Lovelace",
        location={"city": "London", "state": "England", "country": "United States"},
    )

    person_input = profile_to_person_input(profile, "ada@example.com")

    assert person_input.country_code == "US"
    assert person_input.location == "London, England"


def test_unknown_country_leaves_country_code_none() -> None:
    """Unrecognized country names fall through to None — no silent default — so
    the downstream writer skips primary_location rather than misattributing it."""
    profile = HarvestProfile(
        firstName="Marie",
        lastName="Curie",
        location={"city": "Atlantis City", "state": "Atlantis", "country": "Atlantis"},
    )

    person_input = profile_to_person_input(profile, "marie@example.com")

    assert person_input.country_code is None
    # Locality/region tokens are still populated for search/lookup paths.
    assert person_input.location == "Atlantis City, Atlantis"
