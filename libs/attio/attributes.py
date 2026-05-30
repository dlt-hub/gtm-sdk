from __future__ import annotations

from attio.errors.sdkerror import SDKError

from libs.attio.client import get_client
from libs.attio.models import AttributeCreateResult, AttributeInfo


def list_attributes(
    target_object: str,
    *,
    show_archived: bool = False,
) -> list[AttributeInfo]:
    """Return the attributes on ``target_object``.

    Read-only. Powers live-vs-declared schema diffing (see
    ``scripts/attio-bootstrap-social_mentions.py --diff``). For
    ``record-reference`` attributes the wire format returns target object *IDs*;
    this resolves them back to api_slugs via a single ``get_v2_objects`` lookup
    so callers can compare against slug-based declarations. Returns ``[]`` when
    the object does not exist yet (404), mirroring :func:`create_attribute`.

    By default Attio hides archived attributes; pass ``show_archived=True`` to
    include them (each carries ``is_archived``). A diff that wants to tell
    "archived" (``--apply`` will restore) from "absent" (``--apply`` creates)
    must opt in — otherwise an archived slug looks plainly missing.
    """
    with get_client() as client:
        try:
            response = client.attributes.get_v2_target_identifier_attributes(
                target="objects",
                identifier=target_object,
                show_archived=show_archived,
            )
        except SDKError as exc:
            status = getattr(getattr(exc, "raw_response", None), "status_code", None)
            if status != 404:
                raise
            return []

        attrs = list(response.data)
        # Only pay for the objects lookup when a record-reference attribute is
        # present — the common case (text/select/etc.) needs no id->slug map.
        id_to_slug: dict[str, str] = {}
        if any(getattr(a, "type", "") == "record-reference" for a in attrs):
            objects_response = client.objects.get_v2_objects()
            for obj in objects_response.data:
                obj_id = getattr(getattr(obj, "id", None), "object_id", None)
                slug = getattr(obj, "api_slug", None)
                if obj_id and slug:
                    id_to_slug[obj_id] = slug

        result: list[AttributeInfo] = []
        for a in attrs:
            allowed: tuple[str, ...] = ()
            if getattr(a, "type", "") == "record-reference":
                record_reference = getattr(
                    getattr(a, "config", None),
                    "record_reference",
                    None,
                )
                allowed_ids = (
                    getattr(record_reference, "allowed_object_ids", None) or []
                )
                # Fall back to the raw id if a slug can't be resolved, so the
                # diff still surfaces *something* rather than silently dropping.
                allowed = tuple(
                    id_to_slug.get(str(i), str(i)) for i in allowed_ids if i
                )
            result.append(
                AttributeInfo(
                    api_slug=getattr(a, "api_slug", ""),
                    title=getattr(a, "title", ""),
                    attribute_type=getattr(a, "type", ""),
                    is_multiselect=bool(getattr(a, "is_multiselect", False)),
                    is_unique=bool(getattr(a, "is_unique", False)),
                    is_required=bool(getattr(a, "is_required", False)),
                    is_archived=bool(getattr(a, "is_archived", False)),
                    is_system=bool(getattr(a, "is_system_attribute", False)),
                    allowed_objects=allowed,
                ),
            )
        return result


def list_select_options(*, target_object: str, attribute_slug: str) -> list[str]:
    """Return the option titles on a ``select`` attribute. Read-only."""
    with get_client() as client:
        response = (
            client.attributes.get_v2_target_identifier_attributes_attribute_options(
                target="objects",
                identifier=target_object,
                attribute=attribute_slug,
            )
        )
        return [getattr(option, "title", "") for option in response.data]


def list_status_options(*, target_object: str, attribute_slug: str) -> list[str]:
    """Return the status titles on a ``status`` attribute. Read-only.

    ``status`` attributes (e.g. ``triage_status``) expose their vocabulary via a
    different endpoint than selects, and :func:`ensure_select_options` cannot
    read or seed them. The diff path uses this to surface status drift between
    workspaces even though seeding status values stays human-managed.
    """
    with get_client() as client:
        response = (
            client.attributes.get_v2_target_identifier_attributes_attribute_statuses(
                target="objects",
                identifier=target_object,
                attribute=attribute_slug,
            )
        )
        return [getattr(status, "title", "") for status in response.data]


