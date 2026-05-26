"""Smoke tests for scripts/redeploy-webhook.sh.

Verifies the substitute -> deploy -> restore loop preserves the working tree,
even when the deploy fails mid-iteration. Stubs modal / infisical / uv / gcloud
so the test never makes real network calls.

BD: gtm-sdk-43z (epic gtm-sdk-yol). Each test maps to one acceptance criterion.
"""
# trunk-ignore-all(bandit/B105): test fixtures, not real credentials
# trunk-ignore-all(bandit/B607): bash/git invoked by name on purpose so PATH wins

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "redeploy-webhook.sh"
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
            # Inject Modal credentials only if not already set, mirroring the
            # real-world quirk where parent-shell env vars win — which is
            # exactly what `unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET` in the
            # deploy script works around.
            [[ -z "${MODAL_TOKEN_ID:-}" ]] && export MODAL_TOKEN_ID="infisical-injected-id"
            [[ -z "${MODAL_TOKEN_SECRET:-}" ]] && export MODAL_TOKEN_SECRET="infisical-injected-secret"
            while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
            shift  # drop the --
            exec "$@"
            """,
        ),
    )
    _make_executable(bin_dir / "infisical")

    (bin_dir / "modal").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "${1:-}" == "secret" && "${2:-}" == "list" ]]; then
                echo '[{"Name": "devx-gcp-202605111323"}, {"Name": "attio"}]'
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
            if [[ "${1:-}" == "run" && "${2:-}" == "python" ]]; then
                # Defensive: export_to_attio doesn't trigger the bucket
                # preflight, so this branch is unreachable for the current
                # test handler. Kept so the stub set works for export_to_gcp_*.
                echo "stub-bucket-name"
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
    # `scripts/redeploy-webhook.sh` invocation, and that lock is the script's
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
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_substitution_and_restore(stub_bin: Path) -> None:
    """AC1: CI runs redeploy-webhook.sh against stubs; working tree ends clean."""
    original = HANDLER_FILE.read_bytes()

    result = _run_deploy(stub_bin)

    assert result.returncode == 0, (
        f"Script failed unexpectedly.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert HANDLER_FILE.read_bytes() == original
    bak = HANDLER_FILE.with_suffix(HANDLER_FILE.suffix + ".bak")
    assert not bak.exists(), "stale .bak sidecar left behind"


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
                echo '[{"Name": "devx-gcp-202605111323"}, {"Name": "attio"}]'
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
                echo '[{{"Name": "devx-gcp-202605111323"}}, {{"Name": "attio"}}]'
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
        f"Parent shell's MODAL_TOKEN_ID leaked through to modal — the `unset` "
        f"line in redeploy-webhook.sh is missing or ineffective. Got: {recorded}"
    )


def test_backslash_cp_bypasses_alias(tmp_path: Path) -> None:
    r"""AC4: `\cp -f` bypasses a `cp` alias the way the script relies on.

    Direct unit test of the bash mechanism. With aliases force-expanded in a
    non-interactive shell (the worst-case environment), a plain `cp` would
    hit the rigged alias and fail; `\cp` must fall through to the real
    binary and copy the file.
    """
    src = tmp_path / "src.txt"
    src.write_text("ORIGINAL")
    dst = tmp_path / "dst.txt"
    dst.write_text("STALE")

    script = textwrap.dedent(
        f"""\
        shopt -s expand_aliases
        alias cp='echo "alias-blocked" >&2; false'
        \\cp -f '{src}' '{dst}'
        """,
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, (
        f"`\\cp -f` failed even with alias bypass — "
        f"the deploy script's restore mechanism would not work:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert dst.read_text() == "ORIGINAL", (
        "`\\cp -f` did not overwrite the destination — alias won over backslash"
    )
