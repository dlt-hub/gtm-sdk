"""End-to-end replay test for the Fathom → Attio dispatcher path.

Replays the same Fathom webhook plan twice through ``execute()`` with the
lib-side calls monkeypatched. Asserts that note creation is idempotent —
duplicate deliveries don't create duplicate notes on the same meeting.
"""

from __future__ import annotations

from pathlib import Path

import orjson
from libs.attio.contracts import ReliabilityEnvelope
from src.attio.export import execute
from src.fathom.webhook.call import Webhook

FIXTURE = Path("api/samples/fathom.recording.redacted.json")


class _StubNote:
    def __init__(self, note_id: str, title: str, meeting_id: str | None) -> None:
        self.note_id = note_id
        self.title = title
        self.meeting_id = meeting_id


def _meeting_envelope(record_id: str) -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action="created",
        record_id=record_id,
        errors=[],
        warnings=[],
        skipped_fields=[],
        meta={"output_schema_version": "v1"},
    )


def test_replay_does_not_create_duplicate_notes(monkeypatch) -> None:
    """Running the same Fathom plan twice creates each note exactly once.

    The dispatcher must look up existing notes by title on the parent before
    creating, so retried webhook deliveries idempotently no-op.
    """
    webhook = Webhook.model_validate(orjson.loads(FIXTURE.read_bytes()))
    plan = webhook.attio_get_operations()
    # Sanity: fixture must exercise both summary and action-items notes.
    note_titles_in_plan = [
        op.title for op in plan if hasattr(op, "title") and op.op_type == "upsert_note"
    ]
    assert len(note_titles_in_plan) >= 1, (
        "Fixture should produce at least one UpsertNote op"
    )

    # Track notes "stored" in Attio across both replay passes.
    stored_notes: dict[str, list[_StubNote]] = {}
    add_calls: list[tuple[str, str]] = []
    next_note_id = iter(f"note-{i}" for i in range(1, 100))

    def fake_find_or_create_meeting(meeting_input):  # noqa: ANN001, ANN202
        return _meeting_envelope("meet-rec-1")

    def fake_list_notes(*, parent_object, parent_record_id):  # noqa: ANN001, ANN202
        return list(stored_notes.get(parent_record_id, []))

    def fake_add_note(note_input):  # noqa: ANN001, ANN202
        note_id = next(next_note_id)
        note = _StubNote(
            note_id=note_id,
            title=note_input.title,
            meeting_id=note_input.meeting_id,
        )
        stored_notes.setdefault(note_input.parent_record_id, []).append(note)
        add_calls.append((note_input.parent_record_id, note_input.title))
        return note

    monkeypatch.setattr(
        "src.attio.export.find_or_create_meeting",
        fake_find_or_create_meeting,
    )
    monkeypatch.setattr(
        "src.attio.export.libs_list_notes_for_parent",
        fake_list_notes,
    )
    monkeypatch.setattr("src.attio.export.libs_add_note", fake_add_note)

    # Fathom emits no UpsertPerson op — the participant Person is auto-created by
    # the /v2/meetings upsert — so the note parent resolves via a live lookup.
    def fake_resolve_record_id(**_kwargs: str) -> str:  # noqa: ANN003 handled by **_kwargs
        return "person-rec-1"

    monkeypatch.setattr(
        "src.attio.export.libs_resolve_record_id_for_ref",
        fake_resolve_record_id,
    )

    # First delivery: notes are created.
    first = execute(plan)
    assert first.success is True
    note_outcomes_1 = [o for o in first.outcomes if o.op_type == "UpsertNote"]
    assert len(note_outcomes_1) == len(note_titles_in_plan)
    for outcome in note_outcomes_1:
        assert outcome.envelope.action == "created"

    add_call_count_after_first = len(add_calls)
    assert add_call_count_after_first == len(note_titles_in_plan)

    # Second delivery (replay): notes already exist → all skipped, no new writes.
    second = execute(plan)
    assert second.success is True
    note_outcomes_2 = [o for o in second.outcomes if o.op_type == "UpsertNote"]
    assert len(note_outcomes_2) == len(note_titles_in_plan)
    for outcome in note_outcomes_2:
        assert outcome.envelope.action == "noop"

    # Crucially: no additional note creations on replay.
    assert len(add_calls) == add_call_count_after_first
    # Each title in the plan was created at most once across both runs.
    created_titles = [title for (_pid, title) in add_calls]
    assert sorted(created_titles) == sorted(note_titles_in_plan)
