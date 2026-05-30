from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AttributeCreateResult(BaseModel):
    mode: Literal["preview", "apply"]
    attribute_title: str
    attribute_slug: str
    attribute_type: str
    # ``attribute_exists`` is the active-and-present pre-state (historical
    # meaning). ``attribute_archived`` is the observed pre-state for a slug that
    # is present but archived (is_archived=True) — hidden from the default
    # attributes list, so a naive create would POST and 409 on the still-reserved
    # slug (the non-idempotent bug behind the ai-ica prod bootstrap crash). Both
    # are populated in preview and apply so callers can report would-create vs
    # would-restore. ``attribute_restored`` records that an apply un-archived it.
    attribute_exists: bool
    attribute_created: bool
    attribute_archived: bool = False
    attribute_restored: bool = False


class AttributeInfo(BaseModel):
    """A live attribute read back from a workspace, normalized for diffing.

    ``allowed_objects`` holds api_slugs (the wire format returns object IDs;
    :func:`libs.attio.attributes.list_attributes` resolves them to slugs so
    callers can compare against slug-based declarations). ``is_system`` flags
    Attio's built-in attributes (record_id, created_at, ...) so schema diffs can
    ignore them when reporting "present in workspace but not declared".
    """

    api_slug: str
    title: str
    attribute_type: str
    is_multiselect: bool
    is_unique: bool
    is_required: bool
    is_archived: bool
    is_system: bool
    allowed_objects: tuple[str, ...] = ()
