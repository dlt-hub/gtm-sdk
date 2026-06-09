"""Fireflies transcript domain model + MotherDuck-row mapper.

The Fireflies recordings were exported (via dlt) into a personal MotherDuck
database, so unlike Fathom there is no live API: the "source" is normalised
rows. This module is the pure, dependency-light boundary — it turns one
assembled MotherDuck row (the ``transcript_details`` parent columns plus its
joined ``meeting_attendees``) into a normalised :class:`FirefliesRecording`.

Per the repo's code-placement rules this stays a pure transform: it must NOT
import ``libs.motherduck`` (no cross-lib imports) and does no I/O. The
orchestration that reads MotherDuck and feeds rows in lives in ``src/fireflies``.

Observed live schema (122 rows, 2025-12 → 2026-04), captured during the Step-0
probe and pinned as the ``tests/libs/fireflies`` fixture:

- ``date`` is epoch **milliseconds** (UTC).
- ``duration`` is **minutes** (float) → ``end = start + minutes``.
- ``host_email`` is occasionally blank but ``organizer_email`` is always
  populated, so the meeting host is ``host_email or organizer_email`` and is
  always resolvable.
- attendee emails live one-per-row in ``transcript_details__meeting_attendees``
  (the sibling ``__participants`` table is dirty — comma-joined values — so it
  is intentionally ignored).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict


def _normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().casefold()
    # Defensive: a couple of source rows comma-join addresses into one cell.
    # Such a value is not a single address; drop it rather than guess.
    if "@" not in email or "," in email or " " in email:
        return None
    return email


class FirefliesRecording(BaseModel):
    """Normalised Fireflies transcript, ready to map to Attio meeting ops."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    start: datetime
    end: datetime
    # Resolved host (host_email or organizer_email). Always present — it is the
    # ``canonical_meeting_uid`` host and the participant fallback.
    host_email: str
    attendee_emails: list[str]
    recording_url: str | None
    summary_overview: str | None
    summary_action_items: str | None
    summary_bullet_gist: str | None
    summary_short_summary: str | None


def from_motherduck_row(row: dict[str, Any]) -> FirefliesRecording:
    """Map one assembled ``transcript_details`` row to a ``FirefliesRecording``.

    ``row`` carries the parent columns plus an ``attendees`` key holding the
    joined ``meeting_attendees`` (a list of dicts with an ``email``). Raises
    ``ValueError`` for rows we cannot anchor (no id, no usable timestamp, or no
    resolvable host) — the caller skips and reports those rather than aborting.
    """
    rec_id = (row.get("id") or "").strip()
    if not rec_id:
        raise ValueError("transcript row has no id")

    epoch_ms = row.get("date")
    if epoch_ms is None:
        raise ValueError(f"transcript {rec_id} has no date")
    start = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=UTC)

    duration_minutes = row.get("duration") or 0
    end = start + timedelta(minutes=float(duration_minutes))

    host = _normalize_email(row.get("host_email")) or _normalize_email(
        row.get("organizer_email"),
    )
    if not host:
        raise ValueError(f"transcript {rec_id} has no resolvable host email")

    # Sorted, not insertion-ordered: the attendees come from an unordered SQL
    # read, and downstream note-parent selection ("first external participant")
    # must be stable across reruns or note dedup breaks (the note would re-hang
    # off a different Person each run). Sorting makes the chosen parent
    # deterministic.
    attendee_emails = sorted(
        {
            email
            for attendee in row.get("attendees") or []
            if (
                email := _normalize_email(
                    attendee.get("email") if isinstance(attendee, dict) else attendee,
                )
            )
        },
    )

    def _text(key: str) -> str | None:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
        return None

    return FirefliesRecording(
        id=rec_id,
        title=(row.get("title") or "").strip() or "Untitled Fireflies meeting",
        start=start,
        end=end,
        host_email=host,
        attendee_emails=attendee_emails,
        recording_url=_text("transcript_url"),
        summary_overview=_text("summary__overview"),
        summary_action_items=_text("summary__action_items"),
        summary_bullet_gist=_text("summary__bullet_gist"),
        summary_short_summary=_text("summary__short_summary"),
    )
