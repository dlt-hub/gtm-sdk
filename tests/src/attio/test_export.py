# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from libs.attio.contracts import ErrorEntry, ReliabilityEnvelope
from src.attio.export import LookupTable, execute
from src.attio.ops import (
    CompanyRef,
    MeetingExternalRef,
    MeetingParticipant,
    MeetingRef,
    PersonRef,
    UpsertCompany,
    UpsertMeeting,
    UpsertMention,
    UpsertNote,
    UpsertPerson,
)


def _ok(record_id: str) -> ReliabilityEnvelope:
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


def _fail(msg: str) -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=False,
        partial_success=False,
        action="failed",
        record_id=None,
        errors=[
            ErrorEntry(
                code="test_failure",
                message=msg,
                error_type="TestError",
                fatal=True,
            ),
        ],
        warnings=[],
        skipped_fields=[],
        meta={"output_schema_version": "v1"},
    )


def _meeting(ical_uid: str = "fathom-call-1") -> UpsertMeeting:
    return UpsertMeeting(
        external_ref=MeetingExternalRef(ical_uid=ical_uid),
        title="t",
        description="d",
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 1, tzinfo=timezone.utc),
        is_all_day=False,
        participants=[
            MeetingParticipant(email_address="a@example.com", is_organizer=True),
        ],
    )


def test_lookup_table_resolves_person_ref() -> None:
    table = LookupTable()
    table.record(
        UpsertPerson(matching_attribute="email", email="a@example.com"),
        "rec-1",
    )
    assert table.resolve(PersonRef(attribute="email", value="a@example.com")) == "rec-1"


def test_lookup_table_missing_returns_none() -> None:
    table = LookupTable()
    assert table.resolve(PersonRef(attribute="email", value="nope@example.com")) is None


def test_lookup_table_resolves_meeting_ref() -> None:
    table = LookupTable()
    table.record(_meeting("fathom-call-42"), "meet-42")
    assert table.resolve(MeetingRef(ical_uid="fathom-call-42")) == "meet-42"


def test_lookup_table_ignores_none_record_id() -> None:
    table = LookupTable()
    table.record(UpsertPerson(matching_attribute="email", email="a@example.com"), None)
    assert table.resolve(PersonRef(attribute="email", value="a@example.com")) is None


def test_execute_happy_single_op(monkeypatch) -> None:
    handler = MagicMock(return_value=_ok("meet-1"))
    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {UpsertMeeting: handler})

    result = execute([_meeting()])

    assert result.success is True
    assert len(result.outcomes) == 1
    assert result.outcomes[0].record_id == "meet-1"
    assert result.outcomes[0].op_type == "UpsertMeeting"
    handler.assert_called_once()


def test_execute_fail_fast(monkeypatch) -> None:
    ok = MagicMock(return_value=_ok("p-1"))
    bad = MagicMock(return_value=_fail("validation"))
    never = MagicMock(return_value=_ok("never"))
    monkeypatch.setattr(
        "src.attio.export.OP_HANDLERS",
        {UpsertPerson: ok, UpsertCompany: bad, UpsertMeeting: never},
    )

    plan = [
        UpsertPerson(matching_attribute="email", email="a@example.com"),
        UpsertCompany(domain="example.com"),
        _meeting(),
    ]
    result = execute(plan)

    assert result.success is False
    assert result.fail_index == 1
    assert result.fail_reason == "op_failed"
    assert len(result.outcomes) == 2
    assert ok.call_count == 1
    assert bad.call_count == 1
    assert never.call_count == 0


def test_execute_unresolved_ref(monkeypatch) -> None:
    handler_note = MagicMock(
        return_value=_fail("unresolved_ref: person:not-yet-created"),
    )
    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {UpsertNote: handler_note})

    plan = [
        UpsertNote(
            parent=PersonRef(attribute="email", value="missing@example.com"),
            meeting=MeetingRef(ical_uid="not-yet-created"),
            title="x",
            content="y",
        ),
    ]
    result = execute(plan)

    assert result.success is False
    assert result.fail_index == 0
    assert "unresolved_ref" in result.outcomes[0].envelope.errors[0].message


def test_execute_handler_exception_becomes_failed_outcome(monkeypatch) -> None:
    """Library exceptions must turn into a failed ExecutionResult, not propagate."""

    def boom(_op, _table):
        raise RuntimeError("attio api blew up")

    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {UpsertPerson: boom})

    result = execute([UpsertPerson(matching_attribute="email", email="a@example.com")])

    assert result.success is False
    assert result.fail_index == 0
    assert result.fail_reason == "op_failed"
    assert len(result.outcomes) == 1
    envelope = result.outcomes[0].envelope
    assert envelope.success is False
    assert envelope.errors[0].code == "handler_exception"
    assert envelope.errors[0].error_type == "RuntimeError"
    assert "attio api blew up" in envelope.errors[0].message


def test_execute_optional_op_failure_does_not_abort(monkeypatch) -> None:
    """A failed optional op is recorded but does not abort the plan or flip
    overall success; downstream ops still run (ai-0ex)."""
    person = MagicMock(return_value=_fail("schema_mismatch"))
    mention = MagicMock(return_value=_ok("mention-1"))
    monkeypatch.setattr(
        "src.attio.export.OP_HANDLERS",
        {UpsertPerson: person, UpsertMention: mention},
    )

    plan = [
        UpsertPerson(
            matching_attribute="github_handle",
            github_handle="ghosthandle",
            optional=True,
        ),
        UpsertMention(
            mention_url="https://github.com/dlt-hub/dlt/issues/4002",
            last_action="mention_created",
            source_platform="github",
            source_id="abc",
            mention_body="hello",
            mention_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
            author_handle="u",
            primary_keyword="kw",
        ),
    ]
    result = execute(plan)

    assert result.success is True
    assert len(result.outcomes) == 2
    person_outcome = result.outcomes[0]
    assert person_outcome.success is False
    assert person_outcome.optional is True
    assert result.outcomes[1].success is True
    mention.assert_called_once()


