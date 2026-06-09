"""MotherDuck read adapter — connect and query, dataset-agnostic."""

from libs.motherduck.client import connect, query

__all__ = ["connect", "query"]