def create_attribute(
    *,
    target_object: str,
    title: str,
    api_slug: str,
    attribute_type: str = "select",
    description: str | None = None,
    is_multiselect: bool = True,
    is_required: bool = False,
    is_unique: bool = False,
    allowed_objects: list[str] | None = None,
    apply: bool,
) -> AttributeCreateResult:
    with get_client() as client:
        try:
            # show_archived=True so an archived slug is visible here. Without it,
            # Attio hides archived attributes from the list, the slug looks free,
            # and the POST below 409s on the still-reserved slug — the
            # non-idempotent bug behind the ai-ica prod bootstrap crash. We
            # restore (un-archive) such slugs instead of recreating them.
            attributes_response = client.attributes.get_v2_target_identifier_attributes(
                target="objects",
                identifier=target_object,
                show_archived=True,
            )
            archived_by_slug = {
                getattr(attr, "api_slug", ""): bool(getattr(attr, "is_archived", False))
                for attr in attributes_response.data
            }
            title_by_slug = {
                getattr(attr, "api_slug", ""): getattr(attr, "title", "")
                for attr in attributes_response.data
            }
        except SDKError as exc:
            # Parent object does not exist yet (e.g. preview run before bootstrap).
            # Treat as "no attributes exist" so the script can report would-create
            # instead of crashing.
            status = getattr(getattr(exc, "raw_response", None), "status_code", None)
            if status != 404:
                raise
            archived_by_slug = {}
            title_by_slug = {}

        slug_present = api_slug in archived_by_slug
        slug_archived = archived_by_slug.get(api_slug, False)
        # ``attribute_exists`` keeps its historical meaning: present AND active.
        attribute_exists = slug_present and not slug_archived
        attribute_created = False
        attribute_restored = False
        attribute_title_updated = False
        # Pre-state, computed for preview and apply alike: a present slug whose
        # live title differs from the declared title. Drives the preview
        # "would-retitle" status and the apply title PATCH below.
        attribute_title_drifts = (
            slug_present and title_by_slug.get(api_slug, "") != title
        )

        if apply and not slug_present:
            config: dict[str, object] = {}
            if attribute_type == "record-reference":
                # Attio requires record-reference attributes to declare which
                # object slugs they may point at. Default to ["people"] only if
                # the caller did not specify, to avoid silently mis-targeting.
                config["record_reference"] = {
                    "allowed_objects": allowed_objects or ["people"],
                }
            payload = {
                "title": title,
                "description": description or "",
                "api_slug": api_slug,
                "type": attribute_type,
                "is_required": is_required,
                "is_unique": is_unique,
                "is_multiselect": is_multiselect,
                "config": config,
            }
            client.attributes.post_v2_target_identifier_attributes(
                target="objects",
                identifier=target_object,
                data=payload,
            )
            attribute_created = True
        elif apply and slug_archived:
            # Slug exists but archived: restore it rather than POST (which 409s).
            # Fold the declared title into the same PATCH so a restore also
            # converges any title drift in one round-trip.
            restore_data: dict[str, object] = {"is_archived": False}
            if attribute_title_drifts:
                restore_data["title"] = title
                attribute_title_updated = True
            client.attributes.patch_v2_target_identifier_attributes_attribute_(
                target="objects",
                identifier=target_object,
                attribute=api_slug,
                data=restore_data,
            )
            attribute_restored = True
        elif apply and attribute_exists and attribute_title_drifts:
            # Active attribute whose live title drifted from the declared title.
            # Title is the one field create_attribute can converge in place
            # (PATCH); type/flag changes are rejected by Attio and stay manual.
            client.attributes.patch_v2_target_identifier_attributes_attribute_(
                target="objects",
                identifier=target_object,
                attribute=api_slug,
                data={"title": title},
            )
            attribute_title_updated = True

    return AttributeCreateResult(
        mode="apply" if apply else "preview",
        attribute_title=title,
        attribute_slug=api_slug,
        attribute_type=attribute_type,
        attribute_exists=attribute_exists,
        attribute_created=attribute_created,
        attribute_archived=slug_archived,
        attribute_restored=attribute_restored,
        attribute_title_updated=attribute_title_updated,
        attribute_title_drifts=attribute_title_drifts,
    )


def ensure_select_options(
    *,
    target_object: str,
    attribute_slug: str,
    options: list[str],
) -> list[str]:
    """Idempotently ensure each option title exists on a select attribute.

    Returns the list of titles that were newly created. No-op for options that
    already exist. Used both at bootstrap time (closed vocabularies) and at
    write time (open vocabularies like keywords/tags).
    """
    if not options:
        return []
    created: list[str] = []
    with get_client() as client:
        existing_resp = (
            client.attributes.get_v2_target_identifier_attributes_attribute_options(
                target="objects",
                identifier=target_object,
                attribute=attribute_slug,
            )
        )
        existing_titles = {getattr(o, "title", "") for o in existing_resp.data}
        for title in options:
            if title in existing_titles:
                continue
            try:
                client.attributes.post_v2_target_identifier_attributes_attribute_options(
                    target="objects",
                    identifier=target_object,
                    attribute=attribute_slug,
                    data={"title": title},
                )
            except SDKError as exc:
                # Under concurrent webhook deliveries two callers can race past
                # the pre-read above and both POST the same new option title.
                # The loser gets 409; treat it as success since the option now
                # exists, which is the post-condition this function promises.
                status = getattr(
                    getattr(exc, "raw_response", None),
                    "status_code",
                    None,
                )
                if status != 409:
                    raise
                continue
            created.append(title)
    return created


def create_companies_attribute(
    *,
    title: str,
    api_slug: str,
    attribute_type: str = "select",
    description: str | None = None,
    is_multiselect: bool = True,
    is_required: bool = False,
    is_unique: bool = False,
    apply: bool,
) -> AttributeCreateResult:
    """Backward-compatible wrapper. Prefer ``create_attribute(target_object=...)`` for new code."""
    return create_attribute(
        target_object="companies",
        title=title,
        api_slug=api_slug,
        attribute_type=attribute_type,
        description=description,
        is_multiselect=is_multiselect,
        is_required=is_required,
        is_unique=is_unique,
        apply=apply,
    )