def test_execute_optional_op_failure_omitted_from_lookup_table(monkeypatch) -> None:
    """A failed optional person op is not recorded, so a later ref to it
    resolves to None (and the mention handler degrades) — ai-0ex."""
    person = MagicMock(return_value=_fail("schema_mismatch"))
    upsert_mention_mock = MagicMock(return_value=_ok("mention-1"))
    # Use the real mention handler so the LookupTable miss → degrade path runs.
    from src.attio.export import OP_HANDLERS as _REAL  # noqa: N811

    monkeypatch.setattr(
        "src.attio.export.OP_HANDLERS",
        {UpsertPerson: person, UpsertMention: _REAL[UpsertMention]},
    )
    monkeypatch.setattr(
        "src.attio.export.libs_upsert_mention",
        upsert_mention_mock,
    )

    plan = [
        UpsertPerson(
            matching_attribute="github_handle",
            github_handle="ghosthandle",
            optional=True,
        ),
        UpsertMention(
            mention_url="https://github.com/dlt-hub/dlt/issues/3987",
            last_action="mention_created",
            source_platform="github",
            source_id="abc",
            mention_body="hello",
            mention_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
            author_handle="u",
            primary_keyword="kw",
            related_person=PersonRef(attribute="github_handle", value="ghosthandle"),
            related_person_optional=True,
        ),
    ]
    result = execute(plan)

    assert result.success is True
    upsert_mention_mock.assert_called_once()
    assert upsert_mention_mock.call_args.args[0].related_person_record_id is None


def test_execute_required_op_failure_still_aborts(monkeypatch) -> None:
    """Regression: a non-optional failing op keeps the fail-fast semantics."""
    person = MagicMock(return_value=_fail("boom"))
    mention = MagicMock(return_value=_ok("mention-1"))
    monkeypatch.setattr(
        "src.attio.export.OP_HANDLERS",
        {UpsertPerson: person, UpsertMention: mention},
    )
    plan = [
        UpsertPerson(
            matching_attribute="email",
            email="a@example.com",
        ),  # optional=False
        UpsertMention(
            mention_url="https://github.com/x",
            last_action="mention_created",
            source_platform="github",
            source_id="abc",
            mention_body="hello",
            mention_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
            author_handle="u",
            primary_keyword="kw",
        ),
    ]
    result = execute(plan)

    assert result.success is False
    assert result.fail_index == 0
    assert result.fail_reason == "op_failed"
    mention.assert_not_called()


def test_handle_upsert_person_schema_mismatch_is_classified(monkeypatch) -> None:
    """A SchemaMismatchError from the lib layer becomes a `schema_mismatch`
    failed envelope, not a `handler_exception` (ai-0ex)."""
    from libs.attio.errors import SchemaMismatchError
    from src.attio.export import OP_HANDLERS

    _handle_upsert_person = OP_HANDLERS[UpsertPerson]

    def boom(*_args, **_kwargs):
        raise SchemaMismatchError(
            "people object has no filter attribute 'github'",
            field="github",
        )

    monkeypatch.setattr("src.attio.export.libs_upsert_person", boom)

    envelope = _handle_upsert_person(
        UpsertPerson(
            matching_attribute="github_handle",
            github_handle="ghosthandle",
            optional=True,
        ),
        LookupTable(),
    )

    assert envelope.success is False
    assert envelope.errors[0].code == "schema_mismatch"


def test_execute_unknown_op(monkeypatch) -> None:
    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {})
    result = execute([UpsertPerson(matching_attribute="email", email="a@example.com")])
    assert result.success is False
    assert result.fail_reason is not None
    assert "unknown_op" in result.fail_reason


def test_execution_result_body_success(monkeypatch) -> None:
    import orjson

    handler = MagicMock(return_value=_ok("meet-1"))
    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {UpsertMeeting: handler})
    result = execute([_meeting()])
    body = orjson.loads(result.body())

    assert body["success"] is True
    assert body["outcomes"] == [
        {
            "op_index": 0,
            "op_type": "UpsertMeeting",
            "success": True,
            "record_id": "meet-1",
        },
    ]
    assert "fail_index" not in body


def test_handle_upsert_note_creates_when_title_missing(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    person_op = UpsertPerson(matching_attribute="email", email="a@example.com")
    table = LookupTable()
    table.record(person_op, "person-rec-1")

    find_note_mock = MagicMock(return_value=None)
    monkeypatch.setattr(
        "src.attio.export.libs_find_note_by_title",
        find_note_mock,
    )

    fake_result = MagicMock()
    fake_result.note_id = "note-1"
    add_note_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="a@example.com"),
            title="hi",
            content="body",
        ),
        table,
    )

    assert envelope.success is True
    assert envelope.record_id == "note-1"
    assert envelope.action == "created"
    find_note_mock.assert_called_once_with(
        parent_object="people",
        parent_record_id="person-rec-1",
        title="hi",
        meeting_id=None,
    )
    call_input = add_note_mock.call_args.args[0]
    assert call_input.parent_object == "people"
    assert call_input.parent_record_id == "person-rec-1"
    assert call_input.title == "hi"
    assert call_input.content == "body"


def test_handle_upsert_note_skips_when_title_and_meeting_match(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    person_op = UpsertPerson(matching_attribute="email", email="a@example.com")
    table = LookupTable()
    table.record(person_op, "person-rec-1")
    table.record(_meeting("fathom-call-1"), "meet-rec-1")

    # find_note_by_title owns the (title, meeting_id) match; here it finds one.
    find_note_mock = MagicMock(return_value="note-existing")
    monkeypatch.setattr("src.attio.export.libs_find_note_by_title", find_note_mock)

    add_note_mock = MagicMock()
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="a@example.com"),
            meeting=MeetingRef(ical_uid="fathom-call-1"),
            title="Fathom summary",
            content="updated body that should be ignored on replay",
        ),
        table,
    )

    assert envelope.success is True
    assert envelope.action == "noop"
    assert envelope.record_id == "note-existing"
    find_note_mock.assert_called_once_with(
        parent_object="people",
        parent_record_id="person-rec-1",
        title="Fathom summary",
        meeting_id="meet-rec-1",
    )
    add_note_mock.assert_not_called()


