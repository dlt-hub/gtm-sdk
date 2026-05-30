"""Pydantic models for rb2b visit webhook payloads.

rb2b can deliver visits in two shapes:

1. The internal envelope used by saved samples / Hookdeck replays::

       {
           "event_id": "evt_...",
           "timestamp": "<iso8601>",
           "connection": "<connection name>",
           "payload": {"LinkedIn URL": "...", "Company Name": "...", ...}
       }

2. The direct-from-rb2b shape documented at
   https://support.rb2b.com/en/articles/8976614-setup-guide-webhook —
   the visit fields are flat at the top level with no envelope.

``Webhook`` accepts either shape via a ``model_validator(mode="before")``
that synthesizes an envelope when one is missing. The inner payload uses
PascalCase-with-spaces keys, aliased to snake_case Python attributes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_RB2B_DOC_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ]"
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}):(?P<frac>\d+(?:\.\d+)?)"
    r"(?P<tzsign>[+-])(?P<tzh>\d{2})\.(?P<tzm>\d{2})$",
)

# Space-separated form seen in the archived raw payloads (audited 2026-05-29):
# `2026-05-11 21:04:43 +0000` — date/time split by a space, a space before the
# tz offset, and a colon-less `+HHMM` offset. Pydantic v2 rejects all three.
_RB2B_SPACE_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ T]"
    r"(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"\s*(?P<tzsign>[+-])(?P<tzh>\d{2}):?(?P<tzm>\d{2})$",
)


def normalize_rb2b_timestamp(value: Any) -> Any:
    """Normalize rb2b's non-ISO timestamp shapes to ISO 8601.

    rb2b emits timestamps in several forms pydantic v2 can't parse:

    * The documented ``12:34:56:00.00+00.00`` shape — sub-second separator is
      ``:`` instead of ``.`` and the tz offset is dot-separated.
    * The space-separated ``2026-05-11 21:04:43 +0000`` shape found across the
      raw GCS archive — space between date and time, a space before the tz, and
      a colon-less ``+HHMM`` offset.

    Both are rewritten to valid ISO 8601. Already-ISO strings pass through the
    space-form branch unchanged (it re-emits the same value), and anything
    unrecognized is returned untouched so pydantic can apply its built-in
    parsing.
    """
    if not isinstance(value, str):
        return value
    match = _RB2B_DOC_TS_RE.match(value)
    if match is not None:
        g = match.groupdict()
        frac = g["frac"].replace(".", "")
        return (
            f"{g['date']}T{g['h']}:{g['m']}:{g['s']}.{frac}"
            f"{g['tzsign']}{g['tzh']}:{g['tzm']}"
        )
    space_match = _RB2B_SPACE_TS_RE.match(value)
    if space_match is not None:
        g = space_match.groupdict()
        return f"{g['date']}T{g['time']}{g['tzsign']}{g['tzh']}:{g['tzm']}"
    return value


class Payload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    linkedin_url: str | None = Field(default=None, alias="LinkedIn URL")
    first_name: str | None = Field(default=None, alias="First Name")
    last_name: str | None = Field(default=None, alias="Last Name")
    title: str | None = Field(default=None, alias="Title")
    company_name: str | None = Field(default=None, alias="Company Name")
    business_email: str | None = Field(default=None, alias="Business Email")
    website: str | None = Field(default=None, alias="Website")
    industry: str | None = Field(default=None, alias="Industry")
    employee_count: str | None = Field(default=None, alias="Employee Count")
    estimate_revenue: str | None = Field(default=None, alias="Estimate Revenue")
    city: str | None = Field(default=None, alias="City")
    state: str | None = Field(default=None, alias="State")
    zipcode: str | None = Field(default=None, alias="Zipcode")
    seen_at: datetime | None = Field(default=None, alias="Seen At")
    referrer: str | None = Field(default=None, alias="Referrer")
    tags: str | None = Field(default=None, alias="Tags")
    captured_url: str | None = Field(default=None, alias="Captured URL")
    is_repeat_visit: bool | None = None

    @field_validator("employee_count", mode="before")
    @classmethod
    def _coerce_employee_count(cls, value: Any) -> Any:
        """rb2b documents Employee Count as `integer, string, null`."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return str(value)
        return value

    @field_validator("seen_at", mode="before")
    @classmethod
    def _normalize_seen_at(cls, value: Any) -> Any:
        return normalize_rb2b_timestamp(value)


_ENVELOPE_KEYS = frozenset({"event_id", "timestamp", "connection", "payload"})

