# AGENTS.md

Rules for working in this repo. `CLAUDE.md` and `WARP.md` symlink here. The repo layout, CLI surface, and adapter list are discoverable — don't expect this file to mirror them.

## Code placement

- `libs/<service>/` — wrap **one** external SDK/API. Idiomatic Python types/functions only.
- `src/` — orchestration. Multi-step flows, side effects, Modal `@app.function` / `@modal.fastapi_endpoint` decorators.
- `cli/` — Typer subapps. Parse → preflight → call `src/` → render. **No business logic.**
- `data-gen/` — independent, composable data products.
- `webhooks/` — standalone Modal apps. Deploy individually with `modal deploy webhooks/<file>.py`. Do **not** register them in `src/app.py`.
- `api/specs/`, `api/samples/` — external API specs and fixture payloads. Read-only reference.
- `tmp/` — scratch only. Gitignored. Never write temp files anywhere else.

### Hard rules

- **No cross-lib imports.** `libs/<x>` must not import from `libs/<y>`. If two adapters need to coordinate, do it in `src/`.
- **No orchestration in `libs/`.** Adapter modules must be callable in isolation.
- **New top-level package?** Update `[tool.setuptools.packages.find]` in `pyproject.toml` (currently `cli*`, `libs*`, `src*`).

## Modal gotchas

- `deploy.py` stays at the repo root. Moving it under `src/` causes `src/attio/` to shadow the `attio` pip package.
- New endpoint = add the module import to `_ENDPOINT_MODULES` in `src/app.py`, otherwise its decorators don't register.
- New secret = add `"<X>_API_KEY": <x>_client.api_key_scope` to `KEY_SCOPES` in `src/secrets_bootstrap.py` (after wiring an `api_key_scope` contextvar in `libs/<x>/client.py`), then decorate the function with `@with_secrets("<X>_API_KEY")` and bind `secrets=[bootstrap_secret()]`. Do NOT use `modal.Secret.from_name(...)` — see ai-672.
- Free tier caps the app at **8 web endpoints**. Don't silently exceed it.
- App name resolves from the `MODAL_APP` env var (`src/modal_app.py`).

## Webhook deploys

`webhooks/export_to_attio.py`, `webhooks/export_to_gcp_etl.py`, and `webhooks/export_to_gcp_raw.py` ship one Modal app per webhook source, but each file uses a `WebhookModelToReplace` placeholder so the working tree stays source-agnostic. **`modal deploy` on the file as-is fails with `NameError: WebhookModelToReplace is not defined`.**

Use `scripts/webhooks-redeploy.py <handler> <source>` (or `<handler> --all`) to substitute the placeholder, deploy, and restore in one step. The script auto-discovers valid handlers (any `webhooks/*.py` containing the placeholder) and sources (the `Webhook as <Alias>` imports inside the handler), and preflights per-source GCS buckets when the handler routes to `gs://` (etl, raw). It encodes every footgun in the "Scripted deploy pitfalls" section below.

```shell
set -a && source .env.local && set +a   # once per shell
export INFISICAL_ENV=dev                 # explicit; no default
scripts/webhooks-redeploy.py export_to_attio CaldotcomBookingWebhook
scripts/webhooks-redeploy.py export_to_gcp_etl --all
scripts/webhooks-redeploy.py export_to_gcp_raw --all
```

The `modal deploy` step runs inside a Dagger container (matching the
`scripts/hookdeck-dump-connection-events.py` pattern) so the env that ships
images to Modal is reproducible across operators. Modal tokens flow into the
container as `dagger.set_secret(...)` values; the `infisical` CLI stays on the
host. Set `DAGGER_DRY_RUN=1` to skip Dagger and invoke `infisical run -- uv run modal deploy` directly on the host — used by `tests/scripts/test_deploy_webhook.py` so CI doesn't need a Dagger engine or real Modal credentials.

Each source is a separate Modal app, so deploying one source does not redeploy the others — bump them individually after shared-code changes (e.g. `libs/dlt/`) or stale containers will keep importing removed symbols. Do not commit the substituted form; an `atexit`/signal-driven cleanup restores the placeholder even if `modal deploy` fails or the script is interrupted (Ctrl-C, SIGTERM).

