"""Pydantic models for Cal.com domain entities.

Cal.com webhooks ship two distinct payload shapes and seven triggers:

- ``BOOKING_CREATED`` uses ``start``/``end``/``hosts[]``/``icsUid``.
- ``BOOKING_CANCELLED``/``BOOKING_RESCHEDULED``/``PING`` use ``startTime``/
  ``endTime``/``organizer``/``iCalUID`` (different casing).
- ``BOOKING_NO_SHOW_UPDATED`` is slim: ``bookingUid`` + ``attendees`` only.
- ``MEETING_STARTED``/``MEETING_ENDED`` ship the booking fields flat at the top
  level (no ``payload`` wrap).

The ``Webhook`` envelope normalizes the flat shape into the wrapped shape and
dispatches the inner payload to one of seven ``BaseModel`` variants via a
Pydantic discriminated union keyed on ``triggerEvent``. Each variant declares
the fields it actually has; ``extra="allow"`` keeps us forward-compatible with
Cal.com adding fields without versioning.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

import orjson
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


# ---------- Sub-models reused across variants ----------


class EventType(BaseModel):
    id: int
    slug: str


class BookingHost(BaseModel):
    """Host participant. Real cal.com v2 payloads use ``organizer`` instead and
    omit ``hosts`` entirely, so every field beyond identity is optional and
    ``extra="allow"`` keeps unknown keys."""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    name: str | None = None
    email: str
    displayEmail: str | None = None
    username: str | None = None
    timeZone: str | None = None


class BookingAttendee(BaseModel):
    """Attendee. Real cal.com v2 attendees carry ``name``/``email``/``timeZone``
    but NOT ``displayEmail``/``absent`` (those were in the synthetic fixtures),
    so make them optional. ``timeZone`` is surfaced in the Slack card."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    email: str
    displayEmail: str | None = None
    timeZone: str | None = None
    absent: bool = False
    phoneNumber: str | None = None


class Organizer(BaseModel):
    """Organizer on the booking-mutation shape (CANCELLED/RESCHEDULED/PING/...)."""

    model_config = ConfigDict(extra="allow")

    email: str
    id: int | None = None
    name: str | None = None
    username: str | None = None
    timeZone: str | None = None


class MutationAttendee(BaseModel):
    """Attendee on the booking-mutation and meeting shapes.

    Slimmer than ``BookingAttendee``: ``absent`` is absent, but ``noShow`` may be
    present on meeting payloads. Fields beyond email are typically nullable.
    """

    model_config = ConfigDict(extra="allow")

    email: str
    name: str | None = None
    timeZone: str | None = None
    noShow: bool | None = None
    phoneNumber: str | None = None


class NoShowAttendee(BaseModel):
    """Attendee on BOOKING_NO_SHOW_UPDATED: only email + noShow flag."""

    model_config = ConfigDict(extra="allow")

    email: str
    noShow: bool = False


class RecordingItem(BaseModel):
    """Cal.com recording metadata (used by call_recordings flows, not webhooks)."""

    id: str
    roomName: str
    startTs: int
    status: str
    duration: int
    shareToken: str
    maxParticipants: int | None = None
    downloadLink: str | None = None
    error: str | None = None


class Transcript(BaseModel):
    urls: list[str]


# ---------- Per-trigger payload variants (discriminated by ``triggerEvent``) ----------
#
# Each variant carries ``triggerEvent`` as a ``Literal`` so the union below can
# dispatch. The envelope validator injects the trigger string into the payload
# dict before validation so the literal matches even though Cal.com keeps
# ``triggerEvent`` at the envelope level on the wrapped shapes.


