"""Dagger pipeline: run integration pytest in a container and export the JUnit report.

Invoked the same way locally and in CI:

    dagger run python .github/workflows/ci/pytest_integration_dagger.py

The pipeline runs the integration test marker inside a python:3.13 container, then
exports `junit.xml` to the host so a follow-up step (e.g. trunk-io/analytics-uploader)
can upload it.

The pipeline *fails* (non-zero exit) when pytest exits non-zero — a real test
failure OR the collection-time preflight in tests/integration/conftest.py
(missing required Attio object) — while still exporting the report. This is the
whole point of ai-eun: a previous `... || true` swallowed pytest's exit code, so
the job went green even when zero tests ran. We instead capture pytest's exit
code into `/src/pytest_rc` (the trailing `echo` keeps the `with_exec` itself
green so the report stays exportable) and re-raise it after the export.

The integration suite reads its credentials straight from the process environment
(see `INTEGRATION_SECRET_ENV_VARS`); there is no in-container Infisical CLI bootstrap.
Each required value must be present in the host environment — in CI from individual
`secrets.*` GitHub Actions secrets (synced into the repo by Infisical's GitHub App
integration), locally from `infisical run -- …`. They are forwarded into the container
as Dagger secrets, never baked into an image layer.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

import anyio
import dagger
from dagger import dag

# Credentials the integration suite reads at runtime. Audited from tests/conftest.py
# (ATTIO_API_KEY) and tests/integration/test_gtm_remote_smoke.py (the MODAL_* +
# PARALLEL_API_KEY set). MODAL_ENVIRONMENT/MODAL_APP are intentionally absent: the
# Modal client resolves the environment from the token's default workspace, and
# MODAL_APP defaults to "elvis-ai-v2" in src/modal_app.py.
INTEGRATION_SECRET_ENV_VARS = (
    "ATTIO_API_KEY",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "PARALLEL_API_KEY",
)

# The trailing `echo $? > /src/pytest_rc` always exits 0, so the `with_exec`
# succeeds and `junit.xml` is guaranteed exportable; main() reads pytest_rc back
# and re-raises the real code. Do NOT restore a `|| true` here (see ai-eun).
PYTEST_CMD = (
    "uv run pytest -m integration --junit-xml=junit.xml -o junit_family=xunit1; "
    "echo $? > /src/pytest_rc"
)
JUNIT_HOST_PATH = "junit.xml"
PYTEST_RC_PATH = "/src/pytest_rc"

# Distinct exit code the conftest preflight uses when a required Attio object is
# missing ("infra not ready"), so a green-checkmark-masking 0-test run is RED but
# still distinguishable from a genuine regression. Keep in sync with
# PREFLIGHT_MISSING_OBJECT_RC in tests/integration/conftest.py.
PREFLIGHT_MISSING_OBJECT_RC = 86


SOURCE_EXCLUDES = [
    ".venv",
    "tmp",
    ".pytest_cache",
    ".ruff_cache",
    "gtm.egg-info",
    "out",
    "data",
    "worktrees",
]


def build_container(secret_env: Mapping[str, str]) -> dagger.Container:
    """Build the integration pytest container. Caller must be inside `dagger.connection(...)`.

    `secret_env` maps env-var names to their resolved values (typically the
    `INTEGRATION_SECRET_ENV_VARS`). Each is forwarded into the container as a Dagger
    secret so it lands as an env var the test suite reads, without leaking into an
    image layer or the build log.
    """
    source = dag.host().directory(".", exclude=SOURCE_EXCLUDES)
    uv_cache = dag.cache_volume("uv-cache")

    ctr = (
        dag.container()
        .from_("python:3.13")
        .with_exec(
            ["bash", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
        )
        .with_env_variable("PATH", "/root/.local/bin:/usr/local/bin:/usr/bin:/bin")
        .with_mounted_cache("/root/.cache/uv", uv_cache)
    )

    for name, value in secret_env.items():
        secret = dag.set_secret(name.lower().replace("_", "-"), value)
        ctr = ctr.with_secret_variable(name, secret)

    return (
        ctr.with_directory("/src", source)
        .with_workdir("/src")
        .with_exec(["uv", "sync", "--all-extras", "--dev"])
        .with_exec(["bash", "-c", PYTEST_CMD])
    )


async def main() -> None:
    secret_env = {
        name: os.environ[name]
        for name in INTEGRATION_SECRET_ENV_VARS
        if os.environ.get(name)
    }
    missing = [name for name in INTEGRATION_SECRET_ENV_VARS if not os.environ.get(name)]
    if missing:
        # Fail loudly: a missing/incomplete secret sync would otherwise let every
        # integration test silently skip and the run go green.
        sys.stderr.write(
            "Missing required integration secrets in the environment: "
            f"{', '.join(missing)}\n",
        )
        sys.exit(1)

    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        ctr = build_container(secret_env)

        # Read pytest's real exit code (captured in PYTEST_CMD) first so we know
        # whether a missing report is an expected consequence of a crashed run or
        # a genuine problem. Any failure to read or parse it (missing file from a
        # killed/cancelled container, empty value, non-integer) fails closed at
        # rc=1 so the job goes red with a controlled message, not a traceback.
        try:
            rc = int((await ctr.file(PYTEST_RC_PATH).contents()).strip())
        except (dagger.DaggerError, ValueError) as exc:
            sys.stderr.write(
                f"warning: could not read pytest exit code from {PYTEST_RC_PATH} "
                f"({exc}); failing closed at rc=1\n",
            )
            rc = 1

        # Export the report so it reaches the host (and Trunk). A passing run
        # MUST produce one, so an export failure there is fatal (re-raise → red).
        # When pytest already failed, a missing junit.xml is an expected side
        # effect of the crash — warn and keep the real rc rather than masking it.
        try:
            await ctr.file("/src/junit.xml").export(JUNIT_HOST_PATH)
            print(f"exported junit report to {JUNIT_HOST_PATH}")
        except dagger.DaggerError as exc:
            if rc == 0:
                raise
            sys.stderr.write(
                f"warning: could not export {JUNIT_HOST_PATH} "
                f"(pytest already exited {rc}): {exc}\n",
            )

    if rc == PREFLIGHT_MISSING_OBJECT_RC:
        sys.stderr.write(
            "integration preflight: required Attio object missing — "
            "see the ::error:: annotation above (ai-eun / ai-0ou)\n",
        )
    elif rc != 0:
        sys.stderr.write(f"integration pytest exited {rc}\n")
    sys.exit(rc)


if __name__ == "__main__":
    anyio.run(main)