The contract every concrete `src/<source>/webhook/*.py` `Webhook` class must satisfy lives at `libs/webhook/protocol.py` as `WebhookModelProtocol` (a `typing.Protocol`), and `tests/libs/webhook/test_protocol_conformance.py` enforces it across all five sources. Each handler's `TYPE_CHECKING` block aliases `WebhookModelTypeCheckShim` (a concrete `BaseModel` stand-in defined alongside the Protocol) as `WebhookModelToReplace` so pyright sees the full surface — Pydantic methods (`model_rebuild`/`model_validate`) and the contract methods — in the unsubstituted source tree. New sources: extend `protocol.py` only if you add a contract method; otherwise just implement the existing surface on the new `Webhook` class and add a parametrize entry to the conformance test.

### Scripted deploy pitfalls

The pitfalls below explain why `scripts/webhooks-redeploy.py` is shaped the way it is. The first version was bash; the Python rewrite preserves every mitigation as an explicit module-level idiom. Keep them here as design rationale for anyone touching the script:

- **`os.environ.pop("MODAL_TOKEN_ID"/"MODAL_TOKEN_SECRET")` before any `infisical run`.** Otherwise the parent shell's personal Modal tokens win over the dlthub-workspace tokens Infisical injects, and deploys land in the wrong workspace. (Bash equivalent: `unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET`.)
- **Wrap with `uv run modal deploy`, not bare `modal deploy`.** Bare `modal` runs outside the project venv and can't import `src.*` packages registered in `pyproject.toml` → `ModuleNotFoundError: No module named 'src.fathom'`. Applies inside the Dagger container and on the `DAGGER_DRY_RUN=1` host path.
- **Use `shutil.copyfile` (always overwrites) for restore.** The bash version needed `\cp -f` to dodge `cp -i` aliases that would silently refuse the restore; Python's `shutil.copyfile` has no equivalent shadowing risk. A refactor that swaps it for a helper accepting `exist_ok=False` would resurrect the original footgun.
- **Always invoke `infisical run` with a list-arg subprocess, never a string.** The Python subprocess API only accepts argv lists when `shell=False`, which sidesteps the bash gotcha where storing `infisical run --token … --` in a variable made zsh treat the whole thing as `argv[0]` and leaked the service token to stderr/shell history. Never set `shell=True`.
- **Preflight Modal secrets, Infisical keys, and GCS buckets before the deploy loop.** A missing `modal.Secret.from_name(...)` aborts after the image build; a missing Infisical key fails on the first Hookdeck event after deploy; a missing GCS bucket aborts at first write. The script calls `modal secret list --json` (via `infisical run`), `infisical secrets get` per key, and `gcloud storage ls --project=dlthub-sandbox` per bucket before touching the handler file.
- **`atexit`-registered cleanup, gated on `_BACKUP_FRESHLY_WRITTEN`, scoped to the current handler.** Restore the file even if the deploy raised, was Ctrl-C'd, or was SIGTERM'd. The gate prevents an early-failure exit from copying a stale backup from a prior run on top of a clean worktree. Signal handlers route SIGINT/SIGTERM through `sys.exit` so `atexit` fires (the default signal disposition would skip it).
- **Serialize concurrent invocations of the deploy helper.** Two terminals can both pass the clean-tree preflight and then race on the same handler file and shared `tmp/webhook-deploy-bak/` state — one process can delete the other's restore source, or one deploy can pick up the other's substitution. `scripts/webhooks-redeploy.py` uses an atomic `LOCK_DIR.mkdir(exist_ok=False)` as a portable advisory lock and releases it from the `atexit` cleanup.

### Registry

`gtm webhook sync` regenerates `webhooks/registry.yaml` (gitignored) by joining `modal app list` with the Hookdeck API. Run it after any deploy or Hookdeck wiring change. Use `gtm webhook list` to inspect the cached registry. The file is gitignored because it contains personal Modal URLs and Hookdeck IDs that don't belong in OSS — see `webhooks/README.md`.

## Telemetry

OTEL via `libs/telemetry.py`. Activated only when one of these is set: `HYPERDX_API_KEY`, `HYPERDX_OTLP_ENDPOINT`, or `OTEL_EXPORTER_OTLP_ENDPOINT`. Otherwise the tracer is a no-op — don't add fallback logging "just in case."

