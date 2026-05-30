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
    # ``attribute_title_updated`` records that an apply PATCHed the live title to
    # match the declared title. ``create_attribute`` is otherwise add-only, but a
    # title is the one field it CAN converge in place (type/flag changes still
    # need manual handling in Attio). Without this, a workspace created with an
    # older title shows permanent, non-converging drift in ``--diff``.
    attribute_title_updated: bool = False
    # ``attribute_title_drifts`` is the observed pre-state: the slug is present
    # but its live title differs from the declared title. Set in BOTH preview and
    # apply so a ``--preview`` run can report the title PATCH that ``--apply``
    # would perform, instead of mislabeling the attribute "exists (skip)".
    attribute_title_drifts: bool = False


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