def test_handle_upsert_note_creates_when_same_title_different_meeting(
    monkeypatch,
) -> None:
    """A shared Person parent accumulates notes across meetings: a same-titled
    note from a *different* meeting must NOT dedup away the new one (ai-gez).
    find_note_by_title scopes the match to this meeting, so it returns None and
    a fresh note is created."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    table = LookupTable()
    table.record(
        UpsertPerson(matching_attribute="email", email="a@example.com"),
        "person-rec-1",
    )
    table.record(_meeting("fathom-call-2"), "meet-rec-2")

    # No note for THIS meeting (the only same-title note belongs to another).
    monkeypatch.setattr(
        "src.attio.export.libs_find_note_by_title",
        MagicMock(return_value=None),
    )
    fake_result = MagicMock()
    fake_result.note_id = "note-new"
    add_note_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="a@example.com"),
            meeting=MeetingRef(ical_uid="fathom-call-2"),
            title="Action items",
            content="c",
        ),
        table,
    )

    assert envelope.action == "created"
    assert envelope.record_id == "note-new"
    sent = add_note_mock.call_args.args[0]
    assert sent.meeting_id == "meet-rec-2"
    assert sent.format == "markdown"


def test_handle_upsert_note_maps_ref_kind_to_parent_object(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    company_op = UpsertCompany(domain="example.com")
    person_op = UpsertPerson(matching_attribute="email", email="a@example.com")
    meeting_op = _meeting("fathom-call-7")
    table = LookupTable()
    table.record(company_op, "company-rec-1")
    table.record(person_op, "person-rec-1")
    table.record(meeting_op, "meet-rec-7")

    monkeypatch.setattr(
        "src.attio.export.libs_find_note_by_title",
        MagicMock(return_value=None),
    )

    fake_result = MagicMock()
    fake_result.note_id = "note-x"
    add_note_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    # Person parent + meeting association -> people + meeting_id passthrough.
    _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="a@example.com"),
            meeting=MeetingRef(ical_uid="fathom-call-7"),
            title="m",
            content="c",
        ),
        table,
    )
    sent = add_note_mock.call_args.args[0]
    assert sent.parent_object == "people"
    assert sent.parent_record_id == "person-rec-1"
    assert sent.meeting_id == "meet-rec-7"

    from src.attio.ops import CompanyRef

    _handle_upsert_note(
        UpsertNote(
            parent=CompanyRef(domain="example.com"),
            title="m",
            content="c",
        ),
        table,
    )
    sent = add_note_mock.call_args.args[0]
    assert sent.parent_object == "companies"
    assert sent.parent_record_id == "company-rec-1"
    assert sent.meeting_id is None


def test_handle_upsert_note_resolves_parent_via_query_when_table_misses(
    monkeypatch,
) -> None:
    """Fathom emits no UpsertPerson; the /v2/meetings upsert auto-creates the
    Person, so an unresolved table entry falls back to a live email lookup."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    table = LookupTable()
    table.record(_meeting("fathom-call-9"), "meet-rec-9")

    resolve_mock = MagicMock(return_value="person-autocreated")
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    monkeypatch.setattr(
        "src.attio.export.libs_find_note_by_title",
        MagicMock(return_value=None),
    )
    fake_result = MagicMock()
    fake_result.note_id = "note-z"
    add_note_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="buyer@acme.com"),
            meeting=MeetingRef(ical_uid="fathom-call-9"),
            title="Fathom summary",
            content="c",
        ),
        table,
    )

    assert envelope.action == "created"
    # Notes keep attempts=3 (read-after-write of the just-auto-created Person).
    # Guards against an accidental drop to the meeting path's attempts=1.
    resolve_mock.assert_called_once_with(
        parent_object="people",
        email="buyer@acme.com",
        attempts=3,
    )
    assert add_note_mock.call_args.args[0].parent_record_id == "person-autocreated"


def test_handle_upsert_note_unresolved_meeting_returns_failed_envelope(
    monkeypatch,
) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    table = LookupTable()
    table.record(
        UpsertPerson(matching_attribute="email", email="a@example.com"),
        "person-rec-1",
    )

    find_note_mock = MagicMock()
    monkeypatch.setattr("src.attio.export.libs_find_note_by_title", find_note_mock)
    add_note_mock = MagicMock()
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="a@example.com"),
            meeting=MeetingRef(ical_uid="never-created"),
            title="m",
            content="c",
        ),
        table,
    )

    assert envelope.success is False
    assert envelope.errors[0].code == "unresolved_ref"
    assert "meeting" in envelope.errors[0].message
    find_note_mock.assert_not_called()
    add_note_mock.assert_not_called()


def test_handle_upsert_note_unresolved_ref_returns_failed_envelope(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    find_note_mock = MagicMock()
    monkeypatch.setattr(
        "src.attio.export.libs_find_note_by_title",
        find_note_mock,
    )
    add_note_mock = MagicMock()
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)
    # Not in the table and the live email lookup also misses → unresolved.
    monkeypatch.setattr(
        "src.attio.export.libs_resolve_record_id_for_ref",
        MagicMock(return_value=None),
    )

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=PersonRef(attribute="email", value="missing@example.com"),
            title="hi",
            content="body",
        ),
        LookupTable(),
    )

    assert envelope.success is False
    assert envelope.action == "failed"
    assert envelope.record_id is None
    assert len(envelope.errors) == 1
    err = envelope.errors[0]
    assert err.code == "unresolved_ref"
    assert err.error_type == "UnresolvedRefError"
    assert err.fatal is True
    find_note_mock.assert_not_called()
    add_note_mock.assert_not_called()


