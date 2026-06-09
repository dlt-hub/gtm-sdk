"""``from_motherduck_row`` mapping, validated against a redacted real row."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from libs.fireflies import from_motherduck_row

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_row.json"


def _row() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_maps_real_row() -> None:
    rec = from_motherduck_row(_row())

    assert rec.id == "01KCGKYHB2W11WTCRRS3HREJS1"
    assert rec.title == "dltHub <> Simon"
    # date 1765890000000 ms == 2025-12-16T13:00:00Z, tz-aware UTC.
    assert rec.start == datetime(2025, 12, 16, 13, 0, tzinfo=UTC)
    # duration 32.84 min after start.
    assert (rec.end - rec.start).total_seconds() == pytest.approx(32.84 * 60)
    assert rec.host_email == "viredacted@dlthub.com"
    assert rec.attendee_emails == ["siredacted@example.com", "viredacted@dlthub.com"]
    assert rec.recording_url == (
        "https://app.fireflies.ai/view/01KCGKYHB2W11WTCRRS3HREJS1"
    )
    assert rec.summary_overview is not None


def test_host_falls_back_to_organizer() -> None:
    row = _row()
    row["host_email"] = None
    rec = from_motherduck_row(row)
    assert rec.host_email == row["organizer_email"]


def test_emails_normalized_and_deduped() -> None:
    row = _row()
    row["attendees"] = [
        {"email": "  Foo@Example.com  ", "display_name": None},
        {"email": "foo@example.com", "display_name": None},  # dup after casefold
        {"email": "a@b.com,c@d.com", "display_name": None},  # comma-joined → dropped
        {"email": "", "display_name": None},
    ]
    rec = from_motherduck_row(row)
    assert rec.attendee_emails == ["foo@example.com"]


def test_missing_id_raises() -> None:
    row = _row()
    row["id"] = ""
    with pytest.raises(ValueError, match="no id"):
        from_motherduck_row(row)


def test_missing_date_raises() -> None:
    row = _row()
    row["date"] = None
    with pytest.raises(ValueError, match="no date"):
        from_motherduck_row(row)


def test_no_resolvable_host_raises() -> None:
    row = _row()
    row["host_email"] = None
    row["organizer_email"] = ""
    with pytest.raises(ValueError, match="no resolvable host"):
        from_motherduck_row(row)
