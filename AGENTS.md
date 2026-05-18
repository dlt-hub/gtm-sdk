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
- New secret = add a `modal.Secret.from_name("<name>")` binding in `src/app.py`.
- Free tier caps the app at **8 web endpoints**. Don't silently exceed it.
- App name resolves from the `MODAL_APP` env var (`src/modal_app.py`).

## Webhook deploys

`webhooks/export_to_attio.py` and `webhooks/export_to_gcp_etl.py` ship one Modal app per webhook source, but each file uses a `WebhookModelToReplace` placeholder so the working tree stays source-agnostic. **`modal deploy` on the file as-is fails with `NameError: WebhookModelToReplace is not defined`.**

Use `scripts/deploy-webhook.sh <handler> <source>` (or `<handler> --all`) to substitute the placeholder, deploy, and restore in one step. The script encodes every footgun in the "Scripted deploy pitfalls" section below.

```shell
set -a && source .env.local && set +a   # once per shell
scripts/deploy-webhook.sh export_to_attio CaldotcomBookingWebhook
scripts/deploy-webhook.sh export_to_gcp_etl --all
```

Each source is a separate Modal app, so deploying one source does not redeploy the others — bump them individually after shared-code changes (e.g. `libs/dlt/`) or stale containers will keep importing removed symbols. Do not commit the substituted form; the script's `trap` restores the placeholder even if `modal deploy` fails or the script is interrupted.

### Scripted deploy pitfalls

The pitfalls below explain why `scripts/deploy-webhook.sh` is shaped the way it is. The script encodes the answer to each one; keep them here as design rationale for anyone touching the script:

- **`unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET` before `infisical run`.** Otherwise the parent shell's personal Modal tokens win over the dlthub-workspace tokens Infisical injects, and deploys land in the wrong workspace.
- **Wrap with `uv run modal deploy`, not bare `modal deploy`.** Bare `modal` runs outside the project venv and can't import `src.*` packages registered in `pyproject.toml` → `ModuleNotFoundError: No module named 'src.fathom'`.
- **Use `\cp -f` to bypass `cp -i` aliases.** A `cp` alias to interactive mode will silently answer "no" to the placeholder-restore step, leaving the previous iteration's substitution in place and deploying the wrong source on the next pass.
- **Do not store `infisical run --token … --` in a shell variable** and then expand it inline (`$INF modal deploy …`). Zsh treats the whole variable as `argv[0]` (`command not found: infisical run …`) and the service token leaks to stderr and shell history. Use a function or a bash array.
- **Preflight Modal secrets and GCS buckets before the loop.** A missing `modal.Secret.from_name(...)` aborts after the image build; a missing GCS bucket aborts at first write. Check with `infisical run … -- uv run modal secret list` and `gcloud storage ls --project=dlthub-sandbox`.
- **Wrap the loop in a `trap … EXIT` that restores only the current handler file (and removes its `.bak` sidecar)** so the working tree restores even if the loop dies mid-way. Do *not* glob `tmp/webhook-deploy-bak/*.py webhooks/`: a stale backup from a previous run for a different handler would clobber an unrelated `webhooks/` file, and a `sed -i.bak` sidecar left behind by a signal between the `sed` and the `rm -f *.bak` would survive the restore and leave `webhooks/` dirty. `scripts/deploy-webhook.sh` scopes the restore to `${HANDLER}` and deletes the sidecar inside the same trap.
- **Serialize concurrent invocations of the deploy helper.** Two terminals can both pass the clean-tree preflight and then race on the same handler file and shared `tmp/webhook-deploy-bak/` state — one process can delete the other's restore source, or one deploy can pick up the other's substitution. `scripts/deploy-webhook.sh` uses an atomic `mkdir tmp/webhook-deploy.lock` as a portable advisory lock and releases it from the EXIT trap.

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

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