def test_handle_upsert_mention_unresolved_ref_degrades_to_warning(
    monkeypatch,
) -> None:
    """An unresolved related_person must NOT drop the mention (ai-0ex).

    The mention is the primary record; the person link is enrichment. When the
    ref does not resolve, the mention is written WITHOUT a person link and a
    `related_person_unresolved` warning is attached.
    """
    from src.attio.export import OP_HANDLERS

    _handle_upsert_mention = OP_HANDLERS[UpsertMention]

    upsert_mention_mock = MagicMock(return_value=_ok("mention-rec-1"))
    monkeypatch.setattr(
        "src.attio.export.libs_upsert_mention",
        upsert_mention_mock,
    )

    envelope = _handle_upsert_mention(
        UpsertMention(
            mention_url="https://github.com/dlt-hub/dlt/issues/4002",
            last_action="mention_created",
            source_platform="github",
            source_id="abc",
            mention_body="hello",
            mention_timestamp=datetime(2026, 5, 10, 11, 55, 53, tzinfo=timezone.utc),
            author_handle="u",
            primary_keyword="kw",
            related_person=PersonRef(attribute="github_handle", value="ghosthandle"),
            related_person_optional=True,
        ),
        LookupTable(),
    )

    assert envelope.success is True
    assert envelope.record_id == "mention-rec-1"
    upsert_mention_mock.assert_called_once()
    # The mention was written WITHOUT a person link.
    assert upsert_mention_mock.call_args.args[0].related_person_record_id is None
    warning_codes = {w.code for w in envelope.warnings}
    assert "related_person_unresolved" in warning_codes
    assert envelope.partial_success is True


def test_handle_upsert_mention_unresolved_ref_failed_write_stays_failed(
    monkeypatch,
) -> None:
    """If the mention write itself FAILS while the person ref was unresolved,
    the failed envelope must not be reflagged as a partial success (ai-0ex)."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_mention = OP_HANDLERS[UpsertMention]

    monkeypatch.setattr(
        "src.attio.export.libs_upsert_mention",
        MagicMock(return_value=_fail("mention write blew up")),
    )

    envelope = _handle_upsert_mention(
        UpsertMention(
            mention_url="https://github.com/dlt-hub/dlt/issues/4002",
            last_action="mention_created",
            source_platform="github",
            source_id="abc",
            mention_body="hello",
            mention_timestamp=datetime(2026, 5, 10, 11, 55, 53, tzinfo=timezone.utc),
            author_handle="u",
            primary_keyword="kw",
            related_person=PersonRef(attribute="github_handle", value="ghosthandle"),
            related_person_optional=True,
        ),
        LookupTable(),
    )

    assert envelope.success is False
    assert envelope.partial_success is False
    # The degradation context is still recorded for observability.
    assert "related_person_unresolved" in {w.code for w in envelope.warnings}


def test_handle_upsert_mention_unresolved_ref_hard_fails_by_default(
    monkeypatch,
) -> None:
    """Without opt-in best-effort, an unresolved related_person stays a hard
    failure so genuine missing-reference bugs in other plans stay loud (ai-0ex)."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_mention = OP_HANDLERS[UpsertMention]

    upsert_mention_mock = MagicMock(return_value=_ok("never"))
    monkeypatch.setattr(
        "src.attio.export.libs_upsert_mention",
        upsert_mention_mock,
    )

    envelope = _handle_upsert_mention(
        UpsertMention(
            mention_url="https://reddit.com/r/x/comments/abc",
            last_action="mention_created",
            source_platform="reddit",
            source_id="abc",
            mention_body="hello",
            mention_timestamp=datetime(2026, 5, 10, 11, 55, 53, tzinfo=timezone.utc),
            author_handle="u",
            primary_keyword="kw",
            related_person=PersonRef(attribute="email", value="missing@example.com"),
            # related_person_optional defaults to False
        ),
        LookupTable(),
    )

    assert envelope.success is False
    assert envelope.action == "failed"
    assert envelope.errors[0].code == "unresolved_ref"
    assert envelope.errors[0].fatal is True
    upsert_mention_mock.assert_not_called()


def test_execution_result_body_surfaces_degradation_warning(monkeypatch) -> None:
    """A mention written WITHOUT its person link must not look like a plain
    success in the response body — the warning/partial_success is surfaced so
    the degradation is visible to callers (ai-0ex)."""
    import orjson

    from src.attio.export import OP_HANDLERS as _REAL  # noqa: N811

    monkeypatch.setattr(
        "src.attio.export.OP_HANDLERS",
        {UpsertMention: _REAL[UpsertMention]},
    )
    monkeypatch.setattr(
        "src.attio.export.libs_upsert_mention",
        MagicMock(return_value=_ok("mention-1")),
    )

    result = execute(
        [
            UpsertMention(
                mention_url="https://github.com/dlt-hub/dlt/issues/4002",
                last_action="mention_created",
                source_platform="github",
                source_id="abc",
                mention_body="hello",
                mention_timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
                author_handle="u",
                primary_keyword="kw",
                related_person=PersonRef(
                    attribute="github_handle",
                    value="ghosthandle",
                ),
                related_person_optional=True,
            ),
        ],
    )
    body = orjson.loads(result.body())

    assert body["success"] is True
    outcome = body["outcomes"][0]
    assert outcome["success"] is True
    assert outcome["partial_success"] is True
    assert outcome["warnings"][0]["code"] == "related_person_unresolved"


def test_execution_result_body_failure(monkeypatch) -> None:
    import orjson

    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {})
    result = execute([UpsertPerson(matching_attribute="email", email="a@example.com")])
    body = orjson.loads(result.body())

    assert body["success"] is False
    assert body["fail_index"] == 0
    assert body["fail_reason"].startswith("unknown_op")


def test_lookup_table_records_and_resolves_github_handle_person() -> None:
    table = LookupTable()
    op = UpsertPerson(
        matching_attribute="github_handle",
        github_handle="elviskahoro",
        github_url="https://github.com/elviskahoro",
    )
    table.record(op, "rec_gh_1")
    assert (
        table.resolve(PersonRef(attribute="github_handle", value="elviskahoro"))
        == "rec_gh_1"
    )


