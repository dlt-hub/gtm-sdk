"""Persistence for booking-thread anchors (``thread_key`` -> Slack ``ts``).

Threading a booking's lifecycle events together means remembering the ``ts`` of
the message that opened the thread, across separate webhook requests (and
separate Modal containers). The store abstracts that lookup so the dispatcher
in ``src.slack.export`` stays testable without Modal.

- :class:`ThreadStore` — the structural contract (``get`` / ``set``).
- :class:`InMemoryThreadStore` — for tests and the local entrypoint.
- :func:`modal_dict_thread_store` — the production store backed by a
  ``modal.Dict``, which is durable and shared across the app's containers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ThreadStore(Protocol):
    """Maps a booking ``thread_key`` to the Slack ``ts`` that opened its thread."""

    def get(self, thread_key: str) -> str | None: ...

    def set(self, thread_key: str, ts: str) -> None: ...


class InMemoryThreadStore:
    """Process-local store. Loses state across containers — tests/local only."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, thread_key: str) -> str | None:
        return self._store.get(thread_key)

    def set(self, thread_key: str, ts: str) -> None:
        self._store[thread_key] = ts


class _ModalDictThreadStore:
    """Durable store backed by a ``modal.Dict`` (shared across containers)."""

    def __init__(self, backing: object) -> None:
        # ``backing`` is a modal.Dict; typed as object to keep modal off this
        # module's import path for non-Modal callers.
        self._d = backing

    def get(self, thread_key: str) -> str | None:
        return self._d.get(thread_key)  # type: ignore[attr-defined]

    def set(self, thread_key: str, ts: str) -> None:
        self._d[thread_key] = ts  # type: ignore[index]


def modal_dict_thread_store(name: str) -> _ModalDictThreadStore:
    """Build the production store from a named, lazily-created ``modal.Dict``.

    One Dict per Slack app keeps booking keys from colliding across sources.

    Growth is unbounded by design: one entry per booking ``thread_key``, never
    evicted (``modal.Dict`` entries persist indefinitely). This is accepted for
    a low-volume booking notifier — one key per cal.com booking is negligible.
    If a high-volume source is ever wired to Slack, add a TTL/prune here.
    """
    import modal

    backing = modal.Dict.from_name(name, create_if_missing=True)
    return _ModalDictThreadStore(backing)
