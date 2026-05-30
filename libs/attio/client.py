"""Attio SDK client builder.

Key resolution order for ``get_client()``:

1. Explicit ``api_key`` argument (used by tests and one-off CLI scripts).
2. The contextvar set by :func:`api_key_scope` — webhook endpoints open
   this scope after fetching the key from Infisical.
3. ``os.environ["ATTIO_API_KEY"]`` — the long-running ``src/app.py`` Modal
   app still relies on this path via the named ``attio`` Modal Secret.
   Webhook deploys do NOT bind that Modal Secret; they go through
   :func:`api_key_scope` instead.

If none resolve, raise :class:`AttioAuthError` with a message naming all
three resolution paths so the operator knows which to fix.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from libs.attio.errors import AttioAuthError
from libs.attio.sdk_boundary import get_attio_sdk_client_class

ATTIO_OP_TIMEOUT_SECONDS: float = float(
    os.environ.get("ATTIO_OP_TIMEOUT_SECONDS", "10"),
)

_api_key_var: ContextVar[str | None] = ContextVar(
    "attio_api_key",
    default=None,
)


@contextmanager
def api_key_scope(api_key: str) -> Generator[None, None, None]:
    """Bind ``api_key`` as the active Attio key for this async/sync context.

    Used by the webhook flow: the endpoint resolves the key via Infisical
    once per request, then opens this scope before calling Attio operations.
    The scope is reset on exit so concurrent requests in the same Modal
    container (``@modal.concurrent``) do not see each other's keys.
    """
    token = _api_key_var.set(api_key)
    try:
        yield
    finally:
        _api_key_var.reset(token)


def resolve_api_key(api_key: str | None = None) -> str:
    """Resolve the active Attio token via the documented precedence.

    Single source of truth for the resolution order described in the module
    docstring, shared by :func:`get_client` and the scope preflight in
    ``libs.attio.preflight`` (the latter fingerprints the resolved token to
    cache its ``/v2/self`` check per process). Raises :class:`AttioAuthError`
    when no token resolves.
    """
    token = (
        api_key or _api_key_var.get() or os.environ.get("ATTIO_API_KEY", "")
    ).strip()
    if not token:
        raise AttioAuthError(
            "Attio API key not resolved. Provide one of: "
            "(1) explicit api_key= argument, "
            "(2) call inside libs.attio.client.api_key_scope(...), "
            "(3) set ATTIO_API_KEY in the process environment.",
        )
    return token


def get_client(api_key: str | None = None):
    token = resolve_api_key(api_key)
    Attio = get_attio_sdk_client_class()
    return Attio(
        oauth2=token,
        timeout_ms=int(ATTIO_OP_TIMEOUT_SECONDS * 1000),
    )
