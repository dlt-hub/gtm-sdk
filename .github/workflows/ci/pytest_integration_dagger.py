"""Dagger pipeline: run integration pytest in a container and export the JUnit report.

Invoked the same way locally and in CI:

    dagger run python .github/workflows/ci/pytest_integration_dagger.py

The pipeline runs the integration test marker inside a python:3.13 container with
secrets injected via the Infisical CLI, then exports `junit.xml` to the host so a
follow-up step (e.g. trunk-io/analytics-uploader) can upload it. Test failures do
not abort the pipeline so the report always reaches the host.

Requires `INFISICAL_TOKEN` and `INFISICAL_PROJECT_ID` to be set in the host
environment; they are forwarded into the container as Dagger secrets.
"""

from __future__ import annotations

import os
import sys

import anyio
import dagger
from dagger import dag

PYTEST_CMD = (
    'infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev -- '
    "uv run pytest -m integration --junit-xml=junit.xml -o junit_family=xunit1 || true"
)
JUNIT_HOST_PATH = "junit.xml"

INFISICAL_INSTALL = (
    "curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | bash && "
    "apt-get update && apt-get install -y infisical"
)


async def main() -> None:
    token = os.environ.get("INFISICAL_TOKEN")
    project_id = os.environ.get("INFISICAL_PROJECT_ID")
    if not token or not project_id:
        sys.stderr.write(
            "INFISICAL_TOKEN and INFISICAL_PROJECT_ID must be set in the environment\n",
        )
        sys.exit(1)

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
        apt_cache = dag.cache_volume("apt-cache")

        infisical_token = dag.set_secret("infisical-token", token)
        infisical_project_id = dag.set_secret("infisical-project-id", project_id)

        ctr = (
            dag.container()
            .from_("python:3.13")
            .with_mounted_cache("/var/cache/apt", apt_cache)
            .with_exec(["bash", "-c", INFISICAL_INSTALL])
            .with_exec(
                ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            )
            .with_env_variable("PATH", "/root/.local/bin:/usr/local/bin:/usr/bin:/bin")
            .with_mounted_cache("/root/.cache/uv", uv_cache)
            .with_secret_variable("INFISICAL_TOKEN", infisical_token)
            .with_secret_variable("INFISICAL_PROJECT_ID", infisical_project_id)
            .with_directory("/src", source)
            .with_workdir("/src")
            .with_exec(["uv", "sync", "--all-extras", "--dev"])
            .with_exec(["bash", "-c", PYTEST_CMD])
        )

        await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)
        print(f"exported junit report to {JUNIT_HOST_PATH}")


if __name__ == "__main__":
    anyio.run(main)
