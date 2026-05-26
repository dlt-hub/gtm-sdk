# Webhooks

Standalone Modal apps. Each `export_to_*.py` deploys one Modal app per source
via the `WebhookModelToReplace` placeholder pattern — see
[Deploying](#deploying) below.

## Structured logs

Each handler emits one JSON line per event via `libs/logging/structured.py`.
Modal captures stdout into its dashboard, so the lines are filterable by
`source` (per-app) and joinable per request via `request_id`. The logger is
always on — no env-var gate — and it never raises.

### Standard fields (always present)

| Field        | Source                                              |
| ------------ | --------------------------------------------------- |
| `ts`         | `datetime.now(UTC).isoformat()` at call time        |
| `event`      | First positional argument to `log(...)`             |
| `source`     | `set_source(APP_NAME)` / `set_source(BUCKET_NAME)`  |
| `request_id` | `X-Request-Id` inbound header; uuid7 fallback       |

### Event schema

| Event                       | Extra fields                                                          | Emitted from                                            |
| --------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------- |
| `webhook.received`          | `payload_bytes`                                                       | Handler entry, after the FastAPI body parses            |
| `webhook.validated`         | `op_count` (Attio) / `bucket_name` (GCS ETL)                          | After `*_is_valid_webhook()` returns true               |
| `webhook.validation_failed` | `reason`                                                              | After `*_is_valid_webhook()` returns false              |
| `webhook.completed`         | `duration_ms`, `status` (`ok`/`error`), `error_type?`, `error_msg?`   | Handler exit, in `finally`-style branch                 |
| `webhook.error`             | `reason` (`validation_error`/`processing_error`), `path?`, `file?`    | File-iteration paths in `webhooks/export_to_gcp_raw.py` |

### Following a single request

```text
1. In Modal dashboard, filter logs by `"source":"rb2b"`.
2. Pick any `request_id` in the result set.
3. Filter additionally by that `request_id` — you get the full
   received → validated → completed trace for that single delivery.
```

Implementation: [`libs/logging/structured.py`](../libs/logging/structured.py).

## Files

- `export_to_attio.py` — per-source app, writes to Attio.
- `export_to_gcp_etl.py` — per-source app, writes the transformed payload to a
  per-source GCS bucket.
- `export_to_gcp_raw.py` — per-source app, writes the raw payload to a
  per-source GCS bucket (`WebhookModel.raw_get_bucket_name()`).
- `registry.yaml` — **gitignored**. Generated locally; see below.

## Deploying

Use `scripts/redeploy_webhook.py` — never `modal deploy webhooks/<file>.py`
directly. Handler files live in source control with a `WebhookModelToReplace`
placeholder so the working tree stays source-agnostic; the script substitutes
the placeholder, deploys via a Dagger-wrapped `modal deploy`, and restores
the file in one safe step.

```shell
set -a && source .env.local && set +a   # once per shell
export INFISICAL_ENV=dev                 # explicit; no default

# Deploy one source to one handler.
scripts/redeploy_webhook.py export_to_attio   CaldotcomBookingWebhook
scripts/redeploy_webhook.py export_to_gcp_etl Rb2bVisitWebhook

# Deploy every source imported by the handler (one Modal app per source).
scripts/redeploy_webhook.py export_to_attio   --all
scripts/redeploy_webhook.py export_to_gcp_etl --all
scripts/redeploy_webhook.py export_to_gcp_raw --all
```

The deploy itself runs inside a Dagger container (`uv sync --frozen && uv run
modal deploy`) so the env that ships images to Modal is reproducible
operator-to-operator. Set `DAGGER_DRY_RUN=1` to skip Dagger and invoke
`infisical run -- uv run modal deploy` directly on the host (used by the CI
smoke test).

Valid `<handler>` and `<source>` values are discovered at runtime from
`webhooks/*.py` and that handler's `Webhook as <Alias>` imports — there is no
list inside the script to maintain.

### Why a script, not `modal deploy`

Running `modal deploy webhooks/<file>.py` directly on the working tree fails
with `NameError: WebhookModelToReplace is not defined`. Each handler ships one
Modal app *per source*; the placeholder is what keeps the file deployable to N
different sources from a single source-controlled definition. Deploying one
source does not redeploy the others — after a shared-code change (e.g. inside
`libs/dlt/`), bump each source individually or stale containers keep importing
removed symbols.

### Pitfalls the script handles for you

These are the failure modes baked into the script. If you ever bypass it,
these are what bite:

- **Env clobber.** Your shell's personal `MODAL_TOKEN_ID` /
  `MODAL_TOKEN_SECRET` silently win over the Infisical-injected dlthub
  workspace tokens, and deploys land in the wrong workspace. The script
  `os.environ.pop`s both before invoking `infisical run`.
- **`cp -i` alias.** A bash-only footgun the Python rewrite sidesteps by
  using `shutil.copyfile` (which always overwrites) for restore — no
  shell-alias resolution involved.
- **`infisical run` argument-string expansion.** Bash-only zsh issue where
  storing `infisical run --token … --` in a variable made the whole string
  `argv[0]`. The Python rewrite uses list-arg subprocess calls everywhere
  with `shell=False`, so the gotcha is structurally impossible.

Additional mitigations (concurrent-invocation lock, atexit-scoped restore,
Modal-secret/Infisical-key/GCS-bucket preflight, signal-routed cleanup) are
catalogued in `CLAUDE.md` → **Scripted deploy pitfalls**. The CI smoke test
at `tests/scripts/test_deploy_webhook.py` covers the substitute/restore
loop, the cleanup-on-deploy-failure path, and the `MODAL_TOKEN_ID` isolation
rule; the lock and full preflight paths are not yet exercised in CI.

## Registry

`registry.yaml` is the inventory of `(source, handler) → (Modal app, Modal URL,
Hookdeck source / destination / connection IDs)`. It's generated from live
Modal + Hookdeck state; never edit by hand and never commit.

```shell
# Regenerate the registry. Modal tokens come from Infisical; HOOKDECK_API_KEY
# is sourced from the parent ai/gtm-sdk/.env.local because Infisical's value
# for it is empty (and an empty Infisical value would overwrite the host env
# var — surfacing as silently-null hookdeck_*_id fields in the registry).
unset MODAL_TOKEN_ID MODAL_TOKEN_SECRET  # avoid personal-shell tokens winning
set -a && source /Users/elvis/Documents/ai/gtm-sdk/.env.local && set +a
infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \
  --env=dev -- env HOOKDECK_API_KEY="$HOOKDECK_API_KEY" \
  uv run python -m cli.main webhook sync

# Inspect what's there.
uv run python -m cli.main webhook list
```

Optional: set `MODAL_WORKSPACE` to override the Modal subdomain prefix
(default `devx`). Only needed if you redeploy these apps into a different
Modal workspace.

## Rotation procedure

When you redeploy a Modal app, its name (and therefore its URL) stays the same.
App names are deterministic per handler family: `export_to_attio` uses
`WebhookModel.attio_get_app_name()` directly, while the GCP handlers
(`export_to_gcp_etl`, `export_to_gcp_raw`) derive their app name from
`CloudGoogle.clean_bucket_name(bucket_name=WebhookModel.<prefix>_get_bucket_name())`.
Either way, redeploys are name-stable and the registry usually only needs
a refresh:

1. Redeploy via `scripts/redeploy_webhook.py` (see [Deploying](#deploying)).
2. Run `gtm webhook sync` to refresh `generated_at`.

If you change the wiring inside Hookdeck (rerouting a source to a different
Modal destination), re-run `sync` after applying the change in the Hookdeck
dashboard so the IDs in the registry match.

## Why this isn't committed

`gtm-sdk` is a public OSS repo. The registry contains personal Modal URLs and
Hookdeck account IDs that don't belong on GitHub — `.gitignore` keeps them out.
The schema (Pydantic models in `cli/webhook/registry.py`) is the public
contract; the data is local.