# Wire-shape (PascalCase) payload keys that identify a single visit. rb2b
# delivers visits flat with these keys; we hash them to derive a stable id.
_IDENTITY_KEYS: tuple[str, ...] = (
    "Business Email",
    "LinkedIn URL",
    "Captured URL",
    "Seen At",
)


def compute_event_id(payload: dict[str, Any]) -> str:
    """Derive a deterministic ``event_id`` from a flat visit ``payload``.

    rb2b-direct deliveries carry no event_id, so historical replays (the
    GCS / Hookdeck backfill) and live traffic would otherwise mint different
    random ids for the *same* visit — each producing a distinct Attio
    tracking-event ``external_id`` (``rb2b:{event_id}``) and defeating the
    idempotent upsert in ``libs/attio/tracking_events.py``.

    Hashing the visit's identity (business email, LinkedIn URL, captured URL,
    and ``Seen At``) makes the id a pure function of content: live ingestion
    and any number of replays converge on one id, and re-running the backfill
    never duplicates rows. ``Seen At`` is part of the identity by design —
    each timestamped hit is a distinct event.

    ``Seen At`` is normalized **here** (not just by the caller) so the id is
    independent of which timestamp shape the archive happens to store: the
    live webhook path normalizes before wrapping, while the backfill hashes the
    raw archived payload. Without normalizing inside, a space-separated
    ``2026-05-11 21:04:43 +0000`` would hash differently from its ISO form and
    the two paths would diverge — exactly the convergence this function exists
    to guarantee.

    The ``payload`` is the wire shape (PascalCase keys). Identity components are
    JSON-serialized (not delimiter-joined) so a field that legitimately
    contains a separator — e.g. a captured URL with ``|`` — can't collide with
    a different field layout. Fully-anonymous visits (no identity field set)
    fall back to hashing the whole payload so they don't all collapse onto a
    single id.
    """
    identity = [
        _canonical_seen_at(payload.get(key)) if key == "Seen At" else payload.get(key)
        for key in _IDENTITY_KEYS
    ]
    identity = [str(v or "") for v in identity]
    if any(identity):
        basis = json.dumps(identity)
    else:
        basis = json.dumps(payload, sort_keys=True, default=str)
    return f"evt_{hashlib.sha256(basis.encode()).hexdigest()[:32]}"


def _canonical_seen_at(value: Any) -> str:
    """Canonicalize a ``Seen At`` value to a single instant for hashing.

    Normalizing the textual shape isn't enough: ``...:43.000+00:00`` and
    ``...:43+00:00`` denote the same instant but differ as strings, so they'd
    hash differently. Parse the normalized form to a UTC epoch so equal instants
    always produce the same hash basis regardless of sub-second precision or
    timezone notation. Unparseable values fall back to their normalized string.

    An offset-less (naive) timestamp is treated as UTC, *not* the host's local
    timezone — ``astimezone`` would otherwise interpret a naive value relative
    to wherever the code runs, making the id host-dependent and breaking the
    live/replay convergence this exists to guarantee.
    """
    norm = normalize_rb2b_timestamp(value)
    if isinstance(norm, str) and norm:
        try:
            parsed = datetime.fromisoformat(norm)
        except ValueError:
            return norm
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return str(parsed.astimezone(timezone.utc).timestamp())
    return ""


class Webhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    timestamp: datetime
    connection: str
    payload: Payload

    @model_validator(mode="before")
    @classmethod
    def _wrap_flat_payload(cls, data: Any) -> Any:
        """Synthesize an envelope when rb2b posts the visit flat.

        Direct rb2b webhook deliveries put visit fields at the top level
        without an envelope. Detect that shape (no envelope keys + a
        recognizable visit key) and wrap it so the rest of the model
        sees the canonical structure. ``seen_at`` is also normalized
        here so the synthesized envelope timestamp is parseable.
        """
        if not isinstance(data, dict):
            return data
        if "payload" in data or _ENVELOPE_KEYS.issubset(data.keys()):
            return data
        looks_flat = any(
            key in data
            for key in ("LinkedIn URL", "Company Name", "Seen At", "Captured URL")
        )
        if not looks_flat:
            return data

        seen_at = normalize_rb2b_timestamp(data.get("Seen At"))
        envelope_ts = (
            seen_at
            if isinstance(seen_at, str)
            else datetime.now(timezone.utc).isoformat()
        )
        flat_payload = dict(data)
        if seen_at is not None:
            flat_payload["Seen At"] = seen_at
        return {
            "event_id": data.get("event_id") or compute_event_id(flat_payload),
            "timestamp": envelope_ts,
            "connection": data.get("connection") or "rb2b-direct",
            "payload": flat_payload,
        }
