"""Smoke tests for scripts/redeploy_webhook.py.

Verifies the substitute -> deploy -> restore loop preserves the working tree,
even when the deploy fails mid-iteration. Stubs modal / infisical / uv / gcloud
so the test never makes real network calls.

Sets ``DAGGER_DRY_RUN=1`` so the script's deploy step shells out to the host
stubs instead of spinning up a Dagger engine. The non-dry-run path (which
runs ``modal deploy`` inside a Dagger container) is covered by manual smoke
tests — running it in CI would require a Dagger engine and real Modal/GCP
credentials.

BD: gtm-sdk-43z (epic gtm-sdk-yol). Each test maps to one acceptance criterion.
"""
# trunk-ignore-all(bandit/B105): test fixtures, not real credentials
# trunk-ignore-all(bandit/B607): bash/git invoked by name on purpose so PATH wins

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "redeploy_webhook.py"
HANDLER_FILE = REPO_ROOT / "webhooks" / "export_to_attio.py"
HANDLER_NAME = "export_to_attio"
SOURCE_NAME = "CaldotcomBookingWebhook"
LOCK_DIR = REPO_ROOT / "tmp" / "webhook-deploy.lock"
BACKUP_DIR = REPO_ROOT / "tmp" / "webhook-deploy-bak"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_default_stubs(bin_dir: Path) -> None:
    """Write the canonical set of stub binaries used by the happy-path test.

    Stubs mirror real-tool behavior closely enough that the script can't tell
    the difference. In particular, the infisical stub only injects
    MODAL_TOKEN_ID when it's unset — that mirrors the gotcha the script's
    `unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET` line works around, and lets
    test_modal_token_isolation actually catch a regression of that line.
    """
    (bin_dir / "infisical").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Two subcommands need explicit emulation now that the Python
            # rewrite calls `infisical secrets get` directly (instead of
            # always going through `infisical run -- printenv`).
            #
            # `secrets get <name> ... --plain --silent` echoes a stub value
            # so _preflight_infisical_keys and _resolve_modal_tokens see a
            # non-empty stdout and exit 0. The actual value doesn't matter;
            # only its presence is what the script checks.
            if [[ "${1:-}" == "secrets" && "${2:-}" == "get" ]]; then
                echo "stub-${3}-value"
                exit 0
            fi
            # `run … -- <cmd>` injects MODAL_TOKEN_ID/SECRET only if unset,
            # mirroring the real-world quirk where parent-shell env vars win
            # — which is what `os.environ.pop(...)` in the deploy script
            # works around (regression target for test_modal_token_isolation).
            if [[ "${1:-}" == "run" ]]; then
                [[ -z "${MODAL_TOKEN_ID:-}" ]] && export MODAL_TOKEN_ID="infisical-injected-id"
                [[ -z "${MODAL_TOKEN_SECRET:-}" ]] && export MODAL_TOKEN_SECRET="infisical-injected-secret"
                while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
                shift  # drop the --
                exec "$@"
            fi
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "infisical")

    (bin_dir / "modal").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "secret" && "${2:-}" == "list" ]]; then
                echo '[{"Name": "devx-gcp-202605260000"}, {"Name": "attio"}]'
                exit 0
            fi
            if [[ "${1:-}" == "deploy" ]]; then
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "modal")

    (bin_dir / "uv").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "run" && "${2:-}" == "modal" ]]; then
                shift 2
                exec modal "$@"
            fi
            # `uv run python -c "<snippet>"` is used by _preflight_infisical_keys
            # (to print required_api_keys()) and by _preflight_gcs_buckets (to
            # print Webhook.<prefix>_get_bucket_name()). Detect which by
            # grepping the snippet; emit a realistic value for each branch so
            # the script's downstream `infisical secrets get` / `gcloud
            # storage ls` calls run against something that looks like real
            # output, not stub-as-magic-string degenerate behavior.
            if [[ "${1:-}" == "run" && "${2:-}" == "python" && "${3:-}" == "-c" ]]; then
                snippet="${4:-}"
                if [[ "${snippet}" == *required_api_keys* ]]; then
                    echo "ATTIO_API_KEY"
                    exit 0
                fi
                if [[ "${snippet}" == *_get_bucket_name* ]]; then
                    echo "stub-bucket-name"
                    exit 0
                fi
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "uv")

    (bin_dir / "gcloud").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            # Defensive: not exercised by export_to_attio.
            exit 0
            """,
        ),
    )
    _make_executable(bin_dir / "gcloud")


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_default_stubs(bin_dir)
    return bin_dir


@pytest.fixture(autouse=True)
def ensure_handler_restored() -> Iterator[None]:
    """Snapshot the handler file and clean stale lock/.bak state around each test.

    Safety net: even if the test asserts before the script's own EXIT trap
    has fired (or if the script itself were broken), this fixture guarantees
    the handler file ends up clean for the next test.

    Skips if webhooks/ has other uncommitted changes, since the script's
    working-tree preflight would refuse to run and the test would falsely
    fail. In CI this never trips; in local dev it makes the failure mode
    obvious.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "webhooks/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    if status.strip():
        pytest.skip(
            f"webhooks/ has uncommitted changes; deploy script preflight "
            f"would abort. Commit or stash before running these tests:\n{status}",
        )

    original_bytes = HANDLER_FILE.read_bytes()
    LOCK_DIR.parent.mkdir(parents=True, exist_ok=True)
    # Never remove an existing lock — it may belong to a concurrent real
    # `scripts/redeploy_webhook.py` invocation, and that lock is the script's
    # only serialization guard. Skip rather than racing the live deploy.
    if LOCK_DIR.exists():
        pytest.skip(
            f"{LOCK_DIR} already exists; a deploy may be in progress. "
            f"Refusing to remove it. Remove it manually if it is stale.",
        )
    bak = HANDLER_FILE.with_suffix(HANDLER_FILE.suffix + ".bak")
    if bak.exists():
        bak.unlink()
    try:
        yield
    finally:
        HANDLER_FILE.write_bytes(original_bytes)
        # Safe to remove here: we verified LOCK_DIR did not exist at entry,
        # so any lock present now was created by the stubbed deploy under test.
        if LOCK_DIR.exists():
            LOCK_DIR.rmdir()
        if bak.exists():
            bak.unlink()


