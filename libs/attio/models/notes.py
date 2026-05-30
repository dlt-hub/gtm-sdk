from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class NoteInput(BaseModel):
    title: str
    content: str
    parent_object: str
    parent_record_id: str | None = None
    parent_email: str | None = None
    parent_domain: str | None = None
    format: str = "plaintext"
    # Additive: Attio Notes API accepts ``created_at`` for backdating and
    # ``meeting_id`` to link a Note to a Meeting record. Default None to
    # preserve back-compat with existing callers.
    created_at: datetime | None = None
    meeting_id: str | None = None


class NoteResult(BaseModel):
    note_id: str
    title: str
    parent_object: str
    parent_record_id: str
    content_plaintext: str
    created_at: str
    # The Meeting this note is associated with, if any (Attio Notes API
    # ``meeting_id``). Used for meeting-scoped idempotency: a shared
    # person/company parent accumulates notes across many meetings, so dedup
    # keys on (title, meeting_id) — see ai-gez.
    meeting_id: str | None = None
    raw: dict[str, Any] = {}
