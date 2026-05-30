from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from src.attio.ops import (
    AttioOp,
    CompanyRef,
    MeetingExternalRef,
    MeetingParticipant,
    MeetingRef,
    PersonRef,
    Ref,
    UpsertCompany,
    UpsertMeeting,
    UpsertMention,
    UpsertNote,
    UpsertPerson,
)


def test_upsert_person_discriminator() -> None:
    op = UpsertPerson(matching_attribute="email", email="a@example.com")
    assert op.op_type == "upsert_person"


def test_attio_op_union_dispatches_by_op_type() -> None:
    adapter: TypeAdapter[AttioOp] = TypeAdapter(AttioOp)
    parsed = adapter.validate_python(
        {"op_type": "upsert_company", "domain": "example.com"},
    )
    assert isinstance(parsed, UpsertCompany)
    assert parsed.domain == "example.com"


def test_ref_excludes_meeting() -> None:
    # ai-gez: a meeting can never be a note/record parent (Attio's Notes API
    # rejects parent_object="meetings"), so MeetingRef is not part of Ref.
    adapter: TypeAdapter[Ref] = TypeAdapter(Ref)
    with pytest.raises(ValidationError):
        adapter.validate_python({"ref_kind": "meeting", "ical_uid": "fathom-call-1"})


def test_ref_discriminator_person_and_company() -> None:
    adapter: TypeAdapter[Ref] = TypeAdapter(Ref)
    person = adapter.validate_python(
        {"ref_kind": "person", "attribute": "email", "value": "a@b.com"},
    )
    company = adapter.validate_python({"ref_kind": "company", "domain": "b.com"})
    assert isinstance(person, PersonRef)
    assert isinstance(company, CompanyRef)


def test_upsert_meeting_requires_participants() -> None:
    with pytest.raises(ValidationError):
        UpsertMeeting(  # type: ignore[call-arg]
            external_ref=MeetingExternalRef(ical_uid="x"),
            title="t",
            description="d",
            start=datetime(2026, 5, 12, tzinfo=timezone.utc),
            end=datetime(2026, 5, 12, 1, tzinfo=timezone.utc),
        )


def test_upsert_meeting_external_ref_structured_defaults() -> None:
    ref = MeetingExternalRef(ical_uid="fathom-call-42")
    assert ref.provider == "google"
    assert ref.is_recurring is False
    assert ref.original_start_time is None

    op = UpsertMeeting(
        external_ref=ref,
        title="t",
        description="d",
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 1, tzinfo=timezone.utc),
        is_all_day=False,
        participants=[MeetingParticipant(email_address="a@b.com", is_organizer=True)],
    )
    assert op.external_ref.ical_uid == "fathom-call-42"
    assert op.linked_records == []


def test_upsert_note_carries_ref() -> None:
    op = UpsertNote(
        parent=PersonRef(attribute="email", value="a@b.com"),
        title="x",
        content="y",
    )
    assert isinstance(op.parent, PersonRef)
    assert op.op_type == "upsert_note"
    assert op.meeting is None


def test_upsert_note_carries_meeting_association() -> None:
    op = UpsertNote(
        parent=PersonRef(attribute="email", value="a@b.com"),
        meeting=MeetingRef(ical_uid="fathom-call-7"),
        title="x",
        content="y",
    )
    assert isinstance(op.meeting, MeetingRef)
    assert op.meeting.ical_uid == "fathom-call-7"


def test_upsert_note_rejects_meeting_as_parent() -> None:
    # The parent union no longer admits MeetingRef (ai-gez).
    with pytest.raises(ValidationError):
        UpsertNote.model_validate(
            {
                "parent": {"ref_kind": "meeting", "ical_uid": "fathom-call-7"},
                "title": "x",
                "content": "y",
            },
        )


def test_person_ref_generalized_shape_email() -> None:
    ref = PersonRef(attribute="email", value="a@example.com")
    assert ref.attribute == "email"
    assert ref.value == "a@example.com"


def test_person_ref_generalized_shape_github_handle() -> None:
    ref = PersonRef(attribute="github_handle", value="elviskahoro")
    assert ref.attribute == "github_handle"
    assert ref.value == "elviskahoro"


def test_upsert_person_requires_matching_attribute_field_to_be_set() -> None:
    with pytest.raises(ValidationError):
        # matching_attribute="email" but email is None
        UpsertPerson(
            matching_attribute="email",
            linkedin="https://www.linkedin.com/in/foo",
        )


def test_upsert_person_github_handle_construction() -> None:
    op = UpsertPerson(
        matching_attribute="github_handle",
        github_handle="elviskahoro",
        github_url="https://github.com/elviskahoro",
    )
    assert op.matching_attribute == "github_handle"
    assert op.github_handle == "elviskahoro"
    assert op.github_url == "https://github.com/elviskahoro"


def _valid_tracking_event_kwargs() -> dict[str, Any]:
    return dict(
        external_id="rb2b:abc123",
        source="rb2b",
        name="https://example.test/pricing",
        event_type="rb2b_visit",
        event_timestamp=datetime(2026, 5, 14, tzinfo=timezone.utc),
        body_json='{"raw": "payload"}',
    )


def test_upsert_tracking_event_minimal_valid() -> None:
    from src.attio.ops import UpsertTrackingEvent

    op = UpsertTrackingEvent(**_valid_tracking_event_kwargs())
    assert op.op_type == "upsert_tracking_event"
    assert op.source == "rb2b"
    assert op.event_subtype is None
    assert op.subject_person is None


