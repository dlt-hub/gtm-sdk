"""Read Fireflies transcripts out of MotherDuck and assemble parent+children rows.

Orchestration (``src``): combines the ``libs.motherduck`` reader with the
dlt-normalised Fireflies layout. The recordings were loaded by dlt, so the
transcript lives in a parent table (``transcript_details``) with attendees in a
child table joined on ``_dlt_id`` ⇄ ``_dlt_parent_id``. We fetch both with two
queries and join in memory (122 rows — no need to stream a SQL join), yielding
one assembled ``{parent columns, "attendees": [...]}`` dict per transcript ready
for :func:`libs.fireflies.from_motherduck_row`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from libs.motherduck import query

if TYPE_CHECKING:
    from collections.abc import Iterator

    import duckdb

# Live MotherDuck location captured during the Step-0 probe. The database name
# contains a hyphen, so it must be double-quoted in SQL.
DATABASE = "fireflies-backfill"
SCHEMA = "fireflies"

_PARENT_COLUMNS = (
    "id",
    "title",
    "date",
    "duration",
    "transcript_url",
    "host_email",
    "organizer_email",
    "summary__overview",
    "summary__action_items",
    "summary__bullet_gist",
    "summary__short_summary",
    "_dlt_id",
)


def _fqn(table: str) -> str:
    return f'"{DATABASE}"."{SCHEMA}"."{table}"'


def iter_assembled_rows(con: duckdb.DuckDBPyConnection) -> Iterator[dict[str, Any]]:
    """Yield one assembled transcript row (parent columns + ``attendees``)."""
    # Group attendees by parent id up front (one query, joined in memory).
    attendees_by_parent: dict[str, list[dict[str, Any]]] = {}
    # nosec B608 -- the only interpolated value is a module-constant table name.
    for row in query(
        con,
        "SELECT _dlt_parent_id, email, display_name "
        f"FROM {_fqn('transcript_details__meeting_attendees')}",  # nosec B608
    ):
        attendees_by_parent.setdefault(row["_dlt_parent_id"], []).append(
            {"email": row.get("email"), "display_name": row.get("display_name")},
        )

    cols = ", ".join(f'"{c}"' for c in _PARENT_COLUMNS)
    # Stable order so dry-run reports and resumed runs line up. date is epoch ms.
    for parent in query(
        con,
        f"SELECT {cols} FROM {_fqn('transcript_details')} ORDER BY date",  # nosec B608
    ):
        parent["attendees"] = attendees_by_parent.get(parent["_dlt_id"], [])
        yield parent
