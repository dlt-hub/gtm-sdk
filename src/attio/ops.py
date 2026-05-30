"""Attio operation vocabulary.

Source-agnostic Pydantic models that describe what to write to Attio. Each
source webhook produces a list of ``AttioOp`` values; the dispatcher in
``src.attio.export`` turns them into Attio SDK calls.

This module imports only from ``pydantic``. It does NOT import from
``libs.attio.*`` so the vocabulary stays a pure data definition. Conversion
between ops and lib-side input types lives in the dispatcher.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PersonRef(BaseModel):
    """Identifies an Attio Person by exactly one Attio attribute.

    The `attribute` names which Attio Person attribute to match on; `value` is the literal
    value of that attribute. New identity attributes are added by extending the Literal union.
    """

    model_config = ConfigDict(extra="forbid")

    ref_kind: Literal["person"] = "person"
    attribute: Literal["email", "linkedin", "github_handle"]
    value: str


class CompanyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_kind: Literal["company"] = "company"
    domain: str


class MeetingRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_kind: Literal["meeting"] = "meeting"
    # Matches libs.attio.models.MeetingExternalRef.ical_uid — the key the
    # dispatcher's LookupTable uses to resolve meetings already created earlier
    # in the same plan.
    ical_uid: str


# A note/record reference always resolves to a *standard object* record
# (person or company). Meetings are deliberately excluded: Attio's Notes API
# rejects ``parent_object="meetings"`` (a meeting is a first-class
# ``/v2/meetings`` resource, not an object), so a note can never be *parented*
# to a meeting — it is only *associated* via ``UpsertNote.meeting`` /
# ``meeting_id``. See ai-gez and the Step 0 probe.
Ref = Annotated[
    Union[PersonRef, CompanyRef],
    Field(discriminator="ref_kind"),
]


class MeetingExternalRef(BaseModel):
    """Mirror of ``libs.attio.models.MeetingExternalRef``.

    Duplicated so this module can stay free of ``libs/*`` imports. The
    dispatcher copies field-by-field into the lib-side type.
    """

    model_config = ConfigDict(extra="forbid")

    ical_uid: str
    provider: Literal["google", "outlook"] = "google"
    is_recurring: bool = False
    original_start_time: str | None = None


class MeetingParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_address: str
    is_organizer: bool
    # Sources without an RSVP signal (e.g. Fathom — its calendar_invitees payload
    # carries no status) fall back to this default, which means participant.status
    # on Fathom-sourced meetings is not trustworthy. Cal.com and other sources with
    # actual RSVP state should pass it through.
    status: Literal["accepted", "tentative", "declined", "pending"] = "accepted"


class UpsertPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_type: Literal["upsert_person"] = "upsert_person"
    matching_attribute: Literal["email", "linkedin", "github_handle"]
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    linkedin: str | None = None
    github_handle: str | None = None
    github_url: str | None = None
    phone: str | None = None
    company_domain: str | None = None
    title: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    merge_only_if_empty: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_match_value(self) -> UpsertPerson:
        if not getattr(self, self.matching_attribute):
            raise ValueError(
                f"UpsertPerson.matching_attribute={self.matching_attribute!r} "
                f"requires the corresponding field to be set.",
            )
        return self


class UpsertCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_type: Literal["upsert_company"] = "upsert_company"
    domain: str
    name: str | None = None
    industry: str | None = None
    employee_count: str | None = None
    estimate_revenue: str | None = None
    # LinkedIn company-page URL (``/company/<slug>`` shape). Source webhooks
    # (rb2b) discriminate ``/in/<handle>`` vs ``/company/<slug>`` before
    # populating this. The dispatcher writes it to the Company ``linkedin``
    # slug; non-company-page URLs are rejected by
    # ``libs.attio.values.format_company_linkedin``.
    linkedin_url: str | None = None
    merge_only_if_empty: list[str] = Field(default_factory=list)


class UpsertMeeting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_type: Literal["upsert_meeting"] = "upsert_meeting"
    external_ref: MeetingExternalRef
    title: str
    description: str
    start: datetime
    end: datetime
    is_all_day: bool = False
    participants: list[MeetingParticipant]
    linked_records: list[Ref] = Field(default_factory=list)


class UpsertNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_type: Literal["upsert_note"] = "upsert_note"
    # The note hangs off a standard-object record (person/company). Attio's
    # Notes API requires ``parent_object`` to be an object slug/ID, so a
    # meeting can never be the parent (ai-gez).
    parent: Ref
    # Optional association to an Attio Meeting. The dispatcher resolves this
    # ``MeetingRef`` against the plan's LookupTable (the ``UpsertMeeting`` runs
    # earlier) and passes the meeting's record_id as the Notes API's
    # ``meeting_id`` field, so the note also surfaces on the meeting timeline.
    meeting: MeetingRef | None = None
    title: str
    content: str


class UpsertMention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op_type: Literal["upsert_mention"] = "upsert_mention"
    mention_url: str
    last_action: Literal["mention_created", "mention_updated"]
    source_platform: str
    source_id: str
    mention_title: str | None = None
    mention_body: str
    mention_timestamp: datetime
    author_handle: str
    author_profile_url: str | None = None
    author_avatar_url: str | None = None
    relevance_score: str | None = None
    relevance_comment: str | None = None
    primary_keyword: str
    keywords: list[str] = Field(default_factory=list)
    octolens_tags: list[str] = Field(default_factory=list)
    sentiment: Literal["Positive", "Neutral", "Negative"] | None = None
    language: str | None = None
    subreddit: str | None = None
    view_id: int | None = None
    view_name: str | None = None
    bookmarked: bool = False
    image_url: str | None = None
    related_person: PersonRef | None = None


class UpsertTrackingEvent(BaseModel):
    """Source-agnostic op for creating/updating a `tracking_events` row.

    The dispatcher resolves `subject_person` and `subject_company` via the
    plan's LookupTable; the libs/attio adapter is called with already-resolved
    record IDs. Every field maps to a real attribute on the live prod
    ``tracking_events`` schema (confirmed 2026-05-26) — see
    ``libs.attio.values.build_tracking_event_values`` for the crosswalk.
    """

    model_config = ConfigDict(extra="forbid")

    op_type: Literal["upsert_tracking_event"] = "upsert_tracking_event"

    external_id: str
    source: str
    name: str
    event_type: Literal["rb2b_visit"]
    event_subtype: str | None = None
    event_timestamp: datetime
    body_json: str

    captured_url: str | None = None
    referrer: str | None = None
    is_repeat_visit: bool | None = None
    tags: list[str] = Field(default_factory=list)
    # Attio ``location`` attribute shape — build with
    # ``libs.attio.values.format_location_from_parts``.
    location: dict[str, Any] | None = None

    subject_person: PersonRef | None = None
    subject_company: CompanyRef | None = None


class EmitMeetingLifecycleEvent(BaseModel):
    """Source-agnostic op for the per-meeting tracking_events lifecycle row.

    One op per cal.com webhook arrival. The dispatcher PATCHes a single
    ``tracking_events`` row per meeting (keyed by ``external_id``), advancing
    ``event_subtype`` and appending to the cumulative ``details``.

    The op carries a ``PersonRef`` for the host. The dispatcher's
    ``LookupTable`` resolves it to a record_id — earlier ops in the same plan
    (``UpsertCompany`` + ``UpsertPerson`` for the host) populate the table. The
    host upsert is therefore part of every emit-lifecycle plan and ensures the
    ``people`` slug on the row is always populated.

    Used by cal.com because Attio's ``/v2/meetings/`` is append-only — meeting
    state mutations have nowhere else to land. See plan-02 for the API probe,
    and the spec at
    ``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``
    for the per-meeting row model.
    """

    model_config = ConfigDict(extra="forbid")

    op_type: Literal["emit_meeting_lifecycle_event"] = "emit_meeting_lifecycle_event"
    # ``canonical_meeting_uid(host_email, start)``. Same value as the Attio
    # Meeting record's ``external_ref.ical_uid`` — the cross-reference between
    # the tracking_events row and the Meeting record.
    external_id: str
    # Cal.com booking title, combined with ``event_subtype`` to form ``name``.
    meeting_title: str
    event_subtype: Literal[
        "scheduled",
        "cancelled",
        "rescheduled",
        "no_show_attendee",
        "no_show_host",
        "completed",
    ]
    # Webhook ``createdAt``. Updates on every transition.
    timestamp: datetime
    # Raw webhook payload, JSON-stringified. Overwritten on every transition.
    body_json: str
    # One-line summary appended to cumulative ``details``. See spec for the
    # per-variant format.
    details_line: str
    # Reference to the host's Person record. Resolved by ``LookupTable`` from
    # an earlier ``UpsertPerson`` op in the same plan.
    host: PersonRef


AttioOp = Annotated[
    Union[
        UpsertPerson,
        UpsertCompany,
        UpsertMeeting,
        UpsertNote,
        UpsertMention,
        UpsertTrackingEvent,
        EmitMeetingLifecycleEvent,
    ],
    Field(discriminator="op_type"),
]
