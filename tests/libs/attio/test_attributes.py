from __future__ import annotations

from types import SimpleNamespace

import pytest
from attio.errors import SDKDefaultError

from libs.attio.attributes import create_companies_attribute, ensure_select_options
from libs.attio.models import AttributeCreateResult


def _resp(data):
    return SimpleNamespace(data=data)


def _attr(slug: str):
    return SimpleNamespace(api_slug=slug)


class _FakeAttributes:
    def __init__(self, attrs=None, options=None, option_post_errors=None):
        self.attrs = attrs or []
        self.created_attrs: list[dict[str, object]] = []
        self.options = options or []
        self.created_options: list[dict[str, object]] = []
        # Mapping of option title -> Exception to raise when POSTed. Each entry
        # is consumed at most once so subsequent calls succeed (mirrors the
        # production race where a second attempt would also conflict, but lets
        # tests assert "later titles still POST" cleanly).
        self.option_post_errors: dict[str, Exception] = dict(
            option_post_errors or {},
        )

    def get_v2_target_identifier_attributes(self, *, target, identifier):
        assert target == "objects"
        assert identifier == "companies"
        return _resp(self.attrs)

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


def _make_sdk_error(status_code: int, message: str = "boom") -> SDKDefaultError:
    """Build a SDKDefaultError without spinning up a real httpx.Response.

    The real SDKDefaultError signature expects an httpx.Response, but its __init__
    only reads .status_code/.headers/.text — a SimpleNamespace is sufficient
    for the duck-typed access pattern in production (`raw_response.status_code`).
    """
    raw_response = SimpleNamespace(status_code=status_code, headers={}, text=message)
    return SDKDefaultError(message, raw_response, message)  # type: ignore[arg-type]


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
    fake = _FakeAttributes(attrs=[_attr("gtm_tool_type")])
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
    assert fake.created_attrs == []


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

    with pytest.raises(SDKDefaultError):
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