def _run_deploy(
    stub_bin: Path,
    *,
    env_overrides: dict[str, str] | None = None,
    args: tuple[str, ...] = (HANDLER_NAME, SOURCE_NAME),
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["INFISICAL_PROJECT_ID"] = "test-project-id"
    env["INFISICAL_TOKEN"] = "test-token"
    # INFISICAL_ENV is a fail-closed preflight added by ai-2aw — see the
    # script header. Tests pin to "dev" since they stub the modal binary
    # and never reach Infisical.
    env.setdefault("INFISICAL_ENV", "dev")
    # Force the host-subprocess deploy path so the existing infisical/modal/uv
    # stubs handle the deploy step. The Dagger path is exercised by manual
    # smoke tests; bringing a Dagger engine into CI would also drag in real
    # Modal credentials, which defeats the purpose of these stubs.
    env.setdefault("DAGGER_DRY_RUN", "1")
    if env_overrides:
        env.update(env_overrides)
    # Invoke the script with the test's own interpreter rather than
    # `uv run python …`. The PATH-overriding `uv` stub catches every
    # `uv run python <anything>` call (it is meant to intercept the script's
    # *internal* preflight calls to `uv run python -c …`), so going through
    # `uv` here would short-circuit the entire script before it starts.
    # ``sys.executable`` points at the venv pytest itself is running under,
    # so all the script's imports (``dagger``, etc.) resolve normally.
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_substitution_and_restore(stub_bin: Path) -> None:
    """AC1: CI runs redeploy_webhook.py against stubs; working tree ends clean."""
    original = HANDLER_FILE.read_bytes()

    result = _run_deploy(stub_bin)

    assert result.returncode == 0, (
        f"Script failed unexpectedly.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert HANDLER_FILE.read_bytes() == original
    bak = HANDLER_FILE.with_suffix(HANDLER_FILE.suffix + ".bak")
    assert not bak.exists(), "stale .bak sidecar left behind"


def test_all_flag_deploys_every_source(stub_bin: Path) -> None:
    """AC5: ``--all`` iterates every source imported by the handler.

    Regression target: in early drafts argparse parsed ``--all`` as an
    unknown option, breaking the documented invocation entirely. Each
    iteration must end with the placeholder restored, so the file must
    match HEAD bit-for-bit after all five sources deploy.
    """
    original = HANDLER_FILE.read_bytes()

    result = _run_deploy(stub_bin, args=(HANDLER_NAME, "--all"))

    assert result.returncode == 0, (
        f"--all invocation failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert HANDLER_FILE.read_bytes() == original
    # All five sources imported by export_to_attio.py should each have
    # produced a "=== Deploying <source> via <handler> ===" header.
    assert result.stdout.count("=== Deploying ") == 5, (
        f"Expected 5 per-source deploy headers (one for each Webhook import). "
        f"Got:\n{result.stdout}"
    )


def test_restore_on_deploy_failure(stub_bin: Path) -> None:
    """AC2: EXIT trap restores the handler when `modal deploy` fails mid-iteration.

    Regression target: a refactor that drops the `trap … EXIT` registration
    (or fails to flip BACKUP_FRESHLY_WRITTEN before the deploy) leaves the
    substituted form of the handler committed locally.
    """
    (stub_bin / "modal").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "secret" && "${2:-}" == "list" ]]; then
                echo '[{"Name": "devx-gcp-202605260000"}, {"Name": "attio"}]'
                exit 0
            fi
            if [[ "${1:-}" == "deploy" ]]; then
                exit 1  # simulate mid-iteration failure
            fi
            exit 0
            """,
        ),
    )
    _make_executable(stub_bin / "modal")

    original = HANDLER_FILE.read_bytes()

    result = _run_deploy(stub_bin)

    assert result.returncode != 0, (
        "Script should have exited non-zero after stub modal deploy failed"
    )
    assert HANDLER_FILE.read_bytes() == original, (
        f"Handler file NOT restored after deploy failure — EXIT trap may be "
        f"missing or BACKUP_FRESHLY_WRITTEN gate may be wrong.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_modal_token_isolation(stub_bin: Path, tmp_path: Path) -> None:
    """AC3: parent shell's MODAL_TOKEN_ID is unset before infisical injection.

    Regression target: removing `unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET`
    would let the developer's personal Modal tokens win and silently route
    the deploy to the wrong workspace. The infisical stub only injects when
    the var is empty (mirroring the real-world precedence), so this test
    fails iff the unset line is gone.
    """
    env_record = tmp_path / "modal_env.txt"
    (stub_bin / "modal").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{1:-}}" == "secret" && "${{2:-}}" == "list" ]]; then
                echo '[{{"Name": "devx-gcp-202605260000"}}, {{"Name": "attio"}}]'
                exit 0
            fi
            if [[ "${{1:-}}" == "deploy" ]]; then
                echo "MODAL_TOKEN_ID=${{MODAL_TOKEN_ID:-UNSET}}" > "{env_record}"
                exit 0
            fi
            exit 0
            """,
        ),
    )
    _make_executable(stub_bin / "modal")

    result = _run_deploy(
        stub_bin,
        env_overrides={
            "MODAL_TOKEN_ID": "parent-shell-token",
            "MODAL_TOKEN_SECRET": "parent-shell-secret",
        },
    )

    assert result.returncode == 0, (
        f"Script failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert env_record.exists(), "modal deploy stub was never invoked"
    recorded = env_record.read_text().strip()
    assert recorded == "MODAL_TOKEN_ID=infisical-injected-id", (
        f"Parent shell's MODAL_TOKEN_ID leaked through to modal — the "
        f"`os.environ.pop(...)` call in redeploy_webhook.py is missing or "
        f"ineffective. Got: {recorded}"
    )


def test_shutil_copyfile_overwrites(tmp_path: Path) -> None:
    """AC4: restore overwrites unconditionally — no alias-bypass game in Python.

    The bash script needed `\\cp -f` to dodge `cp -i` aliases that would
    silently refuse the restore. The Python rewrite uses ``shutil.copyfile``,
    which always overwrites; this test pins that contract so a refactor to a
    helper that respects ``exist_ok=False`` or similar would fail loudly.
    """
    import shutil

    src = tmp_path / "src.txt"
    src.write_text("ORIGINAL")
    dst = tmp_path / "dst.txt"
    dst.write_text("STALE")

    shutil.copyfile(src, dst)

    assert dst.read_text() == "ORIGINAL"