def test_upsert_tracking_event_source_is_required() -> None:
    """``source`` must be set so the tracking_events row can be filtered by
    emitter in Attio without parsing the external_id prefix — ai-ztm."""
    from src.attio.ops import UpsertTrackingEvent

    kwargs = _valid_tracking_event_kwargs()
    kwargs.pop("source")
    with pytest.raises(ValidationError):
        UpsertTrackingEvent(**kwargs)


def test_upsert_tracking_event_with_person_ref_and_subtype() -> None:
    from src.attio.ops import UpsertTrackingEvent

    op = UpsertTrackingEvent(
        **_valid_tracking_event_kwargs(),
        event_subtype="repeat_visit",
        subject_person=PersonRef(attribute="email", value="alice@example.test"),
    )
    assert op.subject_person is not None
    assert op.subject_person.attribute == "email"
    assert op.subject_person.value == "alice@example.test"
    assert op.event_subtype == "repeat_visit"


def test_upsert_tracking_event_forbids_extra_fields() -> None:
    from src.attio.ops import UpsertTrackingEvent

    with pytest.raises(ValidationError):
        UpsertTrackingEvent(**_valid_tracking_event_kwargs(), bogus="x")  # pyright: ignore[reportCallIssue]  # pyrefly: ignore[unexpected-keyword]


def test_upsert_tracking_event_with_subject_company() -> None:
    """Prod ``tracking_events`` grew a ``company`` record-reference attribute
    (verified 2026-05-26 via ``tmp/inspect_tracking_events_schema.py``). The
    op carries the CompanyRef; the dispatcher resolves it via the plan's
    LookupTable to a record_id; the writer emits the ``company`` slug. See
    ai-0lv.
    """
    from src.attio.ops import UpsertTrackingEvent

    op = UpsertTrackingEvent(
        **_valid_tracking_event_kwargs(),
        subject_company=CompanyRef(domain="example.test"),
    )
    assert op.subject_company is not None
    assert op.subject_company.domain == "example.test"


def test_attio_op_union_discriminates_tracking_event() -> None:
    from src.attio.ops import UpsertTrackingEvent

    adapter = TypeAdapter(AttioOp)
    kwargs = _valid_tracking_event_kwargs()
    kwargs["event_timestamp"] = kwargs["event_timestamp"].isoformat()
    raw: dict[str, Any] = {"op_type": "upsert_tracking_event", **kwargs}
    op = adapter.validate_python(raw)
    assert isinstance(op, UpsertTrackingEvent)


def test_upsert_person_accepts_enrichment_fields() -> None:
    op = UpsertPerson(
        matching_attribute="email",
        email="alice@example.test",
        title="Head of Eng",
        city="Brooklyn",
        state="NY",
        zipcode="11201",
        merge_only_if_empty=["title", "city", "state", "zipcode"],
    )
    assert op.title == "Head of Eng"
    assert op.merge_only_if_empty == ["title", "city", "state", "zipcode"]


def test_upsert_person_defaults_preserve_existing_call_sites() -> None:
    op = UpsertPerson(matching_attribute="email", email="alice@example.test")
    assert op.title is None
    assert op.city is None
    assert op.merge_only_if_empty == []


def test_upsert_company_accepts_enrichment_fields() -> None:
    op = UpsertCompany(
        domain="example.test",
        industry="Software",
        employee_count="200-500",
        estimate_revenue="$10M-$50M",
        merge_only_if_empty=["industry", "employee_count", "estimate_revenue"],
    )
    assert op.industry == "Software"
    assert op.merge_only_if_empty == ["industry", "employee_count", "estimate_revenue"]


def test_upsert_company_defaults_preserve_existing_call_sites() -> None:
    op = UpsertCompany(domain="example.test")
    assert op.industry is None
    assert op.merge_only_if_empty == []


# AI-286 regression: identity invariant must fire on every construction path,
# not only on an explicit `model_validate` call. See src/attio/ops.py:101-108.


def test_upsert_person_validator_fires_via_model_validate() -> None:
    with pytest.raises(ValidationError):
        UpsertPerson.model_validate({"matching_attribute": "email"})


def test_upsert_person_validator_fires_for_linkedin_matching_attribute() -> None:
    with pytest.raises(ValidationError):
        UpsertPerson(matching_attribute="linkedin", email="a@b.com")


def test_upsert_person_validator_fires_for_github_handle_matching_attribute() -> None:
    with pytest.raises(ValidationError):
        UpsertPerson(matching_attribute="github_handle", email="a@b.com")


def test_person_ref_rejects_missing_attribute() -> None:
    with pytest.raises(ValidationError):
        PersonRef(value="a@b.com")  # type: ignore[call-arg]


def test_person_ref_rejects_invalid_attribute_literal() -> None:
    with pytest.raises(ValidationError):
        PersonRef(attribute="phone", value="555")  # type: ignore[arg-type]


def test_upsert_mention_related_person_validates_nested_person_ref() -> None:
    with pytest.raises(ValidationError):
        UpsertMention.model_validate(
            {
                "mention_url": "https://example.test/m/1",
                "last_action": "mention_created",
                "source_platform": "octolens",
                "source_id": "abc",
                "mention_body": "hi",
                "mention_timestamp": "2026-05-14T00:00:00Z",
                "author_handle": "@x",
                "primary_keyword": "foo",
                "related_person": {"attribute": "phone", "value": "555"},
            },
        )
