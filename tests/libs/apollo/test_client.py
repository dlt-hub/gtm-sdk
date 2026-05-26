from __future__ import annotations

import sys
import types

import pytest


class _CapturingFakeApollo:
    """Light stand-in that records the api_key get_client passed."""

    last_api_key: str | None = None

    def __init__(self, *, api_key: str, **_kwargs: object) -> None:
        type(self).last_api_key = api_key


@pytest.fixture
def _fake_sdk(  # pyright: ignore[reportUnusedFunction]  # consumed by tests as a fixture
    monkeypatch: pytest.MonkeyPatch,
) -> type[_CapturingFakeApollo]:
    _CapturingFakeApollo.last_api_key = None
    fake_module = types.ModuleType("apollo")
    fake_module.ApolloSDK = _CapturingFakeApollo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apollo", fake_module)
    return _CapturingFakeApollo


def test_get_client_raises_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeApollo],
) -> None:
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    from libs.apollo.client import get_client

    with pytest.raises(ValueError):
        get_client()


def test_get_client_uses_env_var(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeApollo],
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "from-env")
    from libs.apollo.client import get_client

    get_client()
    assert _fake_sdk.last_api_key == "from-env"


def test_get_client_uses_explicit_api_key_over_everything(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeApollo],
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "from-env")
    from libs.apollo.client import api_key_scope, get_client

    with api_key_scope("from-scope"):
        get_client(api_key="from-arg")
    assert _fake_sdk.last_api_key == "from-arg"


def test_get_client_uses_scope_over_env(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeApollo],
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "from-env")
    from libs.apollo.client import api_key_scope, get_client

    with api_key_scope("from-scope"):
        get_client()
    assert _fake_sdk.last_api_key == "from-scope"


def test_api_key_scope_resets_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeApollo],
) -> None:
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    from libs.apollo.client import api_key_scope, get_client

    with api_key_scope("inside"):
        get_client()
        assert _fake_sdk.last_api_key == "inside"
    with pytest.raises(ValueError):
        get_client()


def test_api_key_scope_nests(
    monkeypatch: pytest.MonkeyPatch,
    _fake_sdk: type[_CapturingFakeApollo],
) -> None:
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    from libs.apollo.client import api_key_scope, get_client

    with api_key_scope("outer"):
        get_client()
        assert _fake_sdk.last_api_key == "outer"
        with api_key_scope("inner"):
            get_client()
            assert _fake_sdk.last_api_key == "inner"
        get_client()
        assert _fake_sdk.last_api_key == "outer"
