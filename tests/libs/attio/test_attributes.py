from __future__ import annotations

from types import SimpleNamespace

import pytest
from attio.errors.sdkerror import SDKError

from libs.attio.attributes import (
    create_companies_attribute,
    ensure_select_options,
    list_attributes,
    list_select_options,
    list_status_options,
)
from libs.attio.models import AttributeCreateResult, AttributeInfo


def _resp(data):
    return SimpleNamespace(data=data)


def _attr(slug: str, *, is_archived: bool = False, title: str | None = None):
    return SimpleNamespace(
        api_slug=slug,
        is_archived=is_archived,
        title=title if title is not None else slug,
    )


class _FakeAttributes:
    def __init__(self, attrs=None, options=None, option_post_errors=None):
        self.attrs = attrs or []
        self.created_attrs: list[dict[str, object]] = []
        self.restored_slugs: list[str] = []
        # Every PATCH (restore and/or title reconcile) recorded as (slug, data).
        self.patch_calls: list[tuple[str, dict[str, object]]] = []
        self.options = options or []
        self.created_options: list[dict[str, object]] = []
        # Mapping of option title -> Exception to raise when POSTed. Each entry
        # is consumed at most once so subsequent calls succeed (mirrors the
        # production race where a second attempt would also conflict, but lets
        # tests assert "later titles still POST" cleanly).
        self.option_post_errors: dict[str, Exception] = dict(
            option_post_errors or {},
        )

    def get_v2_target_identifier_attributes(
        self,
        *,
        target,
        identifier,
        show_archived=None,
    ):
        assert target == "objects"
        assert identifier == "companies"
        # create_attribute always asks for archived slugs so it can restore
        # rather than 409 on a reserved-but-archived slug (ai-ica).
        assert show_archived is True
        return _resp(self.attrs)

    def patch_v2_target_identifier_attributes_attribute_(
        self,
        *,
        target,
        identifier,
        attribute,
        data,
    ):
        assert target == "objects"
        assert identifier == "companies"
        self.patch_calls.append((attribute, dict(data)))
        if data.get("is_archived") is False:
            self.restored_slugs.append(attribute)
        return _resp(_attr(attribute))

    def post_v2_target_identifier_attributes(self, *, target, identifier, data):
        assert target == "objects"
        assert identifier == "companies"
        self.created_attrs.append(data)
        self.attrs.append(_attr(data["api_slug"]))
        return _resp(_attr(data["api_slug"]))

    def get_v2_target_identifier_attributes_attribute_options(
        self,
        *,
        target,
        identifier,
        attribute,
    ):
        assert target == "objects"
        del identifier, attribute  # not asserted by these tests
        return _resp([SimpleNamespace(title=t) for t in self.options])

    def post_v2_target_identifier_attributes_attribute_options(
        self,
        *,
        target,
        identifier,
        attribute,
        data,
    ):
        assert target == "objects"
        del identifier, attribute
        title = data["title"]
        if title in self.option_post_errors:
            raise self.option_post_errors.pop(title)
        self.created_options.append(data)
        self.options.append(title)
        return _resp(SimpleNamespace(title=title))


def _make_sdk_error(status_code: int, message: str = "boom") -> SDKError:
    """Build a SDKError without spinning up a real httpx.Response.

    The real SDKError signature expects an httpx.Response, but its __init__
    only reads .status_code/.headers/.text — a SimpleNamespace is sufficient
    for the duck-typed access pattern in production (`raw_response.status_code`).
    """
    raw_response = SimpleNamespace(status_code=status_code, headers={}, text=message)
    return SDKError(message, raw_response, message)  # type: ignore[arg-type]


