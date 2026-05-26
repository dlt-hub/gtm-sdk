#!/usr/bin/env -S uv run python
# trunk-ignore-all(bandit/B607): list-arg subprocess only; tool names resolved via PATH on purpose.
"""Substitute, deploy, and restore a webhook handler in one safe step.

Port of scripts/redeploy-webhook.sh. Host-side Python does discovery,
preflights, atomic locking, backup, placeholder substitution, restore, and
restore verification. The ``modal deploy`` invocation itself runs inside a
Dagger container (matching ``scripts/hookdeck-dump-connection-events.py``)
so the env that ships images to Modal is reproducible operator-to-operator.

Every footgun catalogued in AGENTS.md "Scripted deploy pitfalls" is encoded
here as an explicit preflight or cleanup step. Keep that section in sync
with this file. The CI smoke test at ``tests/scripts/test_deploy_webhook.py``
exercises the substitute/restore loop, the EXIT-equivalent restore on deploy
failure, and the Modal token isolation rule.

Usage:
    scripts/redeploy_webhook.py <handler> <source>
    scripts/redeploy_webhook.py <handler> --all

The ``DAGGER_DRY_RUN=1`` env var swaps the Dagger deploy for a direct
``infisical run -- uv run modal deploy`` invocation on the host. Used by the
test suite so CI does not need a Dagger engine running.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import dagger

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
    ).strip(),
)
WEBHOOKS_DIR = REPO_ROOT / "webhooks"
BACKUP_DIR = REPO_ROOT / "tmp" / "webhook-deploy-bak"
LOCK_DIR = REPO_ROOT / "tmp" / "webhook-deploy.lock"

PLACEHOLDER = "WebhookModelToReplace"
REQUIRED_MODAL_SECRETS: tuple[str, ...] = ("devx-gcp-202605260000",)
VALID_INFISICAL_ENVS: tuple[str, ...] = ("dev", "staging", "prod")

# Pinned uv image matching the repo's requires-python (>=3.13,<3.14). The
# Dagger container does `uv sync --frozen` against the mounted lock file, so
# the deploy-time interpreter version matches the host venv exactly.
DAGGER_BASE_IMAGE = "ghcr.io/astral-sh/uv:python3.13-bookworm-slim"

# Module-level state read by ``_cleanup`` (registered via ``atexit`` and via
# SIGINT/SIGTERM handlers). Mirrors the bash trap that captured globals by
# name — until ``_backup_freshly_written`` flips to True, the cleanup hook is
# a no-op so an early-failure path cannot restore stale content over a clean
# worktree.
_handler: str | None = None
_handler_file: Path | None = None
_lock_acquired = False
_backup_freshly_written = False


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_handlers() -> list[str]:
    """Return every ``webhooks/*.py`` basename that contains the placeholder.

    New handlers adopt the placeholder pattern by including it in their
    source; no edit to this script is required when one appears.
    """
    handlers = sorted(
        path.stem
        for path in WEBHOOKS_DIR.glob("*.py")
        if PLACEHOLDER in path.read_text()
    )
    if not handlers:
        _fail(
            f"No webhook handlers under {WEBHOOKS_DIR.relative_to(REPO_ROOT)} "
            f"contain {PLACEHOLDER}.",
        )
    return handlers


_SOURCE_RE = re.compile(
    r"^\s*Webhook as ([A-Za-z_][A-Za-z0-9_]*),?\s*$",
    re.MULTILINE,
)


def _discover_sources(handler_file: Path) -> list[str]:
    """Parse a handler's ``Webhook as <Alias>`` import lines.

    The imports are the single source of truth. Adding/removing a source in
    the handler propagates automatically on the next invocation.
    """
    text = handler_file.read_text()
    sources = _SOURCE_RE.findall(text)
    if not sources:
        _fail(f"No 'Webhook as <Alias>' imports found in {handler_file}.")
    return sources


def _source_module_for(handler_file: Path, source: str) -> str:
    """Resolve ``source`` to the dotted module path it was imported from.

    Walks the handler text, tracks the most recent ``from src.… import (``
    line, and emits its dotted form when the matching ``Webhook as <source>``
    appears. Mirrors the bash ``awk`` block.
    """
    last_from: str | None = None
    target = f"Webhook as {source}"
    for raw_line in handler_file.read_text().splitlines():
        if re.match(r"^from src\.[A-Za-z0-9_.]+ import \(", raw_line):
            last_from = raw_line
            continue
        if target in raw_line and last_from is not None:
            stripped = re.sub(r"^from ", "", last_from)
            return re.sub(r" import \(.*$", "", stripped)
    _fail(f"Could not resolve module path for {source} in {handler_file}.")


# ---------------------------------------------------------------------------
# Preflights (all host-side)
# ---------------------------------------------------------------------------


def _preflight_env() -> None:
    """Require Infisical bootstrap creds + an explicit INFISICAL_ENV.

    ``INFISICAL_ENV`` is the slug each deployed function uses at request time
    to fetch its per-domain API keys via ``libs.infisical.fetch_all``. We
    require it explicitly here so an operator who forgets to export it does
    not silently land prod webhook traffic in the dev Attio workspace — the
    exact miss-route shape ai-2aw was filed to eliminate. ``libs.infisical``
    also fails closed at runtime; this catches it before image build.

    The parent shell's personal ``MODAL_TOKEN_*`` would silently win over
    Infisical-injected dlthub workspace tokens — deploys would land in the
    wrong workspace. Always unset.
    """
    for key in ("INFISICAL_PROJECT_ID", "INFISICAL_TOKEN"):
        if not os.environ.get(key):
            _fail(
                f"{key} is unset. Run: set -a && source .env.local && set +a",
            )
    env_slug = os.environ.get("INFISICAL_ENV", "")
    if not env_slug:
        _fail(
            "INFISICAL_ENV is unset. Export it explicitly before deploying "
            "(e.g. 'export INFISICAL_ENV=prod'). No default is applied — see "
            "ai-2aw.",
        )
    if env_slug not in VALID_INFISICAL_ENVS:
        _fail(
            f"INFISICAL_ENV='{env_slug}' not in "
            f"{{{','.join(VALID_INFISICAL_ENVS)}}}. Set one of those before "
            f"deploying.",
        )

    os.environ.pop("MODAL_TOKEN_ID", None)
    os.environ.pop("MODAL_TOKEN_SECRET", None)


def _preflight_working_tree() -> None:
    """Refuse to start if anything in ``webhooks/`` differs from HEAD.

    ``git diff --quiet`` only compares worktree against index — a
    staged-but-uncommitted edit would slip past. ``git status --porcelain``
    flags any deviation (staged, unstaged, or untracked).
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "webhooks/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        print(
            "ERROR: webhooks/ has uncommitted changes (staged, unstaged, or "
            "untracked). Aborting.",
            file=sys.stderr,
        )
        print(status, file=sys.stderr)
        sys.exit(1)


def _preflight_modal_secrets() -> None:
    """Verify every named Modal secret exists in the active workspace.

    Uses ``--json`` because the default table renderer truncates long names
    like ``devx-gcp-202605260000`` with ``…``, which would make a substring
    grep miss the secret even when it exists. Matches the full
    ``"Name": "<secret>"`` token to keep a prefix like ``devx-gcp`` from
    accidentally satisfying a longer name.
    """
    print(f"Preflighting Modal secrets ({len(REQUIRED_MODAL_SECRETS)} required)")
    proc = _infisical_run(
        ["uv", "run", "modal", "secret", "list", "--json"],
        env_slug="dev",
        capture_output=True,
    )
    if proc.returncode != 0:
        _fail(
            "Could not list Modal secrets — check Infisical token and Modal access.",
        )
    payload = proc.stdout
    for secret in REQUIRED_MODAL_SECRETS:
        needle = f'"Name": "{secret}"'
        if needle not in payload:
            _fail(
                "Missing one or more required Modal secrets in the dlthub "
                "workspace. Create required secrets before deploying.",
            )


def _preflight_infisical_keys(
    handler_file: Path,
    sources: Iterable[str],
) -> None:
    """Restore the deploy-time fail-fast for per-source Infisical API keys.

    ai-2aw moved ATTIO_API_KEY/CALCOM_API_KEY off named Modal Secrets and
    onto request-time ``libs.infisical.fetch_all``. That removed the
    deploy-time check that the secret existed; a typo or missing key in the
    target ``INFISICAL_ENV`` now ships cleanly and fails on the first
    Hookdeck event. Each ``Webhook`` subclass declares its keys via two
    static methods:

    - ``required_api_keys()`` — keys every event path on the source needs.
    - ``optional_api_keys()`` — keys reached lazily on only a subset of
      event types (e.g. ``CALCOM_API_KEY`` is only touched by caldotcom's
      ``BOOKING_NO_SHOW_UPDATED`` branch, so declaring it required would
      force the other Cal.com event types to fail-fast on a missing or
      rotated key they never use).

    We preflight the **union** so a missing/rotated key surfaces at deploy
    time instead of on the first qualifying Hookdeck event. Each key is
    verified with a separate ``infisical secrets get`` so the error names
    the specific missing secret. (See ai-q9k.)

    Important: ``infisical secrets get`` (CLI 0.43.84 against
    dlthub-sandbox/dev, confirmed 2026-05-26) exits **0 for both present
    and missing keys** and only differentiates via stdout — present keys
    print the value, missing keys print nothing. A pure ``returncode``
    check is therefore theater: the loop would always pass. We treat
    empty stdout (after strip) as 'missing' to match the only signal the
    CLI actually exposes. ``_fetch_infisical_value`` below uses the same
    pattern for MODAL_TOKEN_*. Do not "simplify" this back to a returncode
    check. (See ai-4pw.)
    """
    env_slug = os.environ["INFISICAL_ENV"]
    preflight: list[str] = []
    for source in sources:
        module = _source_module_for(handler_file, source)
        keys_text = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-c",
                (
                    f"from {module} import Webhook\n"
                    "for k in list(Webhook.required_api_keys()) + "
                    "list(Webhook.optional_api_keys()):\n"
                    "    print(k)\n"
                ),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if keys_text.returncode != 0:
            _fail(
                f"Could not resolve required_api_keys()/optional_api_keys() "
                f"for {source} via {module}.Webhook.",
            )
        for key in keys_text.stdout.splitlines():
            stripped = key.strip()
            if stripped and stripped not in preflight:
                preflight.append(stripped)

    if not preflight:
        return

    print(
        f"Preflighting Infisical keys in env={env_slug}: {' '.join(preflight)}",
    )
    for key in preflight:
        proc = subprocess.run(
            [
                "infisical",
                "secrets",
                "get",
                key,
                "--projectId",
                os.environ["INFISICAL_PROJECT_ID"],
                "--token",
                os.environ["INFISICAL_TOKEN"],
                f"--env={env_slug}",
                "--plain",
                "--silent",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            _fail(
                f"Missing Infisical secret '{key}' in env={env_slug}. Set it "
                f"before deploying (declared by {handler_file.name} "
                f"source(s): {' '.join(sources)}).",
            )
        print(f"  {key} ✓")


_BUCKET_METHOD_RE = re.compile(r"WebhookModel\.([a-z_]+_get_bucket_name)")


def _preflight_gcs_buckets(
    handler_file: Path,
    sources: Iterable[str],
) -> None:
    """Verify the per-source GCS bucket exists for handlers that write to gs://.

    Auto-detects whether the handler routes to a per-source bucket by
    matching ``WebhookModel.<prefix>_get_bucket_name`` in its source. The
    Attio handler doesn't write to GCS, so this pattern is absent and the
    preflight is skipped.
    """
    text = handler_file.read_text()
    match = _BUCKET_METHOD_RE.search(text)
    if match is None:
        return
    bucket_method = match.group(1)

    if shutil.which("gcloud") is None:
        _fail(
            f"gcloud CLI not found — required to preflight GCS buckets for "
            f"{handler_file.stem}.",
        )

    print(f"Preflighting GCS buckets via WebhookModel.{bucket_method}()")
    for source in sources:
        module = _source_module_for(handler_file, source)
        bucket_proc = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-c",
                (f"from {module} import Webhook\nprint(Webhook.{bucket_method}())\n"),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if bucket_proc.returncode != 0:
            _fail(
                f"Could not resolve bucket name for {source} via "
                f"{module}.Webhook.{bucket_method}().",
            )
        bucket = bucket_proc.stdout.strip()
        ls = subprocess.run(
            [
                "gcloud",
                "storage",
                "ls",
                "--project=dlthub-sandbox",
                f"gs://{bucket}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if ls.returncode != 0:
            _fail(
                f"Missing GCS bucket: gs://{bucket} (source {source}). "
                f"Create it before deploying.",
            )
        print(f"  {source}: gs://{bucket} ✓")


# ---------------------------------------------------------------------------
# Lock / backup / restore / cleanup
# ---------------------------------------------------------------------------


def _acquire_lock() -> None:
    """Atomic advisory lock via ``mkdir`` semantics.

    ``Path.mkdir(exist_ok=False)`` raises ``FileExistsError`` atomically on
    every POSIX filesystem. Avoids ``flock`` (not installed by default on
    macOS) and the standard race window of ``if exists ... mkdir``.
    """
    global _lock_acquired  # noqa: PLW0603 — module-level state read by atexit
    (REPO_ROOT / "tmp").mkdir(exist_ok=True)
    try:
        LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        _fail(
            f"Another scripts/redeploy_webhook.py invocation appears to be "
            f"running (lock dir {LOCK_DIR.relative_to(REPO_ROOT)} exists). "
            f"If you are sure it is not, rmdir "
            f"{LOCK_DIR.relative_to(REPO_ROOT)} and retry.",
        )
    _lock_acquired = True


def _release_lock() -> None:
    """Best-effort lock release. Safe to call when the lock was never acquired."""
    if not _lock_acquired:
        return
    try:
        LOCK_DIR.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _write_backup(handler_file: Path) -> None:
    """Snapshot the current handler so cleanup can always restore it.

    Clear any stale backups from prior runs *before* taking the fresh one.
    Otherwise a leftover backup — possibly for a different handler — would
    be the restore source, leaving the worktree dirty. Safe because the
    working-tree preflight already guaranteed ``webhooks/`` matches HEAD.
    """
    global _backup_freshly_written  # noqa: PLW0603 — read by atexit/signals
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BACKUP_DIR.glob("*.py"):
        stale.unlink()
    shutil.copyfile(handler_file, BACKUP_DIR / f"{handler_file.stem}.py")
    _backup_freshly_written = True


def _restore_handler() -> None:
    """Restore the current handler from its backup.

    Gated on ``_backup_freshly_written`` so an early-failure path (e.g.
    Modal preflight error) cannot copy a stale backup from a prior run on
    top of a clean worktree. ``shutil.copyfile`` always overwrites — no
    interactive-cp alias risk.
    """
    if not _backup_freshly_written or _handler is None or _handler_file is None:
        return
    backup = BACKUP_DIR / f"{_handler}.py"
    if backup.exists():
        shutil.copyfile(backup, _handler_file)


def _cleanup() -> None:
    """Restore the handler and release the lock. Idempotent."""
    _restore_handler()
    _release_lock()


def _install_signal_handlers() -> None:
    """Convert SIGINT/SIGTERM into ``sys.exit`` so ``atexit`` runs.

    Without this, a SIGINT during ``modal deploy`` would terminate the
    process without firing the ``atexit``-registered cleanup, leaving the
    substituted handler file in the worktree.
    """

    def _handler(signum: int, _frame: object) -> None:
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# Deploy (Dagger-wrapped)
# ---------------------------------------------------------------------------


def _resolve_modal_tokens() -> tuple[str, str]:
    """Pull MODAL_TOKEN_ID / MODAL_TOKEN_SECRET from Infisical for Dagger.

    Each token is fetched in its own ``infisical secrets get`` call. The
    obvious alternative — ``infisical run -- printenv VAR1 VAR2`` — breaks
    on macOS where BSD ``printenv`` only prints the first matching name (a
    silent divergence from GNU ``printenv``). One subprocess per token also
    means an error message can name the specific missing var instead of
    conflating them.

    Personal Modal tokens were already popped from ``os.environ`` in
    ``_preflight_env``; the values returned here flow straight into Dagger
    ``set_secret`` calls and never land in the script's env.
    """
    return _fetch_infisical_value("MODAL_TOKEN_ID"), _fetch_infisical_value(
        "MODAL_TOKEN_SECRET",
    )


def _fetch_infisical_value(name: str) -> str:
    proc = subprocess.run(
        [
            "infisical",
            "secrets",
            "get",
            name,
            "--projectId",
            os.environ["INFISICAL_PROJECT_ID"],
            "--token",
            os.environ["INFISICAL_TOKEN"],
            "--env=dev",
            "--plain",
            "--silent",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        _fail(
            f"Could not fetch '{name}' from Infisical env=dev. Is it set in "
            f"the dlthub project?",
        )
    return proc.stdout.strip()


async def _deploy_via_dagger(
    handler_file: Path,
    modal_token_id: str,
    modal_token_secret: str,
    infisical_token: str,
    infisical_project_id: str,
    infisical_env: str,
    infisical_host: str | None,
) -> None:
    """Run ``uv sync --frozen && uv run modal deploy <handler>`` in a container.

    Mounts the repo at ``/repo`` (excluding ``.venv``, ``tmp/``, bytecode
    caches, and **both** the worktree's ``.git`` file/dir — worktrees use
    a gitlink file, not a directory, and a stray gitlink inside the
    container points back at host-only git metadata that would break any
    git-aware step), syncs the venv from the pinned lock file, and invokes
    ``modal deploy`` with Modal tokens *and* the full Infisical bootstrap
    env injected as Dagger secrets.

    The Infisical creds are required because each handler's
    ``_bootstrap_secret()`` reads ``INFISICAL_TOKEN`` /
    ``INFISICAL_PROJECT_ID`` / ``INFISICAL_ENV`` / optionally
    ``INFISICAL_HOST`` from ``os.environ`` at module-import time and bakes
    them into a ``modal.Secret.from_dict`` that the deployed app uses at
    request time to call ``libs.infisical.fetch_all``. Without these, the
    deploy would succeed but the app would ``InfisicalAuthError`` on the
    first webhook event. ``INFISICAL_HOST`` is forwarded only when set on
    the host so a missing self-host config doesn't fabricate an empty
    string that confuses ``libs/infisical``. All values flow in as Dagger
    secrets so they never appear in image layers or in Dagger's stderr.
    """
    rel = handler_file.relative_to(REPO_ROOT).as_posix()
    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        secrets = {
            "MODAL_TOKEN_ID": dagger.dag.set_secret(
                "modal-token-id",
                modal_token_id,
            ),
            "MODAL_TOKEN_SECRET": dagger.dag.set_secret(
                "modal-token-secret",
                modal_token_secret,
            ),
            "INFISICAL_TOKEN": dagger.dag.set_secret(
                "infisical-token",
                infisical_token,
            ),
            "INFISICAL_PROJECT_ID": dagger.dag.set_secret(
                "infisical-project-id",
                infisical_project_id,
            ),
            "INFISICAL_ENV": dagger.dag.set_secret(
                "infisical-env",
                infisical_env,
            ),
        }
        if infisical_host:
            secrets["INFISICAL_HOST"] = dagger.dag.set_secret(
                "infisical-host",
                infisical_host,
            )
        src = dagger.dag.host().directory(
            str(REPO_ROOT),
            exclude=[
                ".venv/",
                "tmp/",
                "**/__pycache__/",
                "*.pyc",
                # Belt-and-suspenders for both worktree shapes:
                #   - main checkout: ``.git`` is a directory.
                #   - linked worktree: ``.git`` is a gitlink file pointing
                #     back at host-only metadata that is unreachable from
                #     inside the container.
                ".git",
                ".git/",
            ],
        )
        container = (
            dagger.dag.container()
            .from_(DAGGER_BASE_IMAGE)
            .with_directory("/repo", src)
            .with_workdir("/repo")
            .with_exec(["uv", "sync", "--frozen"])
        )
        for name, secret in secrets.items():
            container = container.with_secret_variable(name, secret)
        await container.with_exec(
            ["uv", "run", "modal", "deploy", rel],
        ).sync()


