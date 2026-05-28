"""Country-name → ISO-3166-1 alpha-2 normalization.

Providers (Harvest, LinkedIn, CRM exports) hand back free-text country names
like ``"United States"`` or informal variants like ``"USA"``/``"UK"``. Attio's
``format_location`` requires an ISO-3166-1 alpha-2 code by contract (see ai-sfp)
and refuses to emit a location without one — so these names must be normalized
before they reach the writer. ``country_name_to_iso2`` returns ``None`` for
empty or unrecognized input rather than guessing, preserving the
no-silent-default contract: the caller skips the write instead of stamping a
wrong country.
"""

from __future__ import annotations

import pycountry

# Informal spellings and abbreviations that pycountry's ISO names miss. Keys are
# lowercased/stripped. Extend this when a real provider spelling falls through to
# None — never reach for pycountry's fuzzy search, which can match surprising
# countries and reintroduce silent misattribution.
_ALIASES: dict[str, str] = {
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "england": "GB",
    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "uae": "AE",
    "czech republic": "CZ",
}


def country_name_to_iso2(name: str | None) -> str | None:
    """Map a free-text country name to ISO-3166-1 alpha-2, or None if unknown.

    Used to normalize provider-supplied country names (e.g. Harvest's
    ``"United States"``, ``"India"``) before they reach ``format_location``,
    which requires alpha-2 by contract (see ai-sfp). Returns ``None`` for empty
    or unrecognized input — never a silent default — so callers skip the write.
    """
    if not name:
        return None

    key = name.strip().lower()
    if not key:
        return None

    if key in _ALIASES:
        return _ALIASES[key]

    try:
        # Case-insensitive across alpha_2/alpha_3/name/official_name/common_name.
        return pycountry.countries.lookup(name.strip()).alpha_2
    except LookupError:
        return None
