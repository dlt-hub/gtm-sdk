from __future__ import annotations

import importlib
from types import ModuleType

import pytest


class _FakeAttioSDK:
    """Captures constructor kwargs so tests can assert what get_client passes."""

    last_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        _FakeAttioSDK.last_kwargs = kwargs


def _reload_client(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Re-import libs.attio.client so module-level ATTIO_OP_TIMEOUT_SECONDS picks up env."""
    import libs.attio.client as client_module

    monkeypatch.setattr(
        client_module,
        "get_attio_sdk_client_class",
        lambda: _FakeAttioSDK,
    )
    reloaded = importlib.reload(client_module)
    monkeypatch.setattr(reloaded, "get_attio_sdk_client_class", lambda: _FakeAttioSDK)
    return reloaded


def test_get_client_passes_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    monkeypatch.delenv("ATTIO_OP_TIMEOUT_SECONDS", raising=False)
    module = _reload_client(monkeypatch)

    module.get_client()

    assert _FakeAttioSDK.last_kwargs["oauth2"] == "test-token"
    assert _FakeAttioSDK.last_kwargs["timeout_ms"] == 10_000


def test_get_client_respects_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATTIO_API_KEY", "test-token")
    monkeypatch.setenv("ATTIO_OP_TIMEOUT_SECONDS", "2.5")
    module = _reload_client(monkeypatch)

    module.get_client()

    assert _FakeAttioSDK.last_kwargs["timeout_ms"] == 2500


def test_get_client_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    module = _reload_client(monkeypatch)

    from libs.attio.errors import AttioAuthError

    with pytest.raises(AttioAuthError):
        module.get_client()
