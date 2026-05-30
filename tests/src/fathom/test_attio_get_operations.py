from __future__ import annotations

from datetime import datetime
from pathlib import Path

import orjson

from libs.fathom.models import ActionItem, Assignee
from libs.meetings import canonical_meeting_uid
from src.attio.ops import (
    UpsertNote,
    MeetingExternalRef,
    MeetingRef,
    PersonRef,
    UpsertMeeting,
)
from src.fathom.webhook.call import Webhook


def _action_item(
    *,
    name: str = "Alex",
    description: str = "Send deck",
    completed: bool = False,
) -> ActionItem:
    return ActionItem(
        assignee=Assignee(name=name, email=None, team=None),
        completed=completed,
        description=description,
        recording_playback_url="https://fathom.video/calls/1/?t=754",
        recording_timestamp="12:34",
        user_generated=False,
    )


FIXTURE = Path("api/samples/fathom.recording.redacted.json")


def _load() -> Webhook:
    payload = orjson.loads(FIXTURE.read_bytes())
    return Webhook.model_validate(payload)


def test_required_api_keys() -> None:
    assert Webhook.required_api_keys() == ["ATTIO_API_KEY"]


def test_attio_is_valid_webhook_true_for_normal_payload() -> None:
    assert _load().attio_is_valid_webhook() is True


def test_attio_is_valid_webhook_true_with_no_attendees() -> None:
    """Ad-hoc Fathom recordings (no calendar invitees) are still exportable."""
    w = _load()
    w.calendar_invitees = []
    assert w.attio_is_valid_webhook() is True


def test_attio_get_operations_falls_back_to_recorder_with_no_attendees() -> None:
    w = _load()
    w.calendar_invitees = []
    plan = w.attio_get_operations()
    op = plan[0]
    assert isinstance(op, UpsertMeeting)
    assert len(op.participants) == 1
    assert op.participants[0].email_address == "host@dlthub.com"
    assert op.participants[0].is_organizer is True

    # With no external invitee the note parent falls back to the recorder, so
    # the summary/action-item notes still have a valid (non-meeting) parent.
    note = plan[1]
    assert isinstance(note, UpsertNote)
    assert isinstance(note.parent, PersonRef)
    assert note.parent.value == "host@dlthub.com"
    assert isinstance(note.meeting, MeetingRef)


def test_attio_is_valid_webhook_false_with_no_recording_id() -> None:
    w = _load()
    w.recording_id = 0
    assert w.attio_is_valid_webhook() is False


def test_attio_get_operations_returns_meeting_and_summary() -> None:
    plan = _load().attio_get_operations()

    assert len(plan) == 2
    op = plan[0]
    assert isinstance(op, UpsertMeeting)
    assert isinstance(plan[1], UpsertNote)

    assert isinstance(op.external_ref, MeetingExternalRef)
    expected = canonical_meeting_uid(
        host_email="host@dlthub.com",
        start=datetime.fromisoformat("2026-05-12T14:00:00+00:00"),
    )
    assert op.external_ref.ical_uid == expected
    assert op.external_ref.provider == "google"
    assert op.external_ref.is_recurring is False

    assert op.title == "Internal sync"
    assert op.description.startswith("## Summary")
    # The recording-provenance footer is appended after the summary body and
    # prefers the shareable link over the internal /calls page.
    assert "🎥 [Watch the Fathom recording](https://fathom.video/share/test)" in (
        op.description
    )
    assert "Fathom recording #999999" in op.description
    assert "language: en" in op.description
    assert op.is_all_day is False

    emails = [p.email_address for p in op.participants]
    assert "host@dlthub.com" in emails
    assert "external@example.com" in emails

    organizers = [p for p in op.participants if p.is_organizer]
    assert len(organizers) == 1
    assert organizers[0].email_address == "host@dlthub.com"


def test_attio_get_operations_falls_back_when_default_summary_missing() -> None:
    w = _load()
    w.default_summary = None
    plan = w.attio_get_operations()
    assert len(plan) == 1
    op = plan[0]
    assert isinstance(op, UpsertMeeting)
    # description falls back to meeting_title (or title) as the body, then the
    # recording footer is appended.
    assert op.description.startswith("Internal sync")
    assert "Fathom recording #999999" in op.description


def test_plan_includes_summary_note() -> None:
    w = _load()
    assert w.default_summary is not None
    plan = w.attio_get_operations()

    assert len(plan) == 2
    note = plan[1]
    assert isinstance(note, UpsertNote)
    assert note.title.startswith("Fathom summary")
    assert note.content == w.default_summary.markdown_formatted

    upsert = plan[0]
    assert isinstance(upsert, UpsertMeeting)
    # ai-gez: the note is parented to the primary external participant and
    # *associated* to the meeting via ``meeting`` (never parented to it).
    assert isinstance(note.parent, PersonRef)
    assert note.parent.attribute == "email"
    assert note.parent.value == "external@example.com"
    assert isinstance(note.meeting, MeetingRef)
    assert note.meeting.ical_uid == upsert.external_ref.ical_uid


def test_plan_skips_summary_when_missing() -> None:
    w = _load()
    w.default_summary = None
    plan = w.attio_get_operations()
    assert len(plan) == 1
    assert isinstance(plan[0], UpsertMeeting)


def test_plan_skips_summary_when_empty_markdown() -> None:
    w = _load()
    assert w.default_summary is not None
    w.default_summary.markdown_formatted = "   "
    plan = w.attio_get_operations()
    assert len(plan) == 1
    assert isinstance(plan[0], UpsertMeeting)


def test_plan_includes_action_items_note() -> None:
    w = _load()
    w.action_items = [
        _action_item(name="Alex", description="Send deck", completed=False),
        _action_item(name="Sarah", description="Confirm budget", completed=True),
        _action_item(name="Jamie", description="Schedule follow-up", completed=False),
    ]
    plan = w.attio_get_operations()

    assert len(plan) == 3
    note = plan[2]
    assert isinstance(note, UpsertNote)
    assert note.title == "Action items"
    assert "Send deck" in note.content
    assert "Confirm budget" in note.content
    assert "Schedule follow-up" in note.content
    assert "[ ]" in note.content
    assert "[x]" in note.content

    upsert = plan[0]
    assert isinstance(upsert, UpsertMeeting)
    assert isinstance(note.parent, PersonRef)
    assert note.parent.value == "external@example.com"
    assert isinstance(note.meeting, MeetingRef)
    assert note.meeting.ical_uid == upsert.external_ref.ical_uid


def test_plan_skips_action_items_when_empty_list() -> None:
    w = _load()
    w.action_items = []
    plan = w.attio_get_operations()
    assert len(plan) == 2
    assert isinstance(plan[0], UpsertMeeting)
    assert isinstance(plan[1], UpsertNote)
    assert plan[1].title.startswith("Fathom summary")


def test_plan_skips_action_items_when_none() -> None:
    w = _load()
    w.action_items = None
    plan = w.attio_get_operations()
    assert len(plan) == 2
    assert isinstance(plan[1], UpsertNote)
    assert plan[1].title.startswith("Fathom summary")
