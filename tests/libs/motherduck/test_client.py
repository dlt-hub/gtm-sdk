"""MotherDuck adapter — token guard and dict-row query shape.

``query`` is exercised against a plain in-memory duckdb connection (no MotherDuck
account needed); only the ``md:`` attach path requires the token.
"""

from __future__ import annotations

import duckdb
import pytest

from libs.motherduck import connect, query


def test_connect_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # The token guard fires before any network attach, so this needs no account.
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MOTHERDUCK_TOKEN is not set"):
        connect("fireflies")


def test_query_yields_column_keyed_dicts() -> None:
    con = duckdb.connect()
    rows = list(query(con, "SELECT 1 AS a, 'x' AS b"))
    assert rows == [{"a": 1, "b": "x"}]


def test_query_passes_params() -> None:
    con = duckdb.connect()
    rows = list(query(con, "SELECT ? AS v", [42]))
    assert rows == [{"v": 42}]
