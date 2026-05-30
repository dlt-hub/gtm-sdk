"""Utilities for Fathom recording webhook pipeline."""

import re
from datetime import datetime
from typing import Any

import flatsplode
import orjson

from libs.fathom.models import ActionItem, CalendarInvitee


def clean_timestamp(dt: datetime) -> str:
    """Convert datetime to clean timestamp format."""
    return dt.strftime("%Y%m%d%H%M%S")


def clean_string(s: str) -> str:
    """Clean string for use in filenames."""
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "_", s)
    s = re.sub(r"-+", "-", s)
    return s.lower()


def flatten_recording(recording_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten recording data using flatsplode."""
    flattened = flatsplode.flatsplode(recording_dict)
    if isinstance(flattened, list):
        return flattened
    return list(flattened)


def generate_row_id(recording_id: int, index: int) -> str:
    """Generate unique row ID for flattened record."""
    return f"{recording_id}-{index:05d}"


def recording_to_jsonl(recording_dict: dict[str, Any], recording_id: int) -> str:
    """Convert recording to JSONL with recording_id and per-row id injected."""
    flattened_rows = flatten_recording(recording_dict)
    lines = []
    for i, row in enumerate(flattened_rows):
        row["recording_id"] = recording_id
        row["id"] = generate_row_id(recording_id, i)
        lines.append(orjson.dumps(row).decode("utf-8"))
    return "\n".join(lines) + "\n"


def generate_gcs_filename(
    recording_start_time: datetime,
    recording_id: int,
    meeting_title: str,
) -> str:
    """Generate GCS filename: {clean_timestamp}-{recording_id}-{clean_title}.jsonl"""
    timestamp = clean_timestamp(recording_start_time)
    clean_title = clean_string(meeting_title)
    return f"{timestamp}-{recording_id}-{clean_title}.jsonl"


def _format_timestamp_label(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    parts = timestamp.split(":")
    if len(parts) not in (2, 3):
        return None
    if not all(p.isdigit() for p in parts):
        return None
    return timestamp


def _format_playback_link(url: str | None, timestamp: str | None) -> str | None:
    if not url or not url.startswith("https://"):
        return None
    label = _format_timestamp_label(timestamp)
    if label is None:
        return None
    return f"[▶ {label}]({url})"


def render_action_items_markdown(items: list[ActionItem]) -> str:
    """Render Fathom action items as a markdown checklist.

    Output shape::

        - [ ] **Alex Doe** — Send deck. [▶ 12:34](https://fathom.video/...)
        - [x] **Sarah Lee** (sarah@example.com) — Confirm budget.

    Rules:
    - ``[x]`` if completed, else ``[ ]``.
    - Bold the assignee name; append ``(email)`` when present.
    - Append a ``[▶ MM:SS](url)`` link only when the URL is https and the
      timestamp parses as ``HH:MM:SS`` or ``MM:SS``.
    - Drop items where both ``description`` and ``assignee.name`` are blank
      (defensive — Fathom occasionally emits empty rows).
    """
    rendered_lines: list[str] = []
    for item in items:
        description = (item.description or "").strip()
        name = (item.assignee.name or "").strip()
        if not description and not name:
            continue

        marker = "[x]" if item.completed else "[ ]"
        who = f"**{name}**" if name else "**(unassigned)**"
        if item.assignee.email:
            who = f"{who} ({item.assignee.email})"

        body = description or "(no description)"
        line = f"- {marker} {who} — {body}"

        link = _format_playback_link(
            item.recording_playback_url,
            item.recording_timestamp,
        )
        if link:
            line = f"{line} {link}"
        rendered_lines.append(line)

    return "\n".join(rendered_lines)


def build_meeting_description(
    *,
    summary_markdown: str | None,
    fallback_title: str,
    recording_url: str | None,
    recording_id: int,
    transcript_language: str | None,
) -> str:
    """Compose the Attio Meeting ``description`` from Fathom metadata.

    Attio's Meeting object has a constrained schema — ``description`` is the
    only free-text field, so it is where Fathom metadata without a native home
    (the recording link, Fathom recording id, language) is surfaced.

    Layout: the summary markdown (or the meeting title when no summary exists)
    as the body, then a ``---`` rule and a one-line source footer linking back
    to the Fathom recording. Keeping the summary in the description matters
    while notes-on-meetings is broken (ai-gez): the description is currently the
    only place a teammate can read the summary inside Attio.
    """
    body = (summary_markdown or "").strip() or fallback_title
    footer = _recording_source_line(
        recording_url=recording_url,
        recording_id=recording_id,
        transcript_language=transcript_language,
    )
    return f"{body}\n\n---\n{footer}"


def _recording_source_line(
    *,
    recording_url: str | None,
    recording_id: int,
    transcript_language: str | None,
) -> str:
    """One-line provenance footer linking back to the Fathom recording."""
    parts: list[str] = []
    if recording_url and recording_url.startswith("https://"):
        parts.append(f"🎥 [Watch the Fathom recording]({recording_url})")
    parts.append(f"Fathom recording #{recording_id}")
    language = (transcript_language or "").strip()
    if language:
        parts.append(f"language: {language}")
    return " · ".join(parts)


def fathom_summary_title(template_name: str | None) -> str:
    """Build the title used for the Fathom-summary UpsertNote.

    Fathom ships templates like "General", "Sales Discovery", etc. When
    present, surface the template in the title so a teammate scanning the
    Attio note list immediately recognises which summary template produced it.
    """
    name = (template_name or "").strip()
    if not name:
        return "Fathom summary"
    return f"Fathom summary — {name}"


def select_note_parent_email(
    *,
    calendar_invitees: list[CalendarInvitee],
    participant_emails: list[str],
    recorder_email: str,
) -> str:
    """Pick the email of the Person that Fathom notes should hang off.

    Attio notes cannot be parented to a meeting (ai-gez), so the summary /
    action-item notes hang off a Person record and are associated to the
    meeting via ``meeting_id``. The ``/v2/meetings`` upsert auto-creates a
    Person only for the emails it receives in ``participants`` — so the parent
    MUST be one of ``participant_emails``, or the dispatcher cannot resolve it
    and the whole export fails. ``participant_emails`` is never empty: it is the
    calendar invitees, or the recorder as the sole fallback participant.

    Preference order, all constrained to the participant set:
    1. the first **external** invitee (the prospect/customer the call is about),
    2. else the recorder (internal host) when present,
    3. else the first participant.
    """
    for invitee in calendar_invitees:
        if (
            invitee.is_external
            and invitee.email.strip()
            and invitee.email in participant_emails
        ):
            return invitee.email
    if recorder_email in participant_emails:
        return recorder_email
    return participant_emails[0]