def test_lookup_table_records_and_resolves_email_person_generalized() -> None:
    table = LookupTable()
    op = UpsertPerson(matching_attribute="email", email="a@example.com")
    table.record(op, "rec_email_1")
    assert (
        table.resolve(PersonRef(attribute="email", value="a@example.com"))
        == "rec_email_1"
    )


def test_lookup_table_records_and_resolves_linkedin_person_generalized() -> None:
    table = LookupTable()
    op = UpsertPerson(
        matching_attribute="linkedin",
        linkedin="https://www.linkedin.com/in/foo",
    )
    table.record(op, "rec_li_1")
    assert (
        table.resolve(
            PersonRef(attribute="linkedin", value="https://www.linkedin.com/in/foo"),
        )
        == "rec_li_1"
    )


# Tests for merge_only_if_empty behavior


@patch("src.attio.export.get_person_values")
@patch("src.attio.export.libs_upsert_person")
def test_handle_upsert_person_no_merge_list_overwrites(
    mock_upsert,
    mock_get_values,
) -> None:
    """When merge_only_if_empty is empty, all fields flow through unchanged."""
    mock_get_values.return_value = None
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "pe_1"

    from src.attio.export import _handle_upsert_person
    from src.attio.ops import UpsertPerson

    result = _handle_upsert_person(
        UpsertPerson(matching_attribute="email", email="a@b.test", title="New Title"),
        LookupTable(),
    )

    assert result.success is True
    # PersonInput passed to libs_upsert_person should have title set
    person_input = mock_upsert.call_args.args[0]
    assert person_input.title == "New Title"
    # get_person_values should not have been called since merge_only_if_empty is empty
    mock_get_values.assert_not_called()


@patch("src.attio.export.get_person_values")
@patch("src.attio.export.libs_upsert_person")
def test_handle_upsert_person_merge_strips_populated_slugs(
    mock_upsert,
    mock_get_values,
) -> None:
    """When merge_only_if_empty is set, populated fields on existing record are nulled."""
    # Simulate existing person with title populated
    mock_get_values.return_value = {
        "name": [{"full_name": "Existing Person"}],
        "title": [{"value": "Existing Title"}],
        "primary_location": None,
    }
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "pe_1"

    from src.attio.export import _handle_upsert_person
    from src.attio.ops import UpsertPerson

    result = _handle_upsert_person(
        UpsertPerson(
            matching_attribute="email",
            email="a@b.test",
            title="Incoming Title",
            city="Brooklyn",
            merge_only_if_empty=["title", "city"],
        ),
        LookupTable(),
    )

    assert result.success is True
    person_input = mock_upsert.call_args.args[0]
    assert person_input.title is None  # stripped (existing was populated)
    assert person_input.city == "Brooklyn"  # kept (existing was None)
    mock_get_values.assert_called_once_with(
        matching_attribute="email",
        email="a@b.test",
        linkedin=None,
        github_handle=None,
    )


@patch("src.attio.export.get_person_values")
@patch("src.attio.export.libs_upsert_person")
def test_handle_upsert_person_merge_keeps_title_when_existing_title_empty(
    mock_upsert,
    mock_get_values,
) -> None:
    """Bug 1 regression: existing has name but empty title — incoming title must NOT be stripped."""
    mock_get_values.return_value = {
        "name": [{"full_name": "Existing Person"}],
        "title": [],
        "primary_location": None,
    }
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "pe_1"

    from src.attio.export import _handle_upsert_person

    _handle_upsert_person(
        UpsertPerson(
            matching_attribute="email",
            email="a@b.test",
            title="Incoming Title",
            merge_only_if_empty=["title"],
        ),
        LookupTable(),
    )

    person_input = mock_upsert.call_args.args[0]
    assert person_input.title == "Incoming Title", (
        "Bug 1: title was nulled even though existing record had an empty title"
    )


@patch("src.attio.export.get_person_values")
@patch("src.attio.export.libs_upsert_person")
def test_handle_upsert_person_merge_strips_title_when_existing_title_populated(
    mock_upsert,
    mock_get_values,
) -> None:
    """Bug 1 inverse: existing has populated title — incoming title IS stripped."""
    mock_get_values.return_value = {
        "name": [],
        "title": [{"value": "CEO"}],
        "primary_location": None,
    }
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "pe_1"

    from src.attio.export import _handle_upsert_person

    _handle_upsert_person(
        UpsertPerson(
            matching_attribute="email",
            email="a@b.test",
            title="Incoming Title",
            merge_only_if_empty=["title"],
        ),
        LookupTable(),
    )

    person_input = mock_upsert.call_args.args[0]
    assert person_input.title is None


@patch("src.attio.export.get_person_values")
@patch("src.attio.export.libs_upsert_person")
def test_handle_upsert_person_merge_uses_github_handle_lookup(
    mock_upsert,
    mock_get_values,
) -> None:
    """T3 (Bug 2): matching_attribute=github_handle routes lookup correctly
    and protected fields on the github-matched record are honored."""
    mock_get_values.return_value = {"title": [{"value": "CTO"}]}
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "pe_1"

    from src.attio.export import _handle_upsert_person

    _handle_upsert_person(
        UpsertPerson(
            matching_attribute="github_handle",
            github_handle="octocat",
            title="Incoming Title",
            merge_only_if_empty=["title"],
        ),
        LookupTable(),
    )

    mock_get_values.assert_called_once_with(
        matching_attribute="github_handle",
        email=None,
        linkedin=None,
        github_handle="octocat",
    )
    person_input = mock_upsert.call_args.args[0]
    assert person_input.title is None


@patch("src.attio.export.get_person_values")
@patch("src.attio.export.libs_upsert_person")
def test_handle_upsert_person_merge_lookup_aligned_with_matching_attribute(
    mock_upsert,
    mock_get_values,
) -> None:
    """T4 (Bug 3): matching_attribute=linkedin with both email and linkedin on
    the op. Helper must be called with matching_attribute=linkedin so the read
    targets the same record the write will touch."""
    mock_get_values.return_value = {"title": [{"value": "Existing"}]}
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "pe_1"

    from src.attio.export import _handle_upsert_person

    _handle_upsert_person(
        UpsertPerson(
            matching_attribute="linkedin",
            email="a@b.test",
            linkedin="https://linkedin.com/in/foo",
            title="Incoming",
            merge_only_if_empty=["title"],
        ),
        LookupTable(),
    )

    mock_get_values.assert_called_once_with(
        matching_attribute="linkedin",
        email="a@b.test",
        linkedin="https://linkedin.com/in/foo",
        github_handle=None,
    )
    person_input = mock_upsert.call_args.args[0]
    assert person_input.title is None


