# trunk-ignore-all(pyright/reportUnusedFunction): autouse pytest fixtures are invoked by name
"""Scope preflight for the Attio writer path (ai-ica).

Covers the three outcomes of ``assert_attio_token_scopes`` — pass, hard-fail on a
missing required scope, soft-warn on a missing recommended scope — plus the
inactive-token path and the per-token result cache.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from libs.attio import preflight
from libs.attio.errors import AttioScopeError

_FULL_SCOPE = (
    "record_permission:read-write object_configuration:read-write meeting:read-write"
)
_RESTRICTED_SCOPE = "record_permission:read-write object_configuration:read"
_NO_RECORD_SCOPE = "object_configuration:read-write meeting:read-write"


@contextmanager
def _mock_self(
    *,
    active: bool = True,
    scope: str = _FULL_SCOPE,
    member_id: str = "wm_dlthub_elvis",
):
    """Patch ``preflight.get_client`` so ``get_v2_self`` returns a stub identity."""
    client = MagicMock()
    client.meta.get_v2_self.return_value = SimpleNamespace(
        active=active,
        scope=scope,
        workspace_slug="dlthub",
        authorized_by_workspace_member_id=member_id,
    )
    with patch("libs.attio.preflight.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = client
        yield mock_get_client


@pytest.fixture(autouse=True)
def _clear_cache():
    preflight.reset_scope_cache()
    yield
    preflight.reset_scope_cache()


def test_full_scope_token_passes_without_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING), _mock_self(scope=_FULL_SCOPE):
        preflight.assert_attio_token_scopes(api_key="tok-full")
    assert "missing_recommended_scope" not in caplog.text


def test_missing_required_scope_raises() -> None:
    with _mock_self(scope=_NO_RECORD_SCOPE), pytest.raises(AttioScopeError) as exc:
        preflight.assert_attio_token_scopes(api_key="tok-norecord")
    assert "record_permission:read-write" in exc.value.missing


def test_inactive_token_raises() -> None:
    with _mock_self(active=False, scope=""), pytest.raises(AttioScopeError) as exc:
        preflight.assert_attio_token_scopes(api_key="tok-inactive")
    assert "active=false" in str(exc.value)


def test_missing_recommended_scope_warns_but_passes(caplog) -> None:
    # This is exactly the prod token state from ai-ica: record write present,
    # object_configuration only read. Must NOT raise (schema can be
    # pre-bootstrapped) but must warn.
    with caplog.at_level(logging.WARNING), _mock_self(scope=_RESTRICTED_SCOPE):
        preflight.assert_attio_token_scopes(api_key="tok-restricted")
    assert "attio_token_missing_recommended_scope" in caplog.text


def test_result_is_cached_per_token() -> None:
    with _mock_self(scope=_FULL_SCOPE) as mock_get_client:
        preflight.assert_attio_token_scopes(api_key="tok-cache")
        preflight.assert_attio_token_scopes(api_key="tok-cache")
        assert mock_get_client.call_count == 1


def test_force_bypasses_cache() -> None:
    with _mock_self(scope=_FULL_SCOPE) as mock_get_client:
        preflight.assert_attio_token_scopes(api_key="tok-force")
        preflight.assert_attio_token_scopes(api_key="tok-force", force=True)
        assert mock_get_client.call_count == 2


def test_cache_does_not_suppress_stricter_check_for_same_token() -> None:
    # A lenient pass (record_permission only) must NOT let a later stricter
    # check (object_configuration:read-write) skip validation for the same token.
    record_only = frozenset({"record_permission:read-write"})
    objcfg = frozenset({"object_configuration:read-write"})
    with _mock_self(scope="record_permission:read-write"):
        # Lenient check passes and caches its profile.
        preflight.assert_attio_token_scopes(
            api_key="tok-shared",
            required=record_only,
            recommended=frozenset(),
        )
        # Stricter check for the SAME token is not suppressed by the cache,
        # so it re-evaluates and fails on the genuinely-missing scope.
        with pytest.raises(AttioScopeError):
            preflight.assert_attio_token_scopes(
                api_key="tok-shared",
                required=objcfg,
                recommended=frozenset(),
            )


def test_resolve_owner_member_id_returns_authorizing_member() -> None:
    with _mock_self(member_id="wm_prod_elvis"):
        assert preflight.resolve_owner_member_id(api_key="tok-owner") == "wm_prod_elvis"


def test_resolve_owner_member_id_caches_per_token() -> None:
    with _mock_self(member_id="wm_prod_elvis") as mock_get_client:
        preflight.resolve_owner_member_id(api_key="tok-owner-cache")
        preflight.resolve_owner_member_id(api_key="tok-owner-cache")
        assert mock_get_client.call_count == 1


def test_resolve_owner_member_id_returns_none_when_absent() -> None:
    with _mock_self(member_id=""):
        assert preflight.resolve_owner_member_id(api_key="tok-no-member") is None


def test_resolve_owner_member_id_returns_none_on_lookup_error() -> None:
    # A transient /v2/self failure must NOT propagate — owner is best-effort,
    # and raising here would abort the record write that called us (ai-ica).
    client = MagicMock()
    client.meta.get_v2_self.side_effect = RuntimeError("network blip")
    with patch("libs.attio.preflight.get_client") as mock_get_client:
        mock_get_client.return_value.__enter__.return_value = client
        assert preflight.resolve_owner_member_id(api_key="tok-flaky") is None
