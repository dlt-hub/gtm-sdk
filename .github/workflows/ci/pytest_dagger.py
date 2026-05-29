"""Dagger pipeline: run pytest in a container and export the JUnit report.

Invoked the same way locally and in CI:

    dagger run python .github/workflows/ci/pytest_dagger.py

The pipeline runs `uv run pytest --junit-xml=junit.xml -o junit_family=xunit1`
inside a python:3.13 container and exports `junit.xml` to the host so a
follow-up step (e.g. trunk-io/analytics-uploader) can upload it.

The pipeline *fails* (non-zero exit) when pytest exits non-zero, while still
exporting the report. A previous `... || true` swallowed pytest's exit code so
the job went green even on a failing suite (ai-eun); we instead capture pytest's
exit code into `/src/pytest_rc` (the trailing `echo` keeps the `with_exec` green
so the report stays exportable) and re-raise it after the export.
"""

from __future__ import annotations

import sys

import anyio
import dagger
from dagger import dag

# The trailing `echo $? > /src/pytest_rc` always exits 0, so the `with_exec`
# succeeds and `junit.xml` is guaranteed exportable; main() reads pytest_rc back
# and re-raises the real code. Do NOT restore a `|| true` here (see ai-eun).
PYTEST_CMD = (
    "uv run pytest --junit-xml=junit.xml -o junit_family=xunit1; "
    "echo $? > /src/pytest_rc"
)
JUNIT_HOST_PATH = "junit.xml"
PYTEST_RC_PATH = "/src/pytest_rc"

# Tests in tests/scripts/test_deploy_webhook.py shell out to `git status` and
# scripts/webhooks-redeploy.py itself runs `git rev-parse --show-toplevel`. When
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


def build_container() -> dagger.Container:
    """Build the pytest container. Caller must be inside `dagger.connection(...)`."""
    source = dag.host().directory(".", exclude=SOURCE_EXCLUDES)
    uv_cache = dag.cache_volume("uv-cache")

    return (
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


async def main() -> None:
    async with dagger.connection(config=dagger.Config(log_output=sys.stderr)):
        ctr = build_container()

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

    if rc != 0:
        sys.stderr.write(f"pytest exited {rc}\n")
    sys.exit(rc)


if __name__ == "__main__":
    anyio.run(main)
