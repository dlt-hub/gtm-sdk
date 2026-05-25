"""Unit tests for the integration-suite preflight helpers in conftest.py.

The pytest hook itself (``pytest_collection_modifyitems``) is exercised
indirectly any time ``pytest -m integration`` runs against a real Attio
workspace. These tests cover the pure-function surface: message formatting
and the credentials gate that decides whether to probe at all.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from tests.integration.conftest import (
    _format_missing_objects_message,  # pyright: ignore[reportPrivateUsage]
    _has_real_attio_credentials,  # pyright: ignore[reportPrivateUsage]
)


def test_format_missing_objects_message_includes_blocker_reference() -> None:
    msg = _format_missing_objects_message(
        missing={"social_mention"},
        blockers={"social_mention": "ai-o32"},
    )
    assert "social_mention" in msg
    assert "ai-o32" in msg
    assert "Bootstrap" in msg


def test_format_missing_objects_message_omits_blocker_when_unknown() -> None:
    msg = _format_missing_objects_message(
        missing={"some_other_object"},
        blockers={},
    )
    assert "some_other_object" in msg
    assert "blocked on" not in msg


def test_format_missing_objects_message_sorts_for_stable_output() -> None:
    msg = _format_missing_objects_message(
        missing={"zeta", "alpha"},
        blockers={},
    )
    assert msg.index("alpha") < msg.index("zeta")


def test_has_real_attio_credentials_false_when_unset() -> None:
    with patch.dict(os.environ, {"ATTIO_API_KEY": ""}, clear=False):
        assert _has_real_attio_credentials() is False


def test_has_real_attio_credentials_false_when_stub() -> None:
    with patch.dict(os.environ, {"ATTIO_API_KEY": "stub"}, clear=False):
        assert _has_real_attio_credentials() is False


def test_has_real_attio_credentials_true_when_full_length() -> None:
    with patch.dict(os.environ, {"ATTIO_API_KEY": "x" * 64}, clear=False):
        assert _has_real_attio_credentials() is True
