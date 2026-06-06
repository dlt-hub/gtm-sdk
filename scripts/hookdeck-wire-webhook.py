#!/usr/bin/env -S uv run python
"""Create (upsert) the Hookdeck Source → Destination → Connection for a deployed
webhook, pointing the Destination at the app's Modal URL.

The repo's other Hookdeck tooling is read-only (`gtm webhook sync` discovers
existing wiring) or event-dump only; this fills the gap of *creating* the wiring
from the CLI instead of the Hookdeck dashboard.

It resolves the Modal app name + URL the same way `gtm webhook sync` does
(`cli.webhook.sync.app_name_for` + `cli.webhook._modal.modal_url_for_app`), then
issues a single idempotent `PUT /connections` to the Hookdeck API. The PUT
upserts the Source and Destination by name, so re-running is safe.

Auth: HOOKDECK_API_KEY in the environment — inject via Infisical. Run under
`infisical run` so the key (and MODAL_WORKSPACE, if set) are present:

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/hookdeck-wire-webhook.py slack CaldotcomBookingWebhook

Examples:
    # cal.com → Slack, reusing an existing Hookdeck source named "caldotcom":
    scripts/hookdeck-wire-webhook.py slack CaldotcomBookingWebhook \\
        --source-name caldotcom

    # print the request without sending it:
    scripts/hookdeck-wire-webhook.py slack CaldotcomBookingWebhook --dry-run

Fan-out: pass `--source-name` matching an EXISTING Hookdeck source (e.g. the one
already feeding the Attio connection) so the same cal.com webhook fans out to
both destinations. A new name creates a new source (with its own ingest URL you
must point cal.com at). After wiring, run `gtm webhook sync` to refresh the
registry.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

import requests

# scripts/ is intentionally excluded from the installed packages, so make the
# repo-local cli/* and scripts.lib imports resolvable when run via `uv run`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.webhook._hookdeck import HOOKDECK_API_BASE  # noqa: E402
from cli.webhook._modal import (  # noqa: E402
    list_deployed_app_names,
    modal_url_for_app,
)
from cli.webhook.sync import SOURCES, app_name_for  # noqa: E402
from scripts.lib.env import infisical_run_example  # noqa: E402

# Mirror scripts/webhooks-redeploy.py's handler aliases.
_HANDLER_ALIASES: dict[str, str] = {
    "attio": "export_to_attio",
    "etl": "export_to_gcp_etl",
    "raw": "export_to_gcp_raw",
    "slack": "export_to_slack",
}


def _fail(msg: str) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_model(source_alias: str) -> type:
    """Map a source display alias (e.g. CaldotcomBookingWebhook) to its class."""
    for _slug, model, display in SOURCES:
        if display == source_alias:
            return model
    valid = ", ".join(display for _s, _m, display in SOURCES)
    _fail(f"Unknown source '{source_alias}'. Known sources: {valid}.")


def _source_slug(source_alias: str) -> str:
    for slug, _model, display in SOURCES:
        if display == source_alias:
            return slug
    return source_alias.lower()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create/upsert the Hookdeck connection for a deployed webhook.",
    )
    parser.add_argument("handler", help="handler name or alias (e.g. slack)")
    parser.add_argument(
        "source",
        help="source class alias (e.g. CaldotcomBookingWebhook)",
    )
    parser.add_argument(
        "--source-name",
        default=None,
        help="Hookdeck source name to route from (default: the source slug). "
        "Pass an EXISTING source's name to fan out from it.",
    )
    parser.add_argument(
        "--connection-name",
        default=None,
        help="Hookdeck connection name (default: the Modal app name).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request body and exit without calling Hookdeck.",
    )
    args = parser.parse_args()

    raw_handler = str(args.handler)
    source_alias = str(args.source)
    handler = _HANDLER_ALIASES.get(raw_handler, raw_handler)
    model = _resolve_model(source_alias)

    app_name = app_name_for(handler, model)
    if app_name is None:
        _fail(
            f"Source {args.source} does not expose the app-name method for "
            f"handler '{handler}' — nothing to wire.",
        )
    modal_url = modal_url_for_app(app_name)

    source_name = args.source_name or _source_slug(args.source)
    connection_name = args.connection_name or app_name
    destination_name = app_name

    body = {
        "name": connection_name,
        "source": {"name": source_name},
        "destination": {"name": destination_name, "config": {"url": modal_url}},
    }

    print(f"Handler:        {handler}")
    print(f"Source class:   {args.source}")
    print(f"Modal app:      {app_name}")
    print(f"Modal URL:      {modal_url}")
    print(f"Hookdeck source: {source_name}")
    print(f"Connection:     {connection_name}")
    print()

    if args.dry_run:
        import json

        print(f"DRY RUN — would PUT to {HOOKDECK_API_BASE}/connections:")
        print(json.dumps(body, indent=2))
        return 0

    # Warn (don't block) if the Modal app isn't deployed yet — the connection
    # can be created ahead of deploy, but events will 5xx until the app exists.
    try:
        if app_name not in list_deployed_app_names():
            print(
                f"WARNING: Modal app '{app_name}' is not currently deployed. "
                "Deploy it (scripts/webhooks-redeploy.py) so the destination "
                "URL resolves.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — deploy-state check is best-effort
        print(f"(skipping deploy-state check: {exc})", file=sys.stderr)

    api_key = os.environ.get("HOOKDECK_API_KEY", "").strip()
    if not api_key:
        _fail(
            "HOOKDECK_API_KEY is not set. Add it to Infisical and run under:\n  "
            + infisical_run_example(
                f"scripts/hookdeck-wire-webhook.py {args.handler} {args.source}",
                env_placeholder="dev",
            ),
        )

    # Idempotent: PUT upserts source/destination/connection by name.
    resp = requests.put(
        f"{HOOKDECK_API_BASE}/connections",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    if resp.status_code >= 400:
        _fail(f"Hookdeck API {resp.status_code}: {resp.text}")
    data = resp.json()

    conn_id = data.get("id")
    src = data.get("source") or {}
    dest = data.get("destination") or {}
    print("Connection wired:")
    print(f"  connection_id:  {conn_id}")
    print(f"  source_id:      {src.get('id')}")
    print(f"  destination_id: {dest.get('id')}")
    ingest_url = src.get("url")
    if ingest_url:
        print()
        print(f"  Source ingest URL (point cal.com's webhook here): {ingest_url}")
    print()
    print("Next: run `gtm webhook sync` to refresh webhooks/registry.yaml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
