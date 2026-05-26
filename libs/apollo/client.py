"""Apollo SDK client builder.

Key resolution order for ``get_client()``:

1. Explicit ``api_key`` argument (used by tests and one-off CLI scripts).
2. The contextvar set by :func:`api_key_scope` — webhook and ``src/app.py``
   call sites open this scope after fetching the key from Infisical.
3. ``os.environ["APOLLO_API_KEY"]`` — back-compat fallback for any path that
   still relies on the legacy named Modal Secret.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_api_key_var: ContextVar[str | None] = ContextVar(
    "apollo_api_key",
    default=None,
)


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Apollo key for this async/sync context.

    Mirrors :func:`libs.attio.client.api_key_scope`. The scope is reset on
    exit so concurrent Modal inputs in the same container do not see each
    other's keys.
    """
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


def get_client(api_key: str | None = None) -> Any:
    # Import here to avoid namespace collision with src/apollo
    import apollo as apollo_sdk

    token = (
        api_key or _api_key_var.get() or os.environ.get("APOLLO_API_KEY", "")
    ).strip()
    if not token:
        raise ValueError(
            "Apollo API key not resolved. Provide one of: "
            "(1) explicit api_key= argument, "
            "(2) call inside libs.apollo.client.api_key_scope(...), "
            "(3) set APOLLO_API_KEY in the process environment.",
        )
    apollo_client_class = getattr(apollo_sdk, "ApolloSDK")
    return apollo_client_class(api_key=token)
