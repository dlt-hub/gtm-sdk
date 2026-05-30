from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TrackingEventInput(BaseModel):
    """Resolved-record-id form of a tracking_events upsert.

    The dispatcher converts UpsertTrackingEvent (which carries refs) into
    this model, replacing refs with resolved Attio record IDs. Mirrors the
    full writable surface of the live prod ``tracking_events`` schema;
    every field maps to a real attribute on the live object — see
    ``build_tracking_event_values`` for the slug → field crosswalk.

    ``location`` is the structured Attio ``location`` attribute shape
    (``{line_1..4, locality, region, postcode, country_code, latitude,
    longitude}``) — same shape that ``primary_location`` uses on People.
    Build it with ``libs.attio.values.format_location_from_parts`` from
    structured city/state/zipcode.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str
    source: str
    name: str
    event_type: str
    event_subtype: str | None = None
    event_timestamp: datetime
    body_json: str

    captured_url: str | None = None
    referrer: str | None = None
    is_repeat_visit: bool | None = None
    tags: list[str] = Field(default_factory=list)
    location: dict[str, Any] | None = None

    related_person_record_id: str | None = None
    related_company_record_id: str | None = None


# Closed vocabulary for the per-row state. Mirrored in the bootstrap script's
# _EVENT_SUBTYPE_OPTIONS. Extending requires re-running the bootstrap.
MeetingLifecycleSubtype = Literal[
    "scheduled",
    "cancelled",
    "rescheduled",
    "no_show_attendee",
    "no_show_host",
    "completed",
]


class MeetingLifecycleEventInput(BaseModel):
    """Per-meeting ``tracking_events`` write for a cal.com meeting lifecycle row.

    One row per meeting, keyed by ``external_id = canonical_meeting_uid(host,
    start)``. Each cal.com webhook for the same meeting PATCHes the same row,
    advancing ``event_subtype`` and appending a new line to the cumulative
    ``details`` text. See
    ``design/backlog-202605251625-meeting_state_attrs_on_tracking_events-spec-01.md``.

    The helper writes the following slugs (all confirmed present on prod's
    ``tracking_events`` and bootstrapped onto dev): ``external_id``, ``name``,
    ``event_type`` (always ``"calcom_meeting"``), ``event_subtype``, ``body``,
    ``details``, ``no_show``, ``timestamp``, ``people``, ``owner``.

    ``contact`` (dev-only legacy slug) is intentionally NOT written. Plan-02's
    lifecycle code wrote to it, which made the codepath silently broken on
    prod where that slug doesn't exist. The dispatcher uses ``people`` going
    forward.
    """

    model_config = ConfigDict(extra="forbid")

    # ``canonical_meeting_uid(host_email, start)`` — same value as the Attio
    # Meeting record's ``external_ref.ical_uid``. Cross-reference between the
    # tracking_events row and the Meeting record without a foreign key.
    external_id: str
    # Cal.com booking title, e.g. "Acme × dlt pricing call". Combined with
    # ``event_subtype`` to form the row's ``name`` slug.
    meeting_title: str
    event_subtype: MeetingLifecycleSubtype
    # Webhook ``createdAt``. Overwritten on every transition so the row's
    # timestamp tracks the most recent state change.
    timestamp: datetime
    # Raw webhook payload, JSON-stringified. Overwritten on every transition;
    # the helper does not keep historical bodies.
    body_json: str
    # One-line summary of THIS transition, e.g.
    # "2026-05-27T08:00:00Z cancelled — by alice@dlthub.com: scheduling conflict".
    # The helper reads the existing ``details`` text, appends "\\n" + this
    # line, and writes back.
    details_line: str
    # Resolved host Person record id. The dispatcher upserts the host via
    # UpsertPerson before emitting EmitMeetingLifecycleEvent so this is always
    # set when the helper runs.
    host_person_record_id: str
    # Workspace-member id to stamp as the row's ``owner`` actor. Resolved per
    # workspace from the active token (``/v2/self``
    # authorized_by_workspace_member_id) by the orchestration layer — NOT
    # hardcoded, since member ids differ between the dev and prod workspaces
    # (ai-ica). ``None`` omits the owner field rather than writing an invalid
    # actor reference (owner is best-effort metadata, not required by schema).
    owner_member_id: str | None = None
