from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AttributeCreateResult(BaseModel):
    mode: Literal["preview", "apply"]
    attribute_title: str
    attribute_slug: str
    attribute_type: str
    attribute_exists: bool
    attribute_created: bool


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
