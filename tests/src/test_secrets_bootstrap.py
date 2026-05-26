from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Any

import pytest


def test_key_scopes_covers_known_libs() -> None:
    from src.secrets_bootstrap import KEY_SCOPES

    expected = {"APOLLO_API_KEY", "ATTIO_API_KEY", "CALCOM_API_KEY", "PARALLEL_API_KEY"}
    assert expected.issubset(KEY_SCOPES.keys())


def test_bootstrap_secret_builds_modal_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFISICAL_TOKEN", "t")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "p")
    monkeypatch.setenv("INFISICAL_ENV", "dev")
    monkeypatch.delenv("INFISICAL_HOST", raising=False)

    import modal

    from src.secrets_bootstrap import bootstrap_secret

    sec = bootstrap_secret()
    assert isinstance(sec, modal.Secret)


def test_hydrate_activates_scopes_for_known_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.secrets_bootstrap as sb

    seen: list[tuple[str, str, str]] = []

    # Replace KEY_SCOPES with capturing fakes.
    monkeypatch.setattr(
        sb,
        "KEY_SCOPES",
        {
            "ATTIO_API_KEY": _make_capturing_scope("attio", seen),
            "PARALLEL_API_KEY": _make_capturing_scope("parallel", seen),
        },
    )

    @contextmanager
    def fake_fetch_all(names):
        yield {n: f"value-for-{n}" for n in names}

    monkeypatch.setattr(sb.infisical, "fetch_all", fake_fetch_all)

    with sb.hydrate("ATTIO_API_KEY", "PARALLEL_API_KEY") as resolved:
        assert resolved == {
            "ATTIO_API_KEY": "value-for-ATTIO_API_KEY",
            "PARALLEL_API_KEY": "value-for-PARALLEL_API_KEY",
        }

    assert ("attio", "value-for-ATTIO_API_KEY", "enter") in seen
    assert ("attio", "value-for-ATTIO_API_KEY", "exit") in seen
    assert ("parallel", "value-for-PARALLEL_API_KEY", "enter") in seen


def test_hydrate_silently_skips_unknown_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.secrets_bootstrap as sb

    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        sb,
        "KEY_SCOPES",
        {"ATTIO_API_KEY": _make_capturing_scope("attio", seen)},
    )

    @contextmanager
    def fake_fetch_all(names):
        yield {n: f"v-{n}" for n in names}

    monkeypatch.setattr(sb.infisical, "fetch_all", fake_fetch_all)

    # UNKNOWN_KEY is not in KEY_SCOPES — should not raise, should not appear in seen.
    with sb.hydrate("ATTIO_API_KEY", "UNKNOWN_KEY") as resolved:
        assert "UNKNOWN_KEY" in resolved
    assert all(label != "UNKNOWN_KEY" for (label, _v, _phase) in seen)


def test_with_secrets_preserves_signature_and_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.secrets_bootstrap as sb

    @contextmanager
    def fake_fetch_all(names):
        yield {n: "v" for n in names}

    monkeypatch.setattr(sb.infisical, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sb, "KEY_SCOPES", {})

    @sb.with_secrets("ATTIO_API_KEY")
    def my_fn(payload: dict[str, Any], multiplier: int = 2) -> dict[str, Any]:
        """My docstring."""
        return {"payload": payload, "multiplier": multiplier}

    assert my_fn.__name__ == "my_fn"
    assert my_fn.__doc__ == "My docstring."
    sig = inspect.signature(my_fn)
    assert list(sig.parameters.keys()) == ["payload", "multiplier"]

    result = my_fn({"a": 1}, multiplier=3)
    assert result == {"payload": {"a": 1}, "multiplier": 3}


def test_with_secrets_passes_resolved_keys_through_contextvars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: @with_secrets wires Infisical → contextvar → libs.<x>.get_client()."""
    import sys
    import types

    import src.secrets_bootstrap as sb
    from libs.apollo import client as apollo_client

    # Stub the Apollo SDK so we can observe what get_client passes through.
    class _FakeApolloSDK:
        last_api_key: str | None = None

        def __init__(self, *, api_key: str, **_kw: object) -> None:
            type(self).last_api_key = api_key

    fake_apollo = types.ModuleType("apollo")
    fake_apollo.ApolloSDK = _FakeApolloSDK  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apollo", fake_apollo)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)

    @contextmanager
    def fake_fetch_all(names):
        yield {n: f"resolved-{n}" for n in names}

    monkeypatch.setattr(sb.infisical, "fetch_all", fake_fetch_all)

    @sb.with_secrets("APOLLO_API_KEY")
    def inner() -> None:
        apollo_client.get_client()

    inner()
    assert _FakeApolloSDK.last_api_key == "resolved-APOLLO_API_KEY"


def test_with_secrets_honors_api_keys_kwarg_override_without_infisical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``api_keys=`` is supplied, Infisical must not be contacted —
    ``inject_api_keys`` seeds the env so ``infisical.fetch`` short-circuits.
    """

    import src.secrets_bootstrap as sb
    from libs.attio import client as attio_client

    class _FakeAttio:
        last_oauth2: str | None = None

        def __init__(self, *, oauth2: str, **_kw: object) -> None:
            type(self).last_oauth2 = oauth2

    monkeypatch.setattr(
        "libs.attio.client.get_attio_sdk_client_class",
        lambda: _FakeAttio,
    )
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)

    # Sentinel: if anything calls _fetch_from_infisical, the test fails.
    def _explode(_name: str) -> str:
        raise AssertionError(
            "Infisical fetch should not run when api_keys override is provided",
        )

    import libs.infisical.client as infisical_client

    monkeypatch.setattr(infisical_client, "_fetch_from_infisical", _explode)
    _ = sb  # keep import live for readers — sb.with_secrets is what's exercised

    @sb.with_secrets("ATTIO_API_KEY")
    def fn(payload: dict[str, Any], api_keys: dict[str, str] | None = None) -> None:
        attio_client.get_client()

    fn({}, api_keys={"attio_api_key": "ak_override"})
    assert _FakeAttio.last_oauth2 == "ak_override"


# --- helpers ---


def _make_capturing_scope(label: str, sink: list[tuple[str, str, str]]):
    @contextmanager
    def scope(value: str):
        sink.append((label, value, "enter"))
        try:
            yield
        finally:
            sink.append((label, value, "exit"))

    return scope