@patch("src.attio.export.get_company_values")
@patch("src.attio.export.libs_upsert_company")
def test_handle_upsert_company_no_merge_list_overwrites(
    mock_upsert,
    mock_get_values,
) -> None:
    """When merge_only_if_empty is empty, all fields flow through unchanged."""
    mock_get_values.return_value = None
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "co_1"

    from src.attio.export import _handle_upsert_company
    from src.attio.ops import UpsertCompany

    result = _handle_upsert_company(
        UpsertCompany(domain="example.test", industry="Software"),
        LookupTable(),
    )

    assert result.success is True
    company_input = mock_upsert.call_args.args[0]
    assert company_input.industry == "Software"
    mock_get_values.assert_not_called()


@patch("src.attio.export.get_company_values")
@patch("src.attio.export.libs_upsert_company")
def test_handle_upsert_company_merge_strips_populated_slugs(
    mock_upsert,
    mock_get_values,
) -> None:
    """When merge_only_if_empty is set, populated fields on existing record are nulled."""
    mock_get_values.return_value = {
        "industry": [{"option": "Technology"}],
        "employee_count": None,
        "estimate_revenue": None,
    }
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "co_1"

    from src.attio.export import _handle_upsert_company
    from src.attio.ops import UpsertCompany

    result = _handle_upsert_company(
        UpsertCompany(
            domain="example.test",
            name="Example Corp",
            industry="SaaS",
            employee_count="50-100",
            merge_only_if_empty=["industry", "employee_count"],
        ),
        LookupTable(),
    )

    assert result.success is True
    company_input = mock_upsert.call_args.args[0]
    assert company_input.industry is None  # stripped (existing was populated)
    assert company_input.employee_count == "50-100"  # kept (existing was None)
    mock_get_values.assert_called_once_with("example.test")


@patch("src.attio.export.get_company_values")
@patch("src.attio.export.libs_upsert_company")
def test_handle_upsert_company_merge_strips_populated_linkedin(
    mock_upsert,
    mock_get_values,
) -> None:
    """``linkedin_url`` on the op maps to the ``linkedin`` slug on the
    existing record. When that slug is already populated and the op opted
    into ``merge_only_if_empty=["linkedin_url"]``, the dispatcher must
    null the input so we don't stomp curated CRM data.
    """
    mock_get_values.return_value = {
        "linkedin": [{"value": "https://www.linkedin.com/company/curated"}],
    }
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "co_2"

    from src.attio.export import _handle_upsert_company
    from src.attio.ops import UpsertCompany

    result = _handle_upsert_company(
        UpsertCompany(
            domain="example.test",
            linkedin_url="https://www.linkedin.com/company/from-rb2b",
            merge_only_if_empty=["linkedin_url"],
        ),
        LookupTable(),
    )

    assert result.success is True
    company_input = mock_upsert.call_args.args[0]
    assert company_input.linkedin_url is None


@patch("src.attio.export.get_company_values")
@patch("src.attio.export.libs_upsert_company")
def test_handle_upsert_company_merge_keeps_linkedin_when_empty(
    mock_upsert,
    mock_get_values,
) -> None:
    """If the existing Company has no ``linkedin``, the op's value must
    flow through even with ``merge_only_if_empty=["linkedin_url"]``.
    """
    mock_get_values.return_value = {"linkedin": None}
    mock_upsert.return_value.success = True
    mock_upsert.return_value.record_id = "co_3"

    from src.attio.export import _handle_upsert_company
    from src.attio.ops import UpsertCompany

    _handle_upsert_company(
        UpsertCompany(
            domain="example.test",
            linkedin_url="https://www.linkedin.com/company/from-rb2b",
            merge_only_if_empty=["linkedin_url"],
        ),
        LookupTable(),
    )

    company_input = mock_upsert.call_args.args[0]
    assert company_input.linkedin_url == "https://www.linkedin.com/company/from-rb2b"


# Tests for _handle_upsert_tracking_event


@patch("src.attio.export.find_or_create_tracking_event")
def test_handle_upsert_tracking_event_resolves_person_ref(mock_libs) -> None:
    """Test that the PersonRef is resolved through LookupTable and forwarded."""
    mock_libs.return_value = MagicMock(success=True, record_id="te_1", action="created")

    from src.attio.export import _handle_upsert_tracking_event
    from src.attio.ops import (
        PersonRef,
        UpsertPerson,
        UpsertTrackingEvent,
    )

    table = LookupTable()
    table.record(
        UpsertPerson(matching_attribute="email", email="a@b.test"),
        "pe_1",
    )

    result = _handle_upsert_tracking_event(
        UpsertTrackingEvent(
            external_id="rb2b:abc123",
            source="rb2b",
            name="https://example.test/pricing",
            event_type="rb2b_visit",
            event_subtype="repeat_visit",
            event_timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json='{"raw": "payload"}',
            subject_person=PersonRef(attribute="email", value="a@b.test"),
        ),
        table,
    )

    assert result.success is True
    input_arg = mock_libs.call_args.args[0]
    assert input_arg.related_person_record_id == "pe_1"
    assert input_arg.event_subtype == "repeat_visit"
    assert input_arg.source == "rb2b"


