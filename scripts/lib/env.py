"""Shared helpers for repo-local command-line scripts.

Keep the execution contract in one place so operators and agents see the same
repo-approved invocation form.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPT_DIR.parent


def infisical_run_example(
    script_relpath: str,
    *,
    env_placeholder: str = "<dev|prod>",
    extra_args: str = "",
) -> str:
    """Return the canonical `infisical run` form for a repo script."""

    suffix = f" {extra_args}" if extra_args else ""
    return (
        'infisical run --projectId "$INFISICAL_PROJECT_ID" '
        '--token "$INFISICAL_TOKEN" '
        f"--env={env_placeholder} -- {script_relpath}{suffix}"
    )


def add_repo_root_to_sys_path() -> None:
    """Insert the repo root into `sys.path` if a script needs local imports."""

    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def clean_env(value: str | None) -> str | None:
    """Strip whitespace from an env value; treat blank-after-strip as ``None``.

    Trailing newlines on secrets (e.g. from a `cat`-ed file or copy-paste)
    silently break auth otherwise — Attio rejects "Bearer key\\n" with a 401
    that looks identical to a bad key. Shared by repo scripts that bootstrap
    secrets from the environment or `.env.local`.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse the subset of `.env` syntax repo scripts care about.

    Supports blank lines, `# comments`, a leading `export` keyword, and
    single-/double-quoted values (with inline `# comment` after an *unquoted*
    value). Does NOT support multiline values or shell expansion — `.env.local`
    here only carries Infisical creds, which are single-line opaque tokens.
    """
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if value and value[0] in ("'", '"'):
            # Quoted: take everything up to the matching closing quote and
            # discard the rest (e.g. a trailing ` # comment`). A `#` inside the
            # quotes is preserved. An unterminated quote keeps the remainder.
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            comment_idx = value.find(" #")
            if comment_idx >= 0:
                value = value[:comment_idx].rstrip()
        parsed[key] = value
    return parsed


def read_infisical_credentials() -> tuple[str, str] | None:
    """Resolve INFISICAL_PROJECT_ID/TOKEN from env, then ``REPO_ROOT/.env.local``.

    We deliberately avoid asking the operator to `set -a; source .env.local`
    (per repo memory) — instead we parse the file ourselves and feed the values
    straight to `infisical run` as CLI flags. Returns ``None`` when neither the
    environment nor `.env.local` supplies both values.

    The two credentials are treated as an ATOMIC PAIR per source: the
    environment is used only when it supplies BOTH; otherwise both values come
    from `.env.local`. Mixing one value from each source could silently target
    the wrong workspace or fail auth in a non-obvious way.
    """
    env_project_id = clean_env(os.environ.get("INFISICAL_PROJECT_ID"))
    env_token = clean_env(os.environ.get("INFISICAL_TOKEN"))
    if env_project_id and env_token:
        return env_project_id, env_token

    env_file = REPO_ROOT / ".env.local"
    if not env_file.is_file():
        return None

    parsed = parse_dotenv(env_file.read_text())
    file_project_id = clean_env(parsed.get("INFISICAL_PROJECT_ID"))
    file_token = clean_env(parsed.get("INFISICAL_TOKEN"))
    if file_project_id and file_token:
        return file_project_id, file_token
    return None
