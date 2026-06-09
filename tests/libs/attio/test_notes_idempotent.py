from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import AttioError
from libs.attio.models import NoteInput


def _raw_note(
    note_id: str,
    *,
    title: str = "t",
    meeting_id: str | None = None,
) -> SimpleNamespace:
    """A NoteResult-valid stand-in for a raw SDK note (real str attrs)."""
    return SimpleNamespace(
        id=SimpleNamespace(note_id=note_id),
        title=title,
        parent_object="people",
        parent_record_id="pid",
        content_plaintext="",
        created_at="2026-01-01T00:00:00Z",
        meeting_id=meeting_id,
    )


def test_list_notes_for_parent_paginates_until_short_page() -> None:
    # ai-crf: the LIST endpoint returns one short page with no sort control, so
    # the dedup must drain every page or it misses notes past page 1.
    page1 = MagicMock(
        data=[_raw_note(f"n{i}") for i in range(50)],
    )  # full page → keep going
    page2 = MagicMock(data=[_raw_note("n50"), _raw_note("n51")])  # short page → stop
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.side_effect = [page1, page2]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import list_notes_for_parent

        out = list_notes_for_parent("people", "pid")

    assert len(out) == 52
    assert fake_client.notes.get_v2_notes.call_count == 2
    _, kwargs = fake_client.notes.get_v2_notes.call_args_list[1]
    assert kwargs["offset"] == 50
    assert kwargs["limit"] == 50


def test_find_note_by_title_matches_note_past_first_page() -> None:
    # ai-crf regression: a freshly-written note can sort past page 1 on a busy
    # parent (e.g. a Fireflies notetaker with 40+ notes); without pagination the
    # dedup misses it and a re-run recreates a duplicate.
    page1 = MagicMock(
        data=[_raw_note(f"n{i}", title="Other", meeting_id="m-x") for i in range(50)],
    )
    target = _raw_note("note-target", title="Action items", meeting_id="m-THIS")
    page2 = MagicMock(data=[target])
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.side_effect = [page1, page2]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="people",
            parent_record_id="pid",
            title="Action items",
            meeting_id="m-THIS",
        )

    assert out == "note-target"
    assert fake_client.notes.get_v2_notes.call_count == 2


def test_list_notes_for_parent_fails_closed_at_page_cap(monkeypatch) -> None:
    # ai-8tv: if every page is full (server ignores offset / pathological parent),
    # the dedup must raise rather than return a truncated list — silently
    # truncating would reopen the duplicate-note hole this fix closes.
    monkeypatch.setattr("libs.attio.notes._NOTES_MAX_PAGES", 2)
    full_page = MagicMock(data=[_raw_note(f"n{i}") for i in range(50)])
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value = full_page  # never a short page

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import list_notes_for_parent

        with pytest.raises(AttioError, match="page cap"):
            list_notes_for_parent("people", "pid")

    # Cap is 2 full pages, plus one confirming probe that is still full → raise.
    assert fake_client.notes.get_v2_notes.call_count == 3


def test_list_notes_for_parent_drains_exact_multiple_of_page_size(monkeypatch) -> None:
    # ai-8tv off-by-one: a parent whose note count is an exact multiple of the page
    # size (here cap=2 → 100 notes, 2 full pages) must be drained by the confirming
    # empty page, NOT false-fail at the boundary.
    monkeypatch.setattr("libs.attio.notes._NOTES_MAX_PAGES", 2)
    full1 = MagicMock(data=[_raw_note(f"a{i}") for i in range(50)])
    full2 = MagicMock(data=[_raw_note(f"b{i}") for i in range(50)])
    empty = MagicMock(data=[])  # confirming probe → drained, no raise
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.side_effect = [full1, full2, empty]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import list_notes_for_parent

        out = list_notes_for_parent("people", "pid")

    assert len(out) == 100
    assert fake_client.notes.get_v2_notes.call_count == 3


def test_find_note_by_title_returns_none_when_no_match() -> None:
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = []

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="companies",
            parent_record_id="cid",
            title="missing",
        )
    assert out is None


def test_find_note_by_title_returns_existing_note_id() -> None:
    note_match = MagicMock()
    note_match.title = "Snowflake CSV annotation — 2026-05-25"
    note_match.id.note_id = "note-1"
    note_other = MagicMock()
    note_other.title = "Other"
    note_other.id.note_id = "note-2"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [note_other, note_match]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="companies",
            parent_record_id="cid",
            title="Snowflake CSV annotation — 2026-05-25",
        )
    assert out == "note-1"


def test_create_note_skips_post_when_title_matches() -> None:
    note_match = MagicMock()
    note_match.title = "Snowflake CSV annotation — 2026-05-25"
    note_match.id.note_id = "existing-note"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [note_match]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope: ReliabilityEnvelope = create_note(
            input=NoteInput(
                parent_object="companies",
                parent_record_id="cid",
                title="Snowflake CSV annotation — 2026-05-25",
                content="duplicate body",
                format="markdown",
            ),
            apply=True,
        )
    assert envelope.action == "noop"
    assert envelope.record_id == "existing-note"
    fake_client.notes.post_v2_notes.assert_not_called()


