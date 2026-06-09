"""Presentation helpers for the Fireflies → Attio backfill.

The Fathom equivalents (``src/fathom/utils.py``) are deliberately not reused:
their provenance footer is Fathom-branded, and Fathom carries a single
pre-rendered ``markdown_formatted`` summary whereas Fireflies splits its summary
across several columns (overview / action items / gist / short summary). Keeping
a Fireflies-specific builder keeps the Attio meeting's provenance honest.
"""

from __future__ import annotations

from libs.fireflies import FirefliesRecording


def fireflies_summary_markdown(rec: FirefliesRecording) -> str:
    """Compose a single markdown summary from Fireflies' split summary columns.

    Sections are emitted only when present so a sparse transcript does not grow
    empty headers. Returns an empty string when the transcript has no summary at
    all (the caller then skips both the description body and the summary note).
    """
    sections: list[str] = []
    # overview / short_summary / bullet_gist all describe the call and overlap
    # heavily, so render exactly one as the summary body to avoid near-duplicate
    # text — but fall back through all three so a transcript with only the gist
    # populated still gets a summary (and a summary note) instead of a title-only
    # description.
    body = rec.summary_overview or rec.summary_short_summary or rec.summary_bullet_gist
    if body:
        sections.append(f"## Overview\n\n{body.strip()}")
    if rec.summary_action_items:
        sections.append(f"## Action items\n\n{rec.summary_action_items.strip()}")
    return "\n\n".join(sections)


def build_meeting_description(rec: FirefliesRecording, *, summary_markdown: str) -> str:
    """Compose the Attio Meeting ``description`` from Fireflies metadata.

    Mirrors ``src/fathom/utils.build_meeting_description``: the summary markdown
    (or the title as a fallback) forms the body, followed by a ``---`` rule and a
    one-line provenance footer linking back to the Fireflies recording. Attio's
    Meeting object has no native summary field, so the description is where this
    metadata lives.
    """
    body = summary_markdown.strip() or rec.title
    footer = _recording_source_line(rec)
    return f"{body}\n\n---\n{footer}"


def _recording_source_line(rec: FirefliesRecording) -> str:
    parts: list[str] = []
    if rec.recording_url and rec.recording_url.startswith("https://"):
        parts.append(f"🎥 [Watch the Fireflies recording]({rec.recording_url})")
    parts.append(f"Fireflies recording {rec.id}")
    return " · ".join(parts)


def select_note_parent_email(
    *,
    participant_emails: list[str],
    host_email: str,
    org_domains: frozenset[str],
) -> str:
    """Pick the email of the Person the Fireflies summary note should hang off.

    Attio notes cannot be parented to a meeting (ai-gez): the note hangs off a
    Person and is associated to the meeting via ``meeting_id``. The
    ``/v2/meetings`` upsert only auto-creates Persons for the emails passed as
    ``participants``, so the parent MUST be one of ``participant_emails``.

    Preference order, all constrained to the participant set:
    1. the first **external** participant (the prospect/customer the call is about),
    2. else the host when present,
    3. else the first participant.
    """
    for email in participant_emails:
        domain = email.rsplit("@", 1)[-1]
        if domain not in org_domains:
            return email
    if host_email in participant_emails:
        return host_email
    return participant_emails[0]