class _FakeClient:
    def __init__(self, attributes):
        self.attributes = attributes

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def test_create_companies_attribute_preview_reports_not_created(monkeypatch) -> None:
    fake = _FakeAttributes(attrs=[_attr("existing")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="GTM Tool Type",
        api_slug="gtm_tool_type",
        attribute_type="select",
        description="GTM stack role(s) this company serves",
        is_multiselect=True,
        apply=False,
    )

    assert isinstance(result, AttributeCreateResult)
    assert result.mode == "preview"
    assert result.attribute_title == "GTM Tool Type"
    assert result.attribute_slug == "gtm_tool_type"
    assert result.attribute_type == "select"
    assert result.attribute_exists is False
    assert result.attribute_created is False
    assert fake.created_attrs == []


def test_create_companies_attribute_apply_creates_when_missing(monkeypatch) -> None:
    fake = _FakeAttributes(attrs=[_attr("existing")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="GTM Tool Type",
        api_slug="gtm_tool_type",
        attribute_type="select",
        description="GTM stack role(s) this company serves",
        is_multiselect=True,
        apply=True,
    )

    assert result.mode == "apply"
    assert result.attribute_exists is False
    assert result.attribute_created is True
    assert len(fake.created_attrs) == 1
    assert fake.created_attrs[0]["title"] == "GTM Tool Type"
    assert fake.created_attrs[0]["api_slug"] == "gtm_tool_type"
    assert fake.created_attrs[0]["type"] == "select"
    assert fake.created_attrs[0]["is_multiselect"] is True


def test_create_companies_attribute_apply_is_noop_when_already_exists(
    monkeypatch,
) -> None:
    fake = _FakeAttributes(attrs=[_attr("gtm_tool_type", title="GTM Tool Type")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="GTM Tool Type",
        api_slug="gtm_tool_type",
        attribute_type="select",
        description="GTM stack role(s) this company serves",
        is_multiselect=True,
        apply=True,
    )

    assert result.mode == "apply"
    assert result.attribute_exists is True
    assert result.attribute_created is False
    assert result.attribute_title_updated is False
    assert fake.created_attrs == []
    assert fake.patch_calls == []


def test_create_attribute_apply_restores_archived_slug(monkeypatch) -> None:
    # The slug exists but is archived: create_attribute must un-archive it
    # (PATCH), NOT POST — POSTing 409s on the still-reserved slug. This is the
    # exact prod state behind ai-ica (no_show archived on tracking_events).
    fake = _FakeAttributes(attrs=[_attr("no_show", is_archived=True, title="No Show")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="No Show",
        api_slug="no_show",
        attribute_type="checkbox",
        is_multiselect=False,
        apply=True,
    )

    assert result.attribute_created is False
    assert result.attribute_restored is True
    assert result.attribute_archived is True
    assert result.attribute_exists is False  # pre-state: not active
    assert result.attribute_title_updated is False  # title already matched
    assert fake.created_attrs == []  # no POST attempted
    assert fake.restored_slugs == ["no_show"]
    # Title matched, so the restore PATCH carries only is_archived.
    assert fake.patch_calls == [("no_show", {"is_archived": False})]


def test_create_attribute_preview_reports_would_restore_for_archived(
    monkeypatch,
) -> None:
    fake = _FakeAttributes(attrs=[_attr("no_show", is_archived=True)])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="No Show",
        api_slug="no_show",
        attribute_type="checkbox",
        is_multiselect=False,
        apply=False,
    )

    assert result.attribute_archived is True
    assert result.attribute_exists is False
    assert result.attribute_restored is False  # preview never writes
    assert fake.created_attrs == []
    assert fake.restored_slugs == []


def test_create_attribute_apply_active_slug_is_noop(monkeypatch) -> None:
    fake = _FakeAttributes(attrs=[_attr("no_show", is_archived=False, title="No Show")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="No Show",
        api_slug="no_show",
        attribute_type="checkbox",
        is_multiselect=False,
        apply=True,
    )

    assert result.attribute_exists is True
    assert result.attribute_created is False
    assert result.attribute_restored is False
    assert result.attribute_title_updated is False
    assert fake.created_attrs == []
    assert fake.restored_slugs == []
    assert fake.patch_calls == []


def test_create_attribute_apply_updates_drifted_title(monkeypatch) -> None:
    # Active slug whose live title drifted from the declared title: --apply must
    # converge it with a title-only PATCH (the non-converging-drift gap behind
    # the ai-flm prod github_url title mismatch).
    fake = _FakeAttributes(attrs=[_attr("no_show", title="Old Show")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="No Show",
        api_slug="no_show",
        attribute_type="checkbox",
        is_multiselect=False,
        apply=True,
    )

    assert result.attribute_exists is True
    assert result.attribute_created is False
    assert result.attribute_restored is False
    assert result.attribute_title_updated is True
    assert fake.created_attrs == []
    assert fake.patch_calls == [("no_show", {"title": "No Show"})]


def test_create_attribute_apply_restore_also_fixes_title(monkeypatch) -> None:
    # An archived slug whose title also drifted: the single restore PATCH should
    # both un-archive AND retitle in one round-trip.
    fake = _FakeAttributes(
        attrs=[_attr("no_show", is_archived=True, title="Old Show")],
    )
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="No Show",
        api_slug="no_show",
        attribute_type="checkbox",
        is_multiselect=False,
        apply=True,
    )

    assert result.attribute_restored is True
    assert result.attribute_title_updated is True
    assert fake.patch_calls == [
        ("no_show", {"is_archived": False, "title": "No Show"}),
    ]


def test_create_attribute_preview_never_patches_title(monkeypatch) -> None:
    # Preview must stay read-only even when the title has drifted.
    fake = _FakeAttributes(attrs=[_attr("no_show", title="Old Show")])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    result = create_companies_attribute(
        title="No Show",
        api_slug="no_show",
        attribute_type="checkbox",
        is_multiselect=False,
        apply=False,
    )

    assert result.attribute_title_updated is False
    # Preview must still REPORT the drift so the operator sees the pending PATCH.
    assert result.attribute_title_drifts is True
    assert fake.patch_calls == []


def test_ensure_select_options_swallows_409_conflict(monkeypatch) -> None:
    fake = _FakeAttributes(
        options=[],
        option_post_errors={"kw_a": _make_sdk_error(409, "slug conflict")},
    )
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    created = ensure_select_options(
        target_object="social_mention",
        attribute_slug="keywords",
        options=["kw_a", "kw_b"],
    )

    # kw_a races a concurrent caller and 409s — not counted as created here.
    # kw_b still POSTs successfully on the same call.
    assert created == ["kw_b"]
    assert fake.created_options == [{"title": "kw_b"}]


def test_ensure_select_options_reraises_non_409_sdkerror(monkeypatch) -> None:
    fake = _FakeAttributes(
        options=[],
        option_post_errors={"kw_a": _make_sdk_error(500, "server error")},
    )
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    with pytest.raises(SDKError):
        ensure_select_options(
            target_object="social_mention",
            attribute_slug="keywords",
            options=["kw_a"],
        )


def test_ensure_select_options_skips_existing(monkeypatch) -> None:
    fake = _FakeAttributes(options=["kw_a"])
    monkeypatch.setattr("libs.attio.attributes.get_client", lambda: _FakeClient(fake))

    created = ensure_select_options(
        target_object="social_mention",
        attribute_slug="keywords",
        options=["kw_a", "kw_b"],
    )

    assert created == ["kw_b"]
    assert fake.created_options == [{"title": "kw_b"}]


# --- read helpers (list_attributes / list_select_options / list_status_options) ---


def _live_attr(
    slug: str,
    attribute_type: str = "text",
    *,
    title: str | None = None,
    is_multiselect: bool = False,
    is_unique: bool = False,
    is_required: bool = False,
    is_archived: bool = False,
    is_system_attribute: bool = False,
    allowed_object_ids: list[str] | None = None,
):
    config = SimpleNamespace(
        record_reference=SimpleNamespace(allowed_object_ids=allowed_object_ids),
    )
    return SimpleNamespace(
        api_slug=slug,
        title=title or slug,
        type=attribute_type,
        is_multiselect=is_multiselect,
        is_unique=is_unique,
        is_required=is_required,
        is_archived=is_archived,
        is_system_attribute=is_system_attribute,
        config=config,
    )


def _live_object(object_id: str, api_slug: str):
    return SimpleNamespace(id=SimpleNamespace(object_id=object_id), api_slug=api_slug)


class _FakeReadAttributes:
    def __init__(self, attrs, *, options=None, statuses=None, attrs_error=None):
        self.attrs = attrs
        self.options = options or {}
        self.statuses = statuses or {}
        self.attrs_error = attrs_error

    def get_v2_target_identifier_attributes(
        self,
        *,
        target,
        identifier,
        show_archived=False,
    ):
        assert target == "objects"
        del identifier, show_archived
        if self.attrs_error is not None:
            raise self.attrs_error
        return _resp(self.attrs)

    def get_v2_target_identifier_attributes_attribute_options(
        self,
        *,
        target,
        identifier,
        attribute,
    ):
        assert target == "objects"
        del identifier
        return _resp(
            [SimpleNamespace(title=t) for t in self.options.get(attribute, [])],
        )

    def get_v2_target_identifier_attributes_attribute_statuses(
        self,
        *,
        target,
        identifier,
        attribute,
    ):
        assert target == "objects"
        del identifier
        return _resp(
            [SimpleNamespace(title=t) for t in self.statuses.get(attribute, [])],
        )


class _FakeObjects:
    def __init__(self, objects):
        self.objects = objects
        self.calls = 0

    def get_v2_objects(self):
        self.calls += 1
        return _resp(self.objects)


class _FakeReadClient:
    def __init__(self, attributes, objects=None):
        self.attributes = attributes
        self.objects = objects if objects is not None else _FakeObjects([])

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def test_list_attributes_normalizes_fields_and_keeps_archived(monkeypatch) -> None:
    attrs = _FakeReadAttributes(
        attrs=[
            _live_attr("mention_url", "text", is_unique=True),
            _live_attr("keywords", "select", is_multiselect=True),
            _live_attr("record_id", "text", is_system_attribute=True),
            _live_attr("legacy", "text", is_archived=True),
            _live_attr("locked", "text", is_required=True),
        ],
    )
    objects = _FakeObjects([])
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs, objects),
    )

    result = list_attributes("social_mention")

    assert all(isinstance(a, AttributeInfo) for a in result)
    by_slug = {a.api_slug: a for a in result}
    assert by_slug["mention_url"].is_unique is True
    assert by_slug["keywords"].is_multiselect is True
    assert by_slug["record_id"].is_system is True
    assert by_slug["locked"].is_required is True
    # Archived attributes are returned (callers decide whether to filter).
    assert by_slug["legacy"].is_archived is True
    # No record-reference present -> no objects lookup paid for.
    assert objects.calls == 0


def test_list_attributes_resolves_record_reference_slugs(monkeypatch) -> None:
    attrs = _FakeReadAttributes(
        attrs=[
            _live_attr(
                "related_person",
                "record-reference",
                allowed_object_ids=["obj_people"],
            ),
        ],
    )
    objects = _FakeObjects([_live_object("obj_people", "people")])
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs, objects),
    )

    result = list_attributes("social_mention")

    assert result[0].allowed_objects == ("people",)
    assert objects.calls == 1


