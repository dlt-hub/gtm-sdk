"""``to_attio_operations`` — op shape, idempotency key, links, note parent."""

from __future__ import annotations

import json
from pathlib import Path

from libs.fireflies import FirefliesRecording, from_motherduck_row
from libs.meetings import canonical_meeting_uid
from src.attio.ops import AttioOp, CompanyRef, PersonRef, UpsertMeeting, UpsertNote
from src.fireflies import SUMMARY_NOTE_TITLE, to_attio_operations

FIXTURE = (
    Path(__file__).parents[2]
    / "libs"
    / "fireflies"
    / "fixtures"
    / "transcript_row.json"
)


def _recording() -> FirefliesRecording:
    return from_motherduck_row(json.loads(FIXTURE.read_text()))


def _meeting(ops: list[AttioOp]) -> UpsertMeeting:
    op = ops[0]
    assert isinstance(op, UpsertMeeting)
    return op


def _note(ops: list[AttioOp]) -> UpsertNote:
    note = next(op for op in ops if isinstance(op, UpsertNote))
    return note


def test_emits_meeting_and_summary_note() -> None:
    ops = to_attio_operations(_recording())
    assert isinstance(ops[0], UpsertMeeting)
    notes = [op for op in ops if isinstance(op, UpsertNote)]
    assert len(notes) == 1
    assert notes[0].title == SUMMARY_NOTE_TITLE


def test_ical_uid_matches_canonical_and_is_deterministic() -> None:
    rec = _recording()
    ops = to_attio_operations(rec)
    expected = canonical_meeting_uid(host_email=rec.host_email, start=rec.start)
    assert _meeting(ops).external_ref.ical_uid == expected
    # The note is associated to the same meeting.
    note = _note(ops)
    assert note.meeting is not None
    assert note.meeting.ical_uid == expected


def test_participants_and_organizer_flag() -> None:
    meeting = _meeting(to_attio_operations(_recording()))
    by_email = {p.email_address: p for p in meeting.participants}
    assert by_email["viredacted@dlthub.com"].is_organizer is True
    assert by_email["siredacted@example.com"].is_organizer is False


def test_company_links_exclude_org_domains() -> None:
    meeting = _meeting(to_attio_operations(_recording()))
    companies = {
        ref.domain for ref in meeting.linked_records if isinstance(ref, CompanyRef)
    }
    assert companies == {"example.com"}  # dlthub.com excluded as internal
    persons = {
        ref.value for ref in meeting.linked_records if isinstance(ref, PersonRef)
    }
    assert persons == {"siredacted@example.com", "viredacted@dlthub.com"}


def test_host_always_a_participant_even_if_absent_from_attendees() -> None:
    # Attendees that omit the host must still yield the host as organizer.
    rec = _recording().model_copy(
        update={
            "host_email": "host@dlthub.com",
            "attendee_emails": ["siredacted@example.com"],
        },
    )
    meeting = _meeting(to_attio_operations(rec))
    by_email = {p.email_address: p for p in meeting.participants}
    assert "host@dlthub.com" in by_email
    assert by_email["host@dlthub.com"].is_organizer is True


def test_personal_mailbox_domains_are_not_company_links() -> None:
    rec = _recording().model_copy(
        update={
            "host_email": "host@dlthub.com",
            "attendee_emails": ["someone@gmail.com", "buyer@acme.com"],
        },
    )
    meeting = _meeting(to_attio_operations(rec))
    companies = {
        ref.domain for ref in meeting.linked_records if isinstance(ref, CompanyRef)
    }
    assert companies == {"acme.com"}  # gmail.com excluded as a personal mailbox


def test_note_parent_prefers_external_participant() -> None:
    note = _note(to_attio_operations(_recording()))
    assert isinstance(note.parent, PersonRef)
    assert note.parent.value == "siredacted@example.com"


def test_no_notes_emits_meeting_only() -> None:
    ops = to_attio_operations(_recording(), include_notes=False)
    assert len(ops) == 1
    assert isinstance(ops[0], UpsertMeeting)


def test_no_attendees_falls_back_to_host() -> None:
    rec = _recording().model_copy(update={"attendee_emails": []})
    meeting = _meeting(to_attio_operations(rec))
    assert len(meeting.participants) == 1
    assert meeting.participants[0].email_address == rec.host_email
    assert meeting.participants[0].is_organizer is True


def test_gist_only_still_emits_note() -> None:
    # A transcript with only the bullet gist populated must not lose its summary.
    rec = _recording().model_copy(
        update={
            "summary_overview": None,
            "summary_action_items": None,
            "summary_short_summary": None,
            "summary_bullet_gist": "🤝 Partnership opportunity discussed.",
        },
    )
    note = _note(to_attio_operations(rec))
    assert "Partnership opportunity" in note.content
    assert "Partnership opportunity" in _meeting(to_attio_operations(rec)).description


def test_empty_summary_skips_note() -> None:
    rec = _recording().model_copy(
        update={
            "summary_overview": None,
            "summary_action_items": None,
            "summary_bullet_gist": None,
            "summary_short_summary": None,
        },
    )
    ops = to_attio_operations(rec)
    assert all(not isinstance(op, UpsertNote) for op in ops)
    # Description falls back to the title when there is no summary.
    assert _meeting(ops).description.startswith(rec.title)
