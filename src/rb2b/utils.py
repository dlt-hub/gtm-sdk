"""Utilities for rb2b visit webhook pipeline."""

import re
from datetime import datetime
from typing import Any

import flatsplode
import orjson


def clean_timestamp(dt: datetime) -> str:
    """Convert datetime to clean timestamp format."""
    return dt.strftime("%Y%m%d%H%M%S")


def clean_string(s: str) -> str:
    """Clean string for use in filenames."""
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "_", s)
    s = re.sub(r"-+", "-", s)
    return s.lower()


def flatten_event(event_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten event data using flatsplode."""
    flattened = flatsplode.flatsplode(event_dict)
    if isinstance(flattened, list):
        return flattened
    return list(flattened)


def generate_row_id(event_id: str, index: int) -> str:
    """Generate unique row ID for flattened record."""
    return f"{event_id}-{index:05d}"


def event_to_jsonl(event_dict: dict[str, Any], event_id: str) -> str:
    """Convert event to JSONL with event_id and per-row id injected."""
    flattened_rows = flatten_event(event_dict)
    lines = []
    for i, row in enumerate(flattened_rows):
        row["event_id"] = event_id
        row["id"] = generate_row_id(event_id, i)
        lines.append(orjson.dumps(row).decode("utf-8"))
    return "\n".join(lines) + "\n"


def generate_gcs_filename(
    timestamp: datetime,
    event_id: str,
    company_name: str | None,
) -> str:
    """Generate GCS filename: {clean_timestamp}-{event_id}-{clean_company}.jsonl"""
    ts = clean_timestamp(timestamp)
    company = clean_string(company_name) if company_name else "unknown"
    return f"{ts}-{event_id}-{company}.jsonl"


def split_rb2b_tags(raw: str | None) -> list[str]:
    """Split an rb2b ``Tags`` value into a deduped list of tag strings.

    rb2b ships tags as a single string field (libs/rb2b/models.py:85,
    ``tags: str | None = Field(default=None, alias="Tags")``).

    Wire format observed in api/samples/rb2b.visit.*.json (audited 2026-05-17,
    7 fixtures): plain comma-separated, no quoting, no embedded commas. If
    rb2b ever ships richer values (quoted segments, escapes), revisit and
    switch to ``csv.reader([raw])`` with the excel dialect.

    Behavior:
    - ``None`` / empty / whitespace-only → ``[]``.
    - Strips whitespace around each token.
    - Drops empty tokens (handles ``",,,"`` and trailing commas).
    - Dedupes case-insensitively, keeping first-seen casing
      (mirrors libs/attio/values.py:36-51 ``normalize_email_address_list``).
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out
