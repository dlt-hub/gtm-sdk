"""Dagger pipeline: run pytest in a container and export the JUnit report.

Invoked the same way locally and in CI:

    dagger run python .github/workflows/ci/pytest_dagger.py

The pipeline runs `uv run pytest --junit-xml=junit.xml -o junit_family=xunit1`
inside a python:3.13 container and exports `junit.xml` to the host so a
follow-up step (e.g. trunk-io/analytics-uploader) can upload it. Test failures
do not abort the pipeline so the report always reaches the host.
"""

from __future__ import annotations

import sys

import anyio
import dagger
from dagger import dag

PYTEST_CMD = "uv run pytest --junit-xml=junit.xml -o junit_family=xunit1 || true"
JUNIT_HOST_PATH = "junit.xml"

# Tests in tests/scripts/test_deploy_webhook.py shell out to `git status` and
# scripts/redeploy-webhook.sh itself runs `git rev-parse --show-toplevel`. When
# Dagger copies the host source into /src from a git worktree, the worktree's
# `.git` is a gitlink file pointing at a path on the host that does not exist
# in the container, so every git invocation fails with "fatal: not a git
# repository". Initializing a throwaway repo at /src — with everything staged
# and committed — gives the script and its tests a valid HEAD to diff against
# without leaking the host's git state.
GIT_INIT_CMD = (
    "git init -q && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false add -A && "
    "git -c user.email=ci@example.com -c user.name=ci "
    "  -c commit.gpgsign=false commit -q -m 'dagger throwaway' --no-verify"
)


async def main() -> None:
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        source = dag.host().directory(
            ".",
            exclude=[
                ".venv",
                "tmp",
                ".pytest_cache",
                ".ruff_cache",
                "gtm.egg-info",
                "out",
                "data",
                "worktrees",
            ],
        )

        uv_cache = dag.cache_volume("uv-cache")

        ctr = (
            dag.container()
            .from_("python:3.13")
            .with_exec(
                ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            )
            .with_env_variable("PATH", "/root/.local/bin:/usr/local/bin:/usr/bin:/bin")
            .with_mounted_cache("/root/.cache/uv", uv_cache)
            .with_directory("/src", source)
            .with_workdir("/src")
            .with_exec(["bash", "-c", f"rm -rf .git && {GIT_INIT_CMD}"])
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_exec(["bash", "-c", PYTEST_CMD])
        )

        await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)
        print(f"exported junit report to {JUNIT_HOST_PATH}")


if __name__ == "__main__":
    anyio.run(main)
