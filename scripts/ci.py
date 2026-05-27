"""Run every CI check the GitHub workflows run, in one Dagger invocation.

Mirrors the three workflows under `.github/workflows/`:

  - trunk-check.yml         -> `trunk check --all` (defined here; not run in
                               Dagger upstream)
  - tests-unit.yml          -> imports `.github/workflows/ci/pytest_dagger.py`
  - tests-integration.yml   -> imports `.github/workflows/ci/pytest_integration_dagger.py`

The pytest pipelines are imported from the actual workflow scripts so any
change there propagates here automatically. Trunk lives inline because there
is no upstream Dagger pipeline for it.

Usage:

    dagger run python scripts/ci.py                       # all jobs
    dagger run python scripts/ci.py --skip integration
    dagger run python scripts/ci.py --only unit

Integration tests are skipped (not failed) when `INFISICAL_TOKEN` /
`INFISICAL_PROJECT_ID` are absent. Jobs run concurrently; the script exits
non-zero if any job fails.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import anyio
import dagger
from dagger import dag

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_CI_DIR = REPO_ROOT / ".github" / "workflows" / "ci"
TMP_DIR = REPO_ROOT / "tmp"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"could not load module from {path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pytest_dagger = _load_module(
    "pytest_dagger",
    WORKFLOW_CI_DIR / "pytest_dagger.py",
)
pytest_integration_dagger = _load_module(
    "pytest_integration_dagger",
    WORKFLOW_CI_DIR / "pytest_integration_dagger.py",
)

# Reuse the source-exclude list and git-init shim from the unit pipeline so the
# trunk container matches what the unit tests see.
SOURCE_EXCLUDES = pytest_dagger.SOURCE_EXCLUDES
GIT_INIT_CMD = pytest_dagger.GIT_INIT_CMD

TRUNK_INSTALL = "curl -fsSL https://get.trunk.io | bash -s -- -y"
TRUNK_CMD = "trunk check --all --ci"


@dataclass
class JobResult:
    name: str
    ok: bool
    detail: str = ""


def _dump_exec_error(name: str, exc: dagger.ExecError) -> Path:
    """Write the failing exec's stdout/stderr to tmp/ci-<name>.log and return the path."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    log_path = TMP_DIR / f"ci-{name}.log"
    parts = [
        f"=== {name} exit code: {exc.exit_code} ===\n",
        f"\n=== {name} stdout ===\n",
        exc.stdout or "",
        f"\n=== {name} stderr ===\n",
        exc.stderr or "",
        "\n",
    ]
    log_path.write_text("".join(parts))
    return log_path


async def run_unit(results: list[JobResult]) -> None:
    try:
        ctr = pytest_dagger.build_container()
        await ctr.sync()
        results.append(JobResult("unit", ok=True))
    except dagger.ExecError as exc:
        log = _dump_exec_error("unit", exc)
        results.append(
            JobResult("unit", ok=False, detail=f"exit {exc.exit_code} (log: {log})"),
        )
    except Exception as exc:  # noqa: BLE001
        results.append(JobResult("unit", ok=False, detail=str(exc)))


async def run_integration(results: list[JobResult]) -> None:
    token = os.environ.get("INFISICAL_TOKEN")
    project_id = os.environ.get("INFISICAL_PROJECT_ID")
    if not token or not project_id:
        results.append(
            JobResult(
                "integration",
                ok=True,
                detail="skipped (INFISICAL_TOKEN/INFISICAL_PROJECT_ID not set)",
            ),
        )
        return
    try:
        ctr = pytest_integration_dagger.build_container(token, project_id)
        await ctr.sync()
        results.append(JobResult("integration", ok=True))
    except dagger.ExecError as exc:
        log = _dump_exec_error("integration", exc)
        results.append(
            JobResult(
                "integration",
                ok=False,
                detail=f"exit {exc.exit_code} (log: {log})",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        results.append(JobResult("integration", ok=False, detail=str(exc)))


async def run_trunk(
    results: list[JobResult],
    apt_cache,
    uv_cache,
    trunk_launcher_cache,
    trunk_user_cache,
) -> None:
    try:
        source = dag.host().directory(".", exclude=SOURCE_EXCLUDES)
        ctr = (
            dag.container()
            .from_("python:3.13")
            .with_exec(
                ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            )
            .with_env_variable("PATH", "/root/.local/bin:/usr/local/bin:/usr/bin:/bin")
            .with_mounted_cache("/root/.cache/uv", uv_cache)
            .with_mounted_cache("/var/cache/apt", apt_cache)
            .with_exec(
                [
                    "bash",
                    "-c",
                    "apt-get update && apt-get install -y git curl ca-certificates",
                ],
            )
            .with_mounted_cache("/root/.cache/trunk", trunk_user_cache)
            .with_mounted_cache("/root/.trunk", trunk_launcher_cache)
            .with_exec(["bash", "-c", TRUNK_INSTALL])
            .with_env_variable(
                "PATH",
                "/root/.trunk/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
            )
            .with_directory("/src", source)
            .with_workdir("/src")
            .with_exec(["bash", "-c", f"rm -rf .git && {GIT_INIT_CMD}"])
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_env_variable("VIRTUAL_ENV", "/src/.venv")
            .with_env_variable(
                "PATH",
                "/src/.venv/bin:/root/.trunk/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
            )
            .with_exec(["bash", "-c", TRUNK_CMD])
        )
        await ctr.sync()
        results.append(JobResult("trunk", ok=True))
    except dagger.ExecError as exc:
        log = _dump_exec_error("trunk", exc)
        results.append(
            JobResult("trunk", ok=False, detail=f"exit {exc.exit_code} (log: {log})"),
        )
    except Exception as exc:  # noqa: BLE001
        results.append(JobResult("trunk", ok=False, detail=str(exc)))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=["unit", "integration", "trunk"],
        help="Run only one job.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=["unit", "integration", "trunk"],
        default=[],
        help="Skip a job (repeatable).",
    )
    args = parser.parse_args()

    jobs = {"unit", "integration", "trunk"}
    if args.only:
        jobs = {args.only}
    jobs -= set(args.skip)

    results: list[JobResult] = []

    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        uv_cache = dag.cache_volume("uv-cache")
        apt_cache = dag.cache_volume("apt-cache")
        trunk_launcher_cache = dag.cache_volume("trunk-launcher")
        trunk_user_cache = dag.cache_volume("trunk-user-cache")

        async with anyio.create_task_group() as tg:
            if "unit" in jobs:
                tg.start_soon(run_unit, results)
            if "integration" in jobs:
                tg.start_soon(run_integration, results)
            if "trunk" in jobs:
                tg.start_soon(
                    run_trunk,
                    results,
                    apt_cache,
                    uv_cache,
                    trunk_launcher_cache,
                    trunk_user_cache,
                )

    print("\n=== CI summary ===")
    for r in results:
        status = "OK  " if r.ok else "FAIL"
        suffix = f" — {r.detail}" if r.detail else ""
        print(f"  [{status}] {r.name}{suffix}")

    if any(not r.ok for r in results):
        sys.exit(1)


if __name__ == "__main__":
    anyio.run(main)