def test_create_note_preview_does_not_post() -> None:
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = []

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope = create_note(
            input=NoteInput(
                parent_object="companies",
                parent_record_id="cid",
                title="t",
                content="body",
                format="markdown",
                created_at=datetime(2025, 1, 5, tzinfo=UTC),
            ),
            apply=False,
        )
    assert envelope.action == "noop"
    assert envelope.record_id is None
    fake_client.notes.post_v2_notes.assert_not_called()


def test_resolve_record_id_for_ref_retries_then_succeeds(monkeypatch) -> None:
    # ai-gez: the participant Person was just auto-created by /v2/meetings, so
    # Attio's record search can lag. A miss must retry before giving up.
    empty = MagicMock()
    empty.data = []
    found = MagicMock()
    found.data = [MagicMock()]
    found.data[0].id.record_id = "person-late"
    fake_client = MagicMock()
    # First query lags (no data), second returns the record.
    fake_client.records.post_v2_objects_object_records_query.side_effect = [
        empty,
        found,
    ]

    sleeps: list[float] = []
    with (
        patch("libs.attio.notes.get_client") as mock_ctx,
        patch(
            "libs.attio.notes.time.sleep",
            side_effect=sleeps.append,
        ),
    ):
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import resolve_record_id_for_ref

        out = resolve_record_id_for_ref(parent_object="people", email="late@acme.com")

    assert out == "person-late"
    assert len(sleeps) == 1  # one backoff between the two attempts


def test_resolve_record_id_for_ref_returns_none_after_attempts(monkeypatch) -> None:
    empty = MagicMock()
    empty.data = []
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value = empty

    with (
        patch("libs.attio.notes.get_client") as mock_ctx,
        patch(
            "libs.attio.notes.time.sleep",
        ),
    ):
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import resolve_record_id_for_ref

        out = resolve_record_id_for_ref(
            parent_object="people",
            email="never@acme.com",
            attempts=2,
        )

    assert out is None
    assert fake_client.records.post_v2_objects_object_records_query.call_count == 2


def test_find_note_by_title_scopes_match_to_meeting_id() -> None:
    # ai-gez: a shared parent accumulates same-titled notes across meetings, so
    # a meeting-scoped lookup must ignore a same-title note from another meeting.
    other_meeting = MagicMock()
    other_meeting.title = "Action items"
    other_meeting.meeting_id = "meet-OTHER"
    other_meeting.id.note_id = "note-other"
    this_meeting = MagicMock()
    this_meeting.title = "Action items"
    this_meeting.meeting_id = "meet-THIS"
    this_meeting.id.note_id = "note-this"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [other_meeting, this_meeting]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="people",
            parent_record_id="pid",
            title="Action items",
            meeting_id="meet-THIS",
        )
    assert out == "note-this"


def test_find_note_by_title_meeting_scope_no_match_for_other_meeting() -> None:
    existing = MagicMock()
    existing.title = "Action items"
    existing.meeting_id = "meet-OTHER"
    existing.id.note_id = "note-other"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [existing]

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import find_note_by_title

        out = find_note_by_title(
            parent_object="people",
            parent_record_id="pid",
            title="Action items",
            meeting_id="meet-THIS",
        )
    assert out is None


def test_create_note_threads_meeting_id_and_scopes_dedup() -> None:
    # Existing same-title note belongs to a different meeting → must still POST,
    # and the POST must carry the new meeting_id.
    existing = MagicMock()
    existing.title = "Action items"
    existing.meeting_id = "meet-OTHER"
    existing.id.note_id = "note-other"
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = [existing]
    created = MagicMock()
    created.id.note_id = "note-new"
    captured: dict[str, object] = {}

    def _spy(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(data=created)

    fake_client.notes.post_v2_notes.side_effect = _spy

    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope = create_note(
            input=NoteInput(
                parent_object="people",
                parent_record_id="pid",
                title="Action items",
                content="body",
                format="markdown",
                meeting_id="meet-THIS",
            ),
            apply=True,
        )
    assert envelope.action == "created"
    assert envelope.record_id == "note-new"
    assert getattr(captured["data"], "meeting_id", None) == "meet-THIS"


def test_create_note_passes_created_at_through_to_sdk_boundary() -> None:
    """The Notes API accepts created_at for backdating; verify it's wired."""
    fake_client = MagicMock()
    fake_client.notes.get_v2_notes.return_value.data = []
    created = MagicMock()
    created.id.note_id = "new-note"
    fake_client.notes.post_v2_notes.return_value.data = created
    captured: dict[str, object] = {}

    def _spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_client.notes.post_v2_notes.return_value

    fake_client.notes.post_v2_notes.side_effect = _spy

    backdated = datetime(2025, 1, 5, tzinfo=UTC)
    with patch("libs.attio.notes.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.notes import create_note

        envelope = create_note(
            input=NoteInput(
                parent_object="companies",
                parent_record_id="cid",
                title="t",
                content="body",
                format="markdown",
                created_at=backdated,
            ),
            apply=True,
        )
    assert envelope.action == "created"
    request_obj = captured["data"]
    assert getattr(request_obj, "created_at", None) is not None
    assert "2025-01-05" in str(getattr(request_obj, "created_at"))