def _deploy_via_host_subprocess(handler_file: Path) -> None:
    """Test-only fallback: run modal deploy on the host (no Dagger engine needed).

    Activated by ``DAGGER_DRY_RUN=1``. Mirrors the bash script's deploy
    shape so the existing stub-binary tests (``infisical``/``modal``/``uv``
    in ``tests/scripts/test_deploy_webhook.py``) exercise the same code
    path without requiring a real Dagger engine in CI.
    """
    rel = handler_file.relative_to(REPO_ROOT).as_posix()
    subprocess.run(
        [
            "infisical",
            "run",
            "--projectId",
            os.environ["INFISICAL_PROJECT_ID"],
            "--token",
            os.environ["INFISICAL_TOKEN"],
            "--env=dev",
            "--",
            "uv",
            "run",
            "modal",
            "deploy",
            rel,
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def _verify_clean_restore(handler_file: Path) -> None:
    """Confirm restore left the file matching HEAD; fail loudly if not."""
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(handler_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not status.strip():
        return
    print(
        f"ERROR: {handler_file.relative_to(REPO_ROOT)} is dirty after restore "
        f"— placeholder swap failed.",
        file=sys.stderr,
    )
    print(status, file=sys.stderr)
    subprocess.run(
        ["git", "diff", "HEAD", "--", str(handler_file)],
        cwd=REPO_ROOT,
        check=False,
    )
    sys.exit(1)


def _deploy_one(handler_file: Path, source: str) -> None:
    """Substitute placeholder → deploy → restore from backup → verify clean."""
    assert _handler is not None  # set by main() before the loop
    print()
    print(f"=== Deploying {source} via {_handler} ===")

    original = handler_file.read_text()
    handler_file.write_text(original.replace(PLACEHOLDER, source))

    try:
        if os.environ.get("DAGGER_DRY_RUN") == "1":
            _deploy_via_host_subprocess(handler_file)
        else:
            modal_token_id, modal_token_secret = _resolve_modal_tokens()
            asyncio.run(
                _deploy_via_dagger(
                    handler_file,
                    modal_token_id=modal_token_id,
                    modal_token_secret=modal_token_secret,
                    infisical_token=os.environ["INFISICAL_TOKEN"],
                    infisical_project_id=os.environ["INFISICAL_PROJECT_ID"],
                    infisical_env=os.environ["INFISICAL_ENV"],
                    # Forward only when set so we don't bake an empty
                    # INFISICAL_HOST into the runtime bootstrap secret.
                    infisical_host=os.environ.get("INFISICAL_HOST") or None,
                ),
            )
    finally:
        # Restore unconditionally — even on deploy failure — so the next
        # iteration starts from a clean placeholder state and so a SIGINT
        # between substitute and deploy still ends with a clean worktree.
        backup = BACKUP_DIR / f"{handler_file.stem}.py"
        if backup.exists():
            shutil.copyfile(backup, handler_file)

    _verify_clean_restore(handler_file)


# ---------------------------------------------------------------------------
# Subprocess + error helpers
# ---------------------------------------------------------------------------


def _infisical_run(
    inner_cmd: list[str],
    *,
    env_slug: str,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``infisical run`` with the project's bootstrap creds.

    Bash equivalent of ``infisical run --projectId … --token … --env=<slug>
    -- <inner_cmd>``. Always called with a list (never a string and never
    via a shell), which sidesteps the "infisical-run-as-argv0" footgun where
    storing the prefix in a shell variable expands wrong under zsh.
    """
    cmd = [
        "infisical",
        "run",
        "--projectId",
        os.environ["INFISICAL_PROJECT_ID"],
        "--token",
        os.environ["INFISICAL_TOKEN"],
        f"--env={env_slug}",
        "--",
        *inner_cmd,
    ]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=capture_output,
        text=True,
        check=False,
    )


def _fail(msg: str) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(handlers: list[str]) -> tuple[str, str]:
    """Parse argv into ``(handler, source_or_all)``.

    The user-facing UX matches the bash predecessor:

        scripts/redeploy_webhook.py <handler> <source>
        scripts/redeploy_webhook.py <handler> --all

    ``--all`` is implemented as an explicit flag rather than a positional
    sentinel because argparse parses a leading-dash positional as an
    unknown option, breaking the documented invocation. The caller above
    sees a single string (either the alias name or the literal ``--all``)
    and dispatches on that, so the internal contract stays the same.
    """
    parser = argparse.ArgumentParser(
        prog="scripts/redeploy_webhook.py",
        description=(
            "Substitute the WebhookModelToReplace placeholder, deploy via "
            "Dagger-wrapped `modal deploy`, then restore the handler. "
            "See AGENTS.md → 'Scripted deploy pitfalls' for the full set "
            "of footguns this encodes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Preconditions:\n"
            "  - INFISICAL_PROJECT_ID and INFISICAL_TOKEN exported\n"
            "    (run: set -a && source .env.local && set +a)\n"
            "  - INFISICAL_ENV exported (dev|staging|prod) — no default\n"
            "  - working tree under webhooks/ is clean\n"
            "  - required Modal secrets exist in the dlthub workspace\n"
            f"\nDiscovered handlers: {' '.join(handlers)}"
        ),
    )
    parser.add_argument("handler", help=f"one of: {' '.join(handlers)}")
    parser.add_argument(
        "source",
        nargs="?",
        help="a 'Webhook as <Alias>' alias imported by the handler",
    )
    parser.add_argument(
        "--all",
        dest="all_sources",
        action="store_true",
        help="deploy every source imported by the handler",
    )
    args = parser.parse_args()
    if args.all_sources and args.source is not None:
        parser.error("specify either <source> or --all, not both")
    if args.all_sources:
        return args.handler, "--all"
    if args.source is None:
        parser.error("specify a <source> alias or pass --all")
    return args.handler, args.source


def main() -> int:
    global _handler, _handler_file  # noqa: PLW0603 — module state for cleanup

    handlers = _discover_handlers()
    handler, source_or_all = _parse_args(handlers)
    if handler not in handlers:
        print(
            f"ERROR: Unknown handler: {handler}\n"
            f"  Valid handlers: {' '.join(handlers)}",
            file=sys.stderr,
        )
        return 1

    handler_file = WEBHOOKS_DIR / f"{handler}.py"
    if not handler_file.exists():
        _fail(f"Handler file not found: {handler_file}")

    valid_sources = _discover_sources(handler_file)
    if source_or_all == "--all":
        sources_to_deploy = list(valid_sources)
    elif source_or_all in valid_sources:
        sources_to_deploy = [source_or_all]
    else:
        print(
            f"ERROR: Unknown source: {source_or_all}\n"
            f"  Sources imported by {handler_file.relative_to(REPO_ROOT)}: "
            f"{' '.join(valid_sources)}",
            file=sys.stderr,
        )
        return 1

    _preflight_env()

    # Acquire lock *before* the working-tree preflight so the snapshot below
    # cannot become stale between check and mutation. Install cleanup
    # immediately after the lock so a Ctrl-C between here and the deploy
    # always releases the lock + (eventually) restores the handler.
    _acquire_lock()
    atexit.register(_cleanup)
    _install_signal_handlers()

    _preflight_working_tree()
    _preflight_modal_secrets()
    _preflight_infisical_keys(handler_file, sources_to_deploy)
    _preflight_gcs_buckets(handler_file, sources_to_deploy)

    _handler = handler
    _handler_file = handler_file
    _write_backup(handler_file)

    for source in sources_to_deploy:
        _deploy_one(handler_file, source)

    print()
    print("All deploys complete. Working tree clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
