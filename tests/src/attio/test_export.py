# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from libs.attio.contracts import ErrorEntry, ReliabilityEnvelope
from src.attio.export import LookupTable, execute
from src.attio.ops import (
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
        return_value=_fail("unresolved_ref: meeting:not-yet-created"),
    )
    monkeypatch.setattr("src.attio.export.OP_HANDLERS", {UpsertNote: handler_note})

    plan = [
        UpsertNote(
            parent=MeetingRef(ical_uid="not-yet-created"),
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

    list_notes_mock = MagicMock(return_value=[])
    monkeypatch.setattr(
        "src.attio.export.libs_list_notes_for_parent",
        list_notes_mock,
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
    list_notes_mock.assert_called_once_with(
        parent_object="people",
        parent_record_id="person-rec-1",
    )
    call_input = add_note_mock.call_args.args[0]
    assert call_input.parent_object == "people"
    assert call_input.parent_record_id == "person-rec-1"
    assert call_input.title == "hi"
    assert call_input.content == "body"


def test_handle_upsert_note_skips_when_title_exists(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    table = LookupTable()
    table.record(_meeting("fathom-call-1"), "meet-rec-1")

    existing = MagicMock()
    existing.note_id = "note-existing"
    existing.title = "Fathom summary"
    list_notes_mock = MagicMock(return_value=[existing])
    monkeypatch.setattr(
        "src.attio.export.libs_list_notes_for_parent",
        list_notes_mock,
    )

    add_note_mock = MagicMock()
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    envelope = _handle_upsert_note(
        UpsertNote(
            parent=MeetingRef(ical_uid="fathom-call-1"),
            title="Fathom summary",
            content="updated body that should be ignored on replay",
        ),
        table,
    )

    assert envelope.success is True
    assert envelope.action == "noop"
    assert envelope.record_id == "note-existing"
    add_note_mock.assert_not_called()


def test_handle_upsert_note_maps_ref_kind_to_parent_object(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    company_op = UpsertCompany(domain="example.com")
    meeting_op = _meeting("fathom-call-7")
    table = LookupTable()
    table.record(company_op, "company-rec-1")
    table.record(meeting_op, "meet-rec-7")

    monkeypatch.setattr(
        "src.attio.export.libs_list_notes_for_parent",
        MagicMock(return_value=[]),
    )

    fake_result = MagicMock()
    fake_result.note_id = "note-x"
    add_note_mock = MagicMock(return_value=fake_result)
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

    _handle_upsert_note(
        UpsertNote(
            parent=MeetingRef(ical_uid="fathom-call-7"),
            title="m",
            content="c",
        ),
        table,
    )
    assert add_note_mock.call_args.args[0].parent_object == "meetings"
    assert add_note_mock.call_args.args[0].parent_record_id == "meet-rec-7"

    from src.attio.ops import CompanyRef

    _handle_upsert_note(
        UpsertNote(
            parent=CompanyRef(domain="example.com"),
            title="m",
            content="c",
        ),
        table,
    )
    assert add_note_mock.call_args.args[0].parent_object == "companies"
    assert add_note_mock.call_args.args[0].parent_record_id == "company-rec-1"


def test_handle_upsert_note_unresolved_ref_returns_failed_envelope(monkeypatch) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_note = OP_HANDLERS[UpsertNote]

    list_notes_mock = MagicMock()
    monkeypatch.setattr(
        "src.attio.export.libs_list_notes_for_parent",
        list_notes_mock,
    )
    add_note_mock = MagicMock()
    monkeypatch.setattr("src.attio.export.libs_add_note", add_note_mock)

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
    list_notes_mock.assert_not_called()
    add_note_mock.assert_not_called()


def test_handle_upsert_mention_unresolved_ref_returns_failed_envelope(
    monkeypatch,
) -> None:
    from src.attio.export import OP_HANDLERS

    _handle_upsert_mention = OP_HANDLERS[UpsertMention]

    upsert_mention_mock = MagicMock()
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
    upsert_mention_mock.assert_not_called()


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