@patch("src.attio.export.find_or_create_tracking_event")
def test_handle_upsert_tracking_event_unresolved_ref_is_fatal(mock_libs) -> None:
    """Test that unresolvable PersonRef causes fatal error."""
    from src.attio.export import _handle_upsert_tracking_event
    from src.attio.ops import PersonRef, UpsertTrackingEvent

    result = _handle_upsert_tracking_event(
        UpsertTrackingEvent(
            external_id="rb2b:abc123",
            source="rb2b",
            name="https://example.test/pricing",
            event_type="rb2b_visit",
            event_timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json='{"raw": "payload"}',
            subject_person=PersonRef(attribute="email", value="missing@b.test"),
        ),
        LookupTable(),  # empty — nothing to resolve
    )

    assert result.success is False
    assert result.errors[0].code == "unresolved_ref"
    assert result.errors[0].fatal is True
    mock_libs.assert_not_called()


@patch("src.attio.export.find_or_create_tracking_event")
def test_handle_upsert_tracking_event_no_refs_passes_none(mock_libs) -> None:
    """Test that when no refs are provided, None is passed to the lib."""
    mock_libs.return_value = MagicMock(success=True, record_id="te_2", action="created")

    from src.attio.export import _handle_upsert_tracking_event
    from src.attio.ops import UpsertTrackingEvent

    result = _handle_upsert_tracking_event(
        UpsertTrackingEvent(
            external_id="rb2b:abc123",
            source="rb2b",
            name="https://example.test/pricing",
            event_type="rb2b_visit",
            event_timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
            body_json='{"raw": "payload"}',
        ),
        LookupTable(),
    )

    assert result.success is True
    input_arg = mock_libs.call_args.args[0]
    assert input_arg.related_person_record_id is None


# ---------- UpsertMeeting.linked_records (ai-ch3) ----------


def _meeting_with_links(
    linked_records: list[PersonRef | CompanyRef],
) -> UpsertMeeting:
    return UpsertMeeting(
        external_ref=MeetingExternalRef(ical_uid="fathom-call-links"),
        title="t",
        description="d",
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 1, tzinfo=timezone.utc),
        is_all_day=False,
        participants=[
            MeetingParticipant(email_address="a@example.com", is_organizer=True),
        ],
        linked_records=linked_records,
    )


def test_handle_upsert_meeting_resolves_links_via_live_lookup(monkeypatch) -> None:
    """Empty table → resolve person/company refs by email/domain (Fathom path)."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    def _resolve(*, parent_object, attempts, email=None, domain=None):  # noqa: ARG001
        if parent_object == "people" and email == "buyer@acme.com":
            return "person-1"
        if parent_object == "companies" and domain == "acme.com":
            return "company-1"
        return None

    resolve_mock = MagicMock(side_effect=_resolve)
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(return_value=_ok("meet-1"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [
                PersonRef(attribute="email", value="buyer@acme.com"),
                CompanyRef(domain="acme.com"),
            ],
        ),
        LookupTable(),
    )

    assert envelope.success is True
    # Both refs exist already → resolved in phase 1, no re-sync POST.
    fc_mock.assert_called_once()
    meeting_input = fc_mock.call_args.args[0]
    linked = meeting_input.linked_records
    assert {(lr.object, lr.record_id) for lr in linked} == {
        ("people", "person-1"),
        ("companies", "company-1"),
    }
    # Pre-existing records resolve in phase 1 with attempts=1 (no read-after-
    # write race); phase 2's attempts=3 only fires for refs that missed.
    for call in resolve_mock.call_args_list:
        assert call.kwargs["attempts"] == 1


def test_handle_upsert_meeting_skips_unresolved_links(monkeypatch) -> None:
    """A ref that never resolves (even post-create) is dropped, NOT failed.

    Contrast with notes, where an unresolved parent is a fatal error.
    """
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    resolve_mock = MagicMock(return_value=None)
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(return_value=_ok("meet-2"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [PersonRef(attribute="email", value="ghost@nowhere.com")],
        ),
        LookupTable(),
    )

    assert envelope.success is True
    # Phase 1 missed and phase 2's read-after-write retry also missed, so no
    # re-sync POST happens and the ref is dropped.
    fc_mock.assert_called_once()
    assert fc_mock.call_args.args[0].linked_records == []
    attempts_used = {call.kwargs["attempts"] for call in resolve_mock.call_args_list}
    assert attempts_used == {1, 3}
    # The drop is observable, not silent: partial success + a warning.
    assert envelope.partial_success is True
    assert any(w.code == "unresolved_meeting_links" for w in envelope.warnings)


def test_handle_upsert_meeting_unresolved_company_warns(monkeypatch) -> None:
    """A company genuinely not in Attio survives the retry as unresolved."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    resolve_mock = MagicMock(return_value=None)
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(return_value=_ok("meet-c"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links([CompanyRef(domain="notin.crm")]),
        LookupTable(),
    )

    assert envelope.success is True
    # Retried (attempts=1 then 3) but still absent → no re-sync POST, surfaced.
    fc_mock.assert_called_once()
    assert fc_mock.call_args.args[0].linked_records == []
    assert {call.kwargs["attempts"] for call in resolve_mock.call_args_list} == {1, 3}
    assert envelope.partial_success is True
    assert any(w.code == "unresolved_meeting_links" for w in envelope.warnings)


def test_handle_upsert_meeting_relinks_company_on_transient_miss(monkeypatch) -> None:
    """A company that misses phase 1 but resolves on retry links via re-sync.

    Covers a transient lookup miss or a company created by a concurrent pipeline
    just before this webhook ran.
    """
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    def _resolve(*, parent_object, attempts, email=None, domain=None):  # noqa: ARG001
        if parent_object == "companies" and domain == "acme.com" and attempts == 3:
            return "company-late"
        return None

    monkeypatch.setattr(
        "src.attio.export.libs_resolve_record_id_for_ref",
        MagicMock(side_effect=_resolve),
    )
    fc_mock = MagicMock(side_effect=[_ok("meet-tc"), _ok("meet-tc")])
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links([CompanyRef(domain="acme.com")]),
        LookupTable(),
    )

    assert envelope.success is True
    assert envelope.partial_success is False
    assert fc_mock.call_count == 2
    assert fc_mock.call_args_list[0].args[0].linked_records == []
    second_links = fc_mock.call_args_list[1].args[0].linked_records
    assert [(lr.object, lr.record_id) for lr in second_links] == [
        ("companies", "company-late"),
    ]