## Secrets (Infisical)

`.env.local` at the repo root holds `INFISICAL_TOKEN` and `INFISICAL_PROJECT_ID`. There is no `.infisical.json`, so the CLI does not auto-detect the project — pass flags explicitly or source the env file first:

```shell
set -a && source .env.local && set +a
infisical secrets --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" --env=dev -- <cmd>
```

Conductor workspaces get `.env.local` copied in at provisioning; the parent `ai/` repo's `.env*` files are not copied. Never fall back to 1Password unless the user explicitly asks.

## Package management

**Use `uv`. Never `pip`, `pip3`, or `python3 -m pip`.** Bare pip bypasses `uv.lock` and causes environment drift.

- `uv sync` — install from lock.
- `uv pip install <pkg>` — add a dep (updates lock).
- `uv run <cmd>` — run inside the env.

## Path anchoring

When a script reads/writes files that live beside it, anchor on `Path(__file__).resolve().parent`, not the CWD. `uv run path/to/script.py` does **not** chdir — relative paths resolve from wherever the user invoked the command, not the script's folder. This silently writes files to the wrong place.

```python
SCRIPT_DIR = Path(__file__).resolve().parent
(SCRIPT_DIR / "output.txt").write_text(...)
```

## Documentation

**Do not create summary, investigation, or "what I did" `.md` files.** Live documentation goes in code:

- Docstrings explain *why*, not *what*.
- Comments document decisions and gotchas inline.
- Significant changes → `CHANGELOG.md`.
- Architectural decisions → design artifacts in the parent `ai/` repo's `design/`, not loose `.md` here.

If you finish a task and want to summarize, output it as your final response. Don't write a file.

## Git

- **Branches**: `agent/<slug>`. Never `claude/*` or other provider-specific prefixes.
  - **Exception — Linear-initiated branches:** When an agent is kicked off from a Linear ticket, the branch is typically pre-created with a `feature/` prefix (e.g., `feature/eng-1234-add-email-validation`). Keep the existing branch name as-is — do not rename or override it to `agent/...`. Linear's ticket ↔ branch linkage depends on the original name. When picking up a handoff, check the current branch first (`git branch --show-current`); if it already starts with `feature/`, continue committing to it rather than creating a new `agent/` branch.
- **Worktrees**: `worktrees/<branch-name>` at the repo root. Ensure `worktrees/` exists and is gitignored. Never use `.git/modules/*` paths as user-facing worktree locations.
- **Commits/PRs**: never add AI co-author trailers (`Co-Authored-By: Claude/Oz/...`). Human authors only.

## Linting

All linters/formatters run via **trunk**, not as bare binaries. `yamllint`, `ruff`, `checkov`, `shellcheck`, `bandit`, `actionlint`, `prettier`, `mypy`, etc. live in trunk's sandbox — invoking them directly will `command not found` or use the wrong config. Reproduce a finding with `trunk check --filter=<tool> <path>`; format with `trunk fmt <path>`.

## Testing

`uv run pytest`. Importlib mode is already configured. Mirror the source layout when adding tests.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, walk the checklist below. Pushing is a shared-state action — the rules differ by branch.

**WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Commit** locally with a clear message
5. **Push policy (branch-aware):**
   - **Roborev gate (applies to ALL branches):** Never `git push` to origin without running `git roborev review --wait` against HEAD first and confirming a clean review. If roborev is unavailable or fails to run, say so and ask before pushing.
   - On `agent/*` or `feature/*` branches: after the roborev gate passes, you MAY `git pull --rebase && git push` without asking — these are scratch branches owned by the current task.
   - On `main`, `master`, or any release/protected branch: **DO NOT push without explicit user confirmation.** Stop after the commit, say what would be pushed, and ask. The user pushing themselves is the default.
   - If unsure which category the branch falls into, treat it as protected and ask.
6. **Clean up** - Clear stashes, prune remote branches (only after push is authorized)
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Committing locally is reversible; pushing to a shared branch is not. Match the action to the blast radius.
- NEVER force-push without an explicit ask, regardless of branch.
- If a push fails, surface the error and ask before resolving — do not retry in a loop or rewrite history.
<!-- END BEADS INTEGRATION -->