class BookingCreatedPayload(BaseModel):
    """BOOKING_CREATED payload.

    ``hosts`` is the canonical place to look for the meeting host email, but
    real-world Cal.com bookings sometimes ship without it (e.g. older webhook
    versions, team bookings configured a specific way). ``ai-4u6`` broadened
    the extractor to fall back to ``organizer.email``, ``user.email``, and
    ``userPrimaryEmail`` in that order. The helper ``creator_email`` below
    walks the same chain so the gate doesn't silently drop valid payloads.
    """

    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["BOOKING_CREATED"]
    uid: str
    # Real cal.com v2 payloads send ``startTime``/``endTime``; the older
    # synthetic fixtures used ``start``/``end``. Accept either via alias so the
    # Python attribute stays ``start``/``end`` and downstream builders are
    # unchanged. (This was the root cause of live BOOKING_CREATED 422s — the
    # model required ``start`` but cal.com only sends ``startTime``.)
    start: datetime = Field(validation_alias=AliasChoices("start", "startTime"))
    end: datetime = Field(validation_alias=AliasChoices("end", "endTime"))
    # Real payloads carry both ``title`` and ``eventTitle``; prefer ``title``.
    title: str | None = Field(
        default=None,
        validation_alias=AliasChoices("title", "eventTitle"),
    )
    description: str | None = Field(
        default=None,
        validation_alias=AliasChoices("description", "eventDescription"),
    )
    additionalNotes: str | None = None
    # Permissive: ``_caldotcom_status_to_attio`` normalizes unknown values
    # safely. Locking this to a Literal would reject any future Cal.com
    # status string and silently drop otherwise-valid bookings.
    status: str | None = None
    hosts: list[BookingHost] = Field(default_factory=list)
    attendees: list[BookingAttendee] = Field(default_factory=list)
    bookingFieldsResponses: dict[str, Any] = Field(default_factory=dict)
    icsUid: str | None = None
    # Fallback host-email sources for variants that omit ``hosts``. Kept as
    # optional fields so the discriminator and ``extra="allow"`` work together.
    organizer: Organizer | None = None
    user: dict[str, Any] | None = None
    userPrimaryEmail: str | None = None

    def creator_email(self) -> str | None:
        """Resolve the meeting creator's email across known Cal.com variants."""
        for host in self.hosts:
            if host.email:
                return host.email
        if self.organizer and self.organizer.email:
            return self.organizer.email
        if isinstance(self.user, dict):
            user_email = self.user.get("email")
            if isinstance(user_email, str) and user_email:
                return user_email
        if self.userPrimaryEmail:
            return self.userPrimaryEmail
        return None


class BookingRequestedPayload(BookingCreatedPayload):
    """BOOKING_REQUESTED — a booking on a "requires confirmation" event type,
    pending host acceptance. Same payload shape as BOOKING_CREATED (startTime,
    organizer, attendees, ...), so it inherits all fields + ``creator_email``;
    only the discriminator differs. cal.com fires this immediately on request
    and follows with BOOKING_CREATED once the host accepts."""

    triggerEvent: Literal["BOOKING_REQUESTED"]  # type: ignore[assignment]


class BookingCancelledPayload(BaseModel):
    """CANCELLED payload.

    Most Cal.com cancellations carry ``organizer.email`` but real-world
    payloads occasionally omit ``organizer`` (e.g. older webhook versions).
    The previous handler walked the same fallback chain as CREATED, so we
    keep it permissive here and resolve via ``creator_email()``.
    """

    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["BOOKING_CANCELLED"]
    uid: str
    startTime: datetime
    endTime: datetime
    title: str | None = None
    additionalNotes: str | None = None
    organizer: Organizer | None = None
    attendees: list[MutationAttendee] = Field(default_factory=list)
    iCalUID: str | None = None
    cancellationReason: str | None = None
    cancelledBy: str | None = None
    user: dict[str, Any] | None = None
    userPrimaryEmail: str | None = None

    def creator_email(self) -> str | None:
        if self.organizer and self.organizer.email:
            return self.organizer.email
        if isinstance(self.user, dict):
            email = self.user.get("email")
            if isinstance(email, str) and email:
                return email
        if self.userPrimaryEmail:
            return self.userPrimaryEmail
        return None


class BookingRescheduledPayload(BaseModel):
    """Reschedule payload.

    Cal.com semantics (confirmed 2026-05-25):
        ``startTime`` = OLD pre-reschedule start time.
        ``rescheduleStartTime`` = NEW post-reschedule start time.

    Counterintuitive; document this everywhere it's relied on. Same
    organizer-fallback rationale as ``BookingCancelledPayload``.
    """

    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["BOOKING_RESCHEDULED"]
    uid: str
    startTime: datetime
    endTime: datetime
    rescheduleUid: str | None = None
    rescheduleStartTime: datetime | None = None
    rescheduleEndTime: datetime | None = None
    rescheduledBy: str | None = None
    title: str | None = None
    additionalNotes: str | None = None
    organizer: Organizer | None = None
    attendees: list[MutationAttendee] = Field(default_factory=list)
    iCalUID: str | None = None
    cancellationReason: str | None = None
    user: dict[str, Any] | None = None
    userPrimaryEmail: str | None = None

    def creator_email(self) -> str | None:
        if self.organizer and self.organizer.email:
            return self.organizer.email
        if isinstance(self.user, dict):
            email = self.user.get("email")
            if isinstance(email, str) and email:
                return email
        if self.userPrimaryEmail:
            return self.userPrimaryEmail
        return None


