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
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_exec(["bash", "-c", PYTEST_CMD])
        )

        await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)
        print(f"exported junit report to {JUNIT_HOST_PATH}")


if __name__ == "__main__":
    anyio.run(main)
