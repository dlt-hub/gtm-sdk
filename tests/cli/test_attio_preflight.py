from __future__ import annotations

from contextlib import contextmanager

import modal
import pytest

from cli.attio.preflight import run_people_preflight
from libs.attio.errors import ConfigurationError, ConnectivityError
from libs.infisical.errors import InfisicalAuthError, InfisicalFetchError


@contextmanager
def _ok_fetch(name: str):
    yield "ak_test_value"


def test_missing_infisical_key_raises_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "id_123")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret_123")

    @contextmanager
    def _explode(name: str):
        raise InfisicalFetchError(
            f"Failed to fetch {name} from Infisical: APIError: Secret with name "
            f"'{name}' not found (Status: 404)",
        )
        yield  # unreachable, but makes this a generator

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _explode)

    with pytest.raises(ConfigurationError) as exc_info:
        run_people_preflight(
            connectivity_probe=True,
            function_name="attio_search_people",
        )

    assert "ATTIO_API_KEY" in str(exc_info.value)


def test_unauthorized_infisical_failure_raises_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "id_123")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret_123")

    @contextmanager
    def _unauthorized(name: str):
        raise InfisicalFetchError(
            f"Failed to fetch {name} from Infisical: APIError: Unauthorized (Status: 401)",
        )
        yield  # unreachable

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _unauthorized)

    with pytest.raises(ConfigurationError):
        run_people_preflight(
            connectivity_probe=True,
            function_name="attio_search_people",
        )


def test_transient_infisical_failure_raises_connectivity_error(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "id_123")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret_123")

    @contextmanager
    def _flake(name: str):
        raise InfisicalFetchError(
            f"Failed to fetch {name} from Infisical: ConnectionError: read timeout",
        )
        yield  # unreachable

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _flake)

    with pytest.raises(ConnectivityError):
        run_people_preflight(
            connectivity_probe=True,
            function_name="attio_search_people",
        )


def test_infisical_auth_failure_raises_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "id_123")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret_123")

    @contextmanager
    def _auth_fail(name: str):
        raise InfisicalAuthError("INFISICAL_TOKEN missing")
        yield  # unreachable

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _auth_fail)

    with pytest.raises(ConfigurationError) as exc_info:
        run_people_preflight(
            connectivity_probe=True,
            function_name="attio_search_people",
        )

    assert "Infisical" in str(exc_info.value)


def test_happy_path(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "id_123")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret_123")

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _ok_fetch)

    def _from_name(_app: str, _fn: str):
        return object()

    monkeypatch.setattr(modal.Function, "from_name", _from_name)

    env_payload, warnings, parity_meta = run_people_preflight(
        connectivity_probe=True,
        function_name="attio_search_people",
    )

    assert env_payload["MODAL_TOKEN_ID"] == "id_123"
    assert isinstance(warnings, list)
    assert isinstance(parity_meta, dict)


def test_token_check_still_wins(monkeypatch) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)

    @contextmanager
    def _should_not_run(name: str):
        pytest.fail("should not call infisical.fetch if MODAL_TOKEN_ID is missing")
        yield  # unreachable

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _should_not_run)

    with pytest.raises(ConfigurationError) as exc_info:
        run_people_preflight(
            connectivity_probe=True,
            function_name="attio_search_people",
        )

    assert "MODAL_TOKEN" in str(exc_info.value)


def test_connectivity_probe_false_skips_infisical_check(monkeypatch) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "id_123")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret_123")

    fetched: list[str] = []

    @contextmanager
    def _record(name: str):
        fetched.append(name)
        yield "v"

    monkeypatch.setattr("cli.attio.preflight.infisical.fetch", _record)

    env_payload, _, _ = run_people_preflight(
        connectivity_probe=False,
        function_name="attio_search_people",
    )

    assert fetched == []
    assert env_payload["MODAL_TOKEN_ID"] == "id_123"
