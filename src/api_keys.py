from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

_KEY_ENV_MAP: dict[str, str] = {
    "apollo_api_key": "APOLLO_API_KEY",
    "attio_api_key": "ATTIO_API_KEY",
    "calcom_api_key": "CALCOM_API_KEY",
    "parallel_api_key": "PARALLEL_API_KEY",
    "resend_api_key": "RESEND_API_KEY",
    "granola_api_key": "GRANOLA_API_KEY",
}


@contextmanager
def inject_api_keys(api_keys: dict[str, str]) -> Generator[None, None, None]:
    """Override env vars from api_keys dict, restore originals on exit."""
    unknown_keys = sorted(set(api_keys) - set(_KEY_ENV_MAP))
    if unknown_keys:
        keys = ", ".join(unknown_keys)
        raise ValueError(f"Unsupported api_keys provided: {keys}")

    saved: dict[str, str | None] = {}
    for key_name, value in api_keys.items():
        env_var = _KEY_ENV_MAP[key_name]
        saved[env_var] = os.environ.get(env_var)
        os.environ[env_var] = value
    try:
        yield
    finally:
        for env_var, original in saved.items():
            if original is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = original