def test_list_attributes_falls_back_to_raw_id_when_slug_unresolved(monkeypatch) -> None:
    attrs = _FakeReadAttributes(
        attrs=[
            _live_attr(
                "related_person",
                "record-reference",
                allowed_object_ids=["obj_orphan"],
            ),
        ],
    )
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs, _FakeObjects([])),
    )

    result = list_attributes("social_mention")

    assert result[0].allowed_objects == ("obj_orphan",)


def test_list_attributes_returns_empty_on_404(monkeypatch) -> None:
    attrs = _FakeReadAttributes(attrs=[], attrs_error=_make_sdk_error(404, "no object"))
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs),
    )

    assert list_attributes("social_mention") == []


def test_list_attributes_reraises_non_404(monkeypatch) -> None:
    attrs = _FakeReadAttributes(attrs=[], attrs_error=_make_sdk_error(500, "boom"))
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs),
    )

    with pytest.raises(SDKError):
        list_attributes("social_mention")


def test_list_select_options_returns_titles(monkeypatch) -> None:
    attrs = _FakeReadAttributes(attrs=[], options={"relevance_score": ["high", "low"]})
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs),
    )

    assert list_select_options(
        target_object="social_mention",
        attribute_slug="relevance_score",
    ) == ["high", "low"]


def test_list_status_options_returns_titles(monkeypatch) -> None:
    attrs = _FakeReadAttributes(attrs=[], statuses={"triage_status": ["New", "Done"]})
    monkeypatch.setattr(
        "libs.attio.attributes.get_client",
        lambda: _FakeReadClient(attrs),
    )

    assert list_status_options(
        target_object="social_mention",
        attribute_slug="triage_status",
    ) == ["New", "Done"]
