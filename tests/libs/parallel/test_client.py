# pyright: reportPrivateUsage=none
# Tests intentionally exercise the module-private ``_get_client`` to verify
# the api_key resolution chain — same pattern as ``test_search_handles_none_excerpts``
# below, which has lived here since before ai-672.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from libs.parallel.models import (
    ExtractExcerptsInput,
    ExtractFullContentInput,
    FindAllCreateInput,
    FindAllLookupInput,
    MatchCondition,
    SearchInput,
)


def test_search_input_defaults():
    inp = SearchInput(objective="find acme")
    assert inp.mode == "one-shot"
    assert inp.max_results == 10


def test_findall_create_input_requires_fields():
    inp = FindAllCreateInput(
        objective="find",
        entity_type="company",
        match_conditions=[MatchCondition(name="rev", description="revenue > 1M")],
    )
    assert inp.match_limit == 10
    assert inp.generator == "base"


def test_extract_excerpts_input():
    inp = ExtractExcerptsInput(url="https://acme.com", objective="funding")
    assert inp.url == "https://acme.com"


def test_extract_full_content_input():
    inp = ExtractFullContentInput(url="https://acme.com")
    assert inp.url == "https://acme.com"


def test_findall_lookup_input():
    inp = FindAllLookupInput(findall_id="fa_123")
    assert inp.findall_id == "fa_123"


def test_search_handles_none_excerpts(monkeypatch) -> None:
    from libs.parallel.client import search

    response = SimpleNamespace(
        search_id="s_1",
        results=[
            SimpleNamespace(
                url="https://example.com",
                title=None,
                publish_date=None,
                excerpts=None,
            ),
        ],
    )

    def _search(**_: object) -> SimpleNamespace:
        return response

    fake_client = SimpleNamespace(beta=SimpleNamespace(search=_search))

    def _get_client() -> SimpleNamespace:
        return fake_client

    monkeypatch.setattr("libs.parallel.client._get_client", _get_client)

    parsed = search(SearchInput(objective="acme"))
    assert parsed.results[0].excerpts == []


# --- ai-672: api_key arg + api_key_scope context ---


class _CapturingFakeParallel:
    """Light stand-in that records the api_key _get_client passed."""

    last_api_key: str | None = None

    def __init__(self, *, api_key: str, **_kwargs: object) -> None:
        type(self).last_api_key = api_key


@pytest.fixture
def _fake_sdk(  # pyright: ignore[reportUnusedFunction]  # consumed by tests as a fixture
    monkeypatch,
) -> type[_CapturingFakeParallel]:
    import sys
    import types

    _CapturingFakeParallel.last_api_key = None
    fake_module = types.ModuleType("parallel")
    fake_module.Parallel = _CapturingFakeParallel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "parallel", fake_module)
    return _CapturingFakeParallel


def test_get_client_raises_without_api_key(monkeypatch, _fake_sdk) -> None:
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    from libs.parallel.client import _get_client

    with pytest.raises(ValueError):
        _get_client()


def test_get_client_uses_env_var(monkeypatch, _fake_sdk) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "from-env")
    from libs.parallel.client import _get_client

    _get_client()
    assert _fake_sdk.last_api_key == "from-env"


def test_get_client_uses_explicit_api_key_over_everything(
    monkeypatch,
    _fake_sdk,
) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "from-env")
    from libs.parallel.client import _get_client, api_key_scope

    with api_key_scope("from-scope"):
        _get_client(api_key="from-arg")
    assert _fake_sdk.last_api_key == "from-arg"


def test_get_client_uses_scope_over_env(monkeypatch, _fake_sdk) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "from-env")
    from libs.parallel.client import _get_client, api_key_scope

    with api_key_scope("from-scope"):
        _get_client()
    assert _fake_sdk.last_api_key == "from-scope"


def test_api_key_scope_resets_on_exit(monkeypatch, _fake_sdk) -> None:
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    from libs.parallel.client import _get_client, api_key_scope

    with api_key_scope("inside"):
        _get_client()
        assert _fake_sdk.last_api_key == "inside"
    with pytest.raises(ValueError):
        _get_client()


def test_api_key_scope_nests(monkeypatch, _fake_sdk) -> None:
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    from libs.parallel.client import _get_client, api_key_scope

    with api_key_scope("outer"):
        _get_client()
        assert _fake_sdk.last_api_key == "outer"
        with api_key_scope("inner"):
            _get_client()
            assert _fake_sdk.last_api_key == "inner"
        _get_client()
        assert _fake_sdk.last_api_key == "outer"
