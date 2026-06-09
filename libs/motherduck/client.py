"""Thin MotherDuck (DuckDB cloud) read adapter.

Wraps exactly one external thing — a MotherDuck account reached over the duckdb
``md:`` connection scheme — and exposes a couple of read helpers. It knows
nothing about any particular dataset (Fireflies, GitHub, …); callers pass the
database name and SQL. Per the repo's code-placement rules this module must not
import from another ``libs/<x>`` adapter.

Auth: duckdb reads ``MOTHERDUCK_TOKEN`` from the environment when it attaches an
``md:`` database. We surface a clear, actionable error when it is absent rather
than letting duckdb raise a cryptic attach failure deep in a query.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterator

_TOKEN_ENV = "MOTHERDUCK_TOKEN"  # nosec B105 -- env var name, not a credential


def _require_token() -> None:
    if not os.environ.get(_TOKEN_ENV):
        raise RuntimeError(
            f"{_TOKEN_ENV} is not set. It is a personal MotherDuck token kept in "
            "the repo-root .env.local (intentionally not in Infisical). Export it "
            "before connecting, e.g. via the backfill script's self-load, or "
            f"`{_TOKEN_ENV}=... <cmd>`.",
        )


def connect(database: str | None = None) -> duckdb.DuckDBPyConnection:
    """Open a MotherDuck connection.

    ``database`` attaches a specific MotherDuck database (``md:<database>``);
    pass ``None`` to connect at the account level (``md:``) — useful for
    ``SHOW DATABASES`` / cross-database queries. Database names containing
    characters like ``-`` are still valid MotherDuck names; quote them in SQL.
    """
    _require_token()
    target = f"md:{database}" if database else "md:"
    return duckdb.connect(target)


def query(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | tuple[Any, ...] | None = None,
) -> Iterator[dict[str, Any]]:
    """Run ``sql`` and yield each row as a ``{column: value}`` dict.

    Streams via the cursor so a large result set is not materialised twice.
    """
    cur = con.execute(sql, list(params) if params else None)
    columns = [d[0] for d in cur.description]
    for row in cur.fetchall():
        yield dict(zip(columns, row, strict=True))