class BookingNoShowPayload(BaseModel):
    """Slim payload: too thin to compute ``canonical_meeting_uid`` directly.

    The webhook handler must fetch the full booking via the Cal.com API
    (``GET /v2/bookings/{bookingUid}``) to resolve host email + start time.
    """

    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["BOOKING_NO_SHOW_UPDATED"]
    bookingUid: str
    bookingId: int | None = None
    attendees: list[NoShowAttendee] = Field(default_factory=list)
    message: str | None = None


class MeetingStartedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["MEETING_STARTED"]
    uid: str
    startTime: datetime
    endTime: datetime
    userPrimaryEmail: str
    title: str | None = None
    attendees: list[MutationAttendee] = Field(default_factory=list)
    iCalUID: str | None = None


class MeetingEndedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["MEETING_ENDED"]
    uid: str
    startTime: datetime
    endTime: datetime
    userPrimaryEmail: str
    title: str | None = None
    attendees: list[MutationAttendee] = Field(default_factory=list)
    iCalUID: str | None = None
    rating: int | None = None
    ratingFeedback: str | None = None
    noShowHost: bool = False


class PingPayload(BaseModel):
    """Cal.com connectivity check. Shape varies; only ``triggerEvent`` is required."""

    model_config = ConfigDict(extra="allow")

    triggerEvent: Literal["PING"]


CalcomPayload = Annotated[
    Union[
        BookingCreatedPayload,
        BookingRequestedPayload,
        BookingCancelledPayload,
        BookingRescheduledPayload,
        BookingNoShowPayload,
        MeetingStartedPayload,
        MeetingEndedPayload,
        PingPayload,
    ],
    Field(discriminator="triggerEvent"),
]


class Webhook(BaseModel):
    """Cal.com webhook envelope.

    Cal.com delivers webhooks wrapped in ``{"body": "<json string>"}``. The
    inner payload has two shapes depending on ``triggerEvent``:

    - Booking events (``BOOKING_CREATED``, ``BOOKING_CANCELLED``,
      ``BOOKING_RESCHEDULED``, ``BOOKING_NO_SHOW_UPDATED``, ``PING``) wrap
      booking fields under a nested ``payload`` key.
    - Meeting events (``MEETING_STARTED``, ``MEETING_ENDED``) put booking fields
      flat at the top level alongside ``triggerEvent``.

    The validator normalizes both shapes and injects ``triggerEvent`` into the
    payload dict so the discriminated union can dispatch.
    """

    model_config = ConfigDict(extra="allow")

    triggerEvent: str
    createdAt: datetime
    payload: CalcomPayload

    @model_validator(mode="before")
    @classmethod
    def _unwrap_and_normalize(cls, data: Any) -> Any:
        # Step 1: unwrap Hookdeck-style ``{"body": "<json>"}``.
        if isinstance(data, dict) and "body" in data and "triggerEvent" not in data:
            body = data["body"]
            if isinstance(body, (bytes, bytearray, memoryview, str)):
                data = orjson.loads(body)

        if not isinstance(data, dict):
            return data

        # Step 2: lift the flat meeting shape into the wrapped envelope shape.
        # Flat shape: triggerEvent at top, no ``payload`` key.
        # Wrapped shape: triggerEvent + createdAt + payload{}.
        if "triggerEvent" in data and "payload" not in data:
            trigger = data.get("triggerEvent")
            created = data.get("createdAt")
            payload = {
                k: v for k, v in data.items() if k not in ("triggerEvent", "createdAt")
            }
            data = {
                "triggerEvent": trigger,
                "createdAt": created,
                "payload": payload,
            }

        # Step 3: inject ``triggerEvent`` into the payload dict so the
        # discriminator can dispatch. Wrapped Cal.com payloads don't carry it
        # internally — discriminator lookup fails without this.
        if (
            isinstance(data.get("payload"), dict)
            and "triggerEvent" not in data["payload"]
        ):
            data["payload"]["triggerEvent"] = data.get("triggerEvent")

        return data
