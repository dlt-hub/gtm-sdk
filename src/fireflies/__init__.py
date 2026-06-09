"""Fireflies → Attio backfill orchestration."""

from src.fireflies.source import DATABASE, SCHEMA, iter_assembled_rows
from src.fireflies.to_attio import (
    DEFAULT_ORG_DOMAINS,
    SUMMARY_NOTE_TITLE,
    to_attio_operations,
)

__all__ = [
    "DATABASE",
    "DEFAULT_ORG_DOMAINS",
    "SCHEMA",
    "SUMMARY_NOTE_TITLE",
    "iter_assembled_rows",
    "to_attio_operations",
]
