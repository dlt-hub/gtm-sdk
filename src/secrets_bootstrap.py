"""Shared Infisical bootstrap helpers for Modal apps.

The webhook flow (``webhooks/export_to_attio.py``, shipped via ai-2aw) replaced
the named Modal Secret bindings with an inline ``modal.Secret.from_dict({...})``
bootstrap that ships only the Infisical creds. At runtime, the function body
opens ``infisical.fetch_all`` to resolve the real API keys, then binds each
into a per-lib ``api_key_scope`` contextvar so ``libs.<x>.get_client()`` finds
the right token.

``src/app.py`` and its 30+ ``@app.function`` sites need the exact same shape.
This module is the single source of truth for that pattern — both the
webhook handler and ``src/`` orchestrators import from here.

Adding a new lib to the pattern:

1. Define ``_api_key_var`` + ``api_key_scope`` in ``libs/<x>/client.py``
   (mirror ``libs/attio/client.py``).
2. Add a ``"<X>_API_KEY": <x>_client.api_key_scope`` entry to ``KEY_SCOPES``.
3. Decorate the Modal function with ``@with_secrets("<X>_API_KEY")``.

Keys declared by a caller but missing from ``KEY_SCOPES`` are silently
skipped — they are resolved into the ``infisical.fetch_all`` dict but not
bound into any lib scope. Use the explicit ``api_key=`` argument path for
those callers.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import TypeVar

import modal

from libs import infisical
from libs.apollo import client as apollo_client
from libs.attio import client as attio_client
from libs.caldotcom import client as caldotcom_client
from libs.parallel import client as parallel_client

KEY_SCOPES: dict[str, Callable[[str], AbstractContextManager[None]]] = {
    "APOLLO_API_KEY": apollo_client.api_key_scope,
    "ATTIO_API_KEY": attio_client.api_key_scope,
    "CALCOM_API_KEY": caldotcom_client.api_key_scope,
    "PARALLEL_API_KEY": parallel_client.api_key_scope,
}


def bootstrap_secret() -> modal.Secret:
    """Build an inline Modal Secret carrying Infisical bootstrap creds.

    Values come from the deploy-time shell env (populated by sourcing
    ``.env.local`` and running under ``infisical run --env=<env>``). Values
    are transmitted to Modal as a server-side secret object — they do NOT
    appear in image layers or build logs (validated 2026-05-18 during the
    ``ai-2aw`` ``from_dict`` probe; see
    ``modal-never-use-image-env-for-secrets-values`` bd memory).

    Missing creds are NOT raised here. This function runs at module-import
    time (it's the value passed to ``@app.function(secrets=[...])``), so
    raising would break tests that load this module without a real Infisical
    environment. ``scripts/redeploy_webhook.py`` preflights the bootstrap env
    before deploy; at runtime, ``infisical.fetch_all`` will raise
    ``InfisicalAuthError`` if the token is empty.
    """
    payload: dict[str, str | None] = {
        "INFISICAL_TOKEN": os.environ.get("INFISICAL_TOKEN", ""),
        "INFISICAL_PROJECT_ID": os.environ.get("INFISICAL_PROJECT_ID", ""),
    }
    for opt in ("INFISICAL_HOST", "INFISICAL_ENV"):
        v = os.environ.get(opt, "").strip()
        if v:
            payload[opt] = v
    return modal.Secret.from_dict(payload)


@contextmanager
def _activate_key_scopes(resolved: dict[str, str]) -> Generator[None, None, None]:
    with ExitStack() as stack:
        for name, value in resolved.items():
            scope_fn = KEY_SCOPES.get(name)
            if scope_fn is None:
                continue
            stack.enter_context(scope_fn(value))
        yield


@contextmanager
def hydrate(*keys: str) -> Generator[dict[str, str], None, None]:
    """Fetch ``keys`` from Infisical and bind them into per-lib scopes.

    Usage:

    .. code-block:: python

        with hydrate("ATTIO_API_KEY", "PARALLEL_API_KEY") as resolved:
            ...  # libs.attio.get_client() and libs.parallel._get_client()
                  # will both resolve to the freshly-fetched keys

    Yields the ``{name: value}`` resolution map for callers that need
    direct access (rare — usually you want the contextvar path).
    """
    required = list(keys)
    with (
        infisical.fetch_all(required) as resolved,
        _activate_key_scopes(resolved),
    ):
        yield resolved


F = TypeVar("F", bound=Callable[..., object])


def with_secrets(*keys: str) -> Callable[[F], F]:
    """Decorator: wrap a function body in :func:`hydrate` for ``keys``.

    Stacks inside ``@app.function(secrets=[bootstrap_secret()])`` so the
    Infisical bootstrap creds are present when the wrapper runs. For HTTP
    endpoints, ``@with_secrets`` goes innermost (below
    ``@modal.fastapi_endpoint``) so FastAPI introspects the decorated
    function's preserved signature (via ``functools.wraps``).

    Honors the ``api_keys=`` kwarg convention used by the existing Modal
    function surface: callers (CLI, tests) can pass
    ``api_keys={"attio_api_key": "..."}`` via ``.spawn(...)`` /
    ``.local(...)`` to override the Infisical-fetched values. This wrapper
    seeds ``inject_api_keys`` before opening ``hydrate``, so
    ``infisical.fetch_all`` (which checks env first) returns the override
    immediately instead of round-tripping to Infisical. Without this, every
    test that passes ``api_keys=`` would attempt a real Infisical fetch and
    fail without valid creds in the env.
    """

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> object:
            # Local import: src/api_keys.py is leaf-level (no exotic deps);
            # keeping it inside the wrapper avoids any reverse-import surprise
            # from CLI/test modules that import from src.secrets_bootstrap.
            from src.api_keys import inject_api_keys

            raw = kwargs.get("api_keys") or {}
            api_keys_override: dict[str, str] = raw if isinstance(raw, dict) else {}
            with inject_api_keys(api_keys_override), hydrate(*keys):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return deco