def test_handle_upsert_meeting_prefers_table_over_live_lookup(monkeypatch) -> None:
    """A ref already in the plan's LookupTable short-circuits the live query."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    table = LookupTable()
    table.record(
        UpsertPerson(matching_attribute="email", email="buyer@acme.com"),
        "person-from-table",
    )

    resolve_mock = MagicMock(return_value="should-not-be-used")
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(return_value=_ok("meet-3"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [PersonRef(attribute="email", value="buyer@acme.com")],
        ),
        table,
    )

    assert envelope.success is True
    resolve_mock.assert_not_called()
    linked = fc_mock.call_args.args[0].linked_records
    assert [(lr.object, lr.record_id) for lr in linked] == [
        ("people", "person-from-table"),
    ]


def test_handle_upsert_meeting_dedups_links(monkeypatch) -> None:
    """Refs resolving to the same (object, record_id) collapse to one link."""
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    monkeypatch.setattr(
        "src.attio.export.libs_resolve_record_id_for_ref",
        MagicMock(return_value="person-dup"),
    )
    fc_mock = MagicMock(return_value=_ok("meet-4"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [
                PersonRef(attribute="email", value="a@acme.com"),
                PersonRef(attribute="email", value="b@acme.com"),
            ],
        ),
        LookupTable(),
    )

    assert envelope.success is True
    linked = fc_mock.call_args.args[0].linked_records
    assert [(lr.object, lr.record_id) for lr in linked] == [("people", "person-dup")]


def test_handle_upsert_meeting_empty_links_makes_no_lookups(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    resolve_mock = MagicMock(return_value="x")
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(return_value=_ok("meet-5"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(_meeting_with_links([]), LookupTable())

    assert envelope.success is True
    resolve_mock.assert_not_called()
    fc_mock.assert_called_once()
    assert fc_mock.call_args.args[0].linked_records == []


def test_handle_upsert_meeting_non_email_person_ref_warns(monkeypatch) -> None:
    """A linkedin/github person ref not in the table is dropped but surfaced.

    The live lookup is email-only, so these identities resolve solely via the
    plan's LookupTable; an unresolved one must not vanish silently.
    """
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    resolve_mock = MagicMock(return_value=None)
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(return_value=_ok("meet-li"))
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [PersonRef(attribute="linkedin", value="https://linkedin.com/in/x")],
        ),
        LookupTable(),
    )

    assert envelope.success is True
    fc_mock.assert_called_once()
    assert fc_mock.call_args.args[0].linked_records == []
    # The live lookup is email-only, so a linkedin ref never reaches it (table
    # miss → straight to unresolved). It is counted, not silently dropped.
    resolve_mock.assert_not_called()
    assert envelope.partial_success is True
    assert any(w.code == "unresolved_meeting_links" for w in envelope.warnings)


def test_handle_upsert_meeting_relinks_autocreated_participant(monkeypatch) -> None:
    """A first-time attendee misses phase 1, then links via a re-sync POST.

    Models the roborev gap: /v2/meetings auto-creates the participant Person, so
    the ref that missed before the write is resolvable after it. The handler
    re-resolves (attempts=3) and re-POSTs to attach the just-created record.
    """
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    def _resolve(*, parent_object, attempts, email=None, domain=None):  # noqa: ARG001
        if parent_object == "companies" and domain == "acme.com":
            return "company-1"  # pre-existing company, resolves in phase 1
        if parent_object == "people" and email == "buyer@acme.com" and attempts == 3:
            return "person-autocreated"  # only resolvable AFTER the meeting POST
        return None

    resolve_mock = MagicMock(side_effect=_resolve)
    monkeypatch.setattr("src.attio.export.libs_resolve_record_id_for_ref", resolve_mock)
    fc_mock = MagicMock(side_effect=[_ok("meet-6"), _ok("meet-6")])
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [
                PersonRef(attribute="email", value="buyer@acme.com"),
                CompanyRef(domain="acme.com"),
            ],
        ),
        LookupTable(),
    )

    assert envelope.success is True
    assert fc_mock.call_count == 2
    # Phase 1 POST carries only the pre-existing company.
    first_links = fc_mock.call_args_list[0].args[0].linked_records
    assert [(lr.object, lr.record_id) for lr in first_links] == [
        ("companies", "company-1"),
    ]
    # Re-sync POST carries the company plus the auto-created participant Person.
    second_links = fc_mock.call_args_list[1].args[0].linked_records
    assert {(lr.object, lr.record_id) for lr in second_links} == {
        ("companies", "company-1"),
        ("people", "person-autocreated"),
    }


def test_handle_upsert_meeting_relink_failure_degrades_to_warning(monkeypatch) -> None:
    """A failed re-sync keeps the (created) meeting as a partial success.

    The meeting already exists from phase 1, so a failed re-link must not abort
    the plan — downstream notes still need to run.
    """
    from src.attio.export import OP_HANDLERS

    _handle_upsert_meeting = OP_HANDLERS[UpsertMeeting]

    def _resolve(*, parent_object, attempts, email=None, domain=None):  # noqa: ARG001
        if attempts == 3 and email == "buyer@acme.com":
            return "person-autocreated"
        return None

    monkeypatch.setattr(
        "src.attio.export.libs_resolve_record_id_for_ref",
        MagicMock(side_effect=_resolve),
    )
    fc_mock = MagicMock(side_effect=[_ok("meet-7"), _fail("relink boom")])
    monkeypatch.setattr("src.attio.export.find_or_create_meeting", fc_mock)

    envelope = _handle_upsert_meeting(
        _meeting_with_links(
            [PersonRef(attribute="email", value="buyer@acme.com")],
        ),
        LookupTable(),
    )

    assert envelope.success is True
    assert envelope.partial_success is True
    assert envelope.record_id == "meet-7"
    assert fc_mock.call_count == 2
    assert any(w.code == "unresolved_meeting_links" for w in envelope.warnings)
