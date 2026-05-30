#!/usr/bin/env -S uv run python
"""Backfill historical rb2b website-visit events into Attio.

rb2b has no API for retrieving historical visits — its API is identity /
enrichment only, and visits arrive solely via realtime webhooks. So the only
archives of past visits are:

1. GCS ``gs://dlthub-devx-rb2b-visits-raw`` — one verbatim payload per
   ``.jsonl`` object (written raw by ``webhooks/export_to_gcp_raw.py`` before
   model validation).
2. Hookdeck — archived/replayable events for the rb2b connection.

We backfill by **replaying** each historical event through the same deployed
Modal webhook that ingests live traffic (``export-to-attio-from-rb2b-visits``),
so replays pass through identical qualification + Attio mapping. We never write
Attio directly.

The two archives overlap in time, so we union + dedupe them with dlt
(``write_disposition="merge"`` on a content-derived ``dedup_key``). The dedup
key is ``libs.rb2b.compute_event_id`` applied to the visit's inner payload — the
*same* function the live webhook now uses to mint its ``event_id`` — so the
backfill, live ingestion, and any re-run all converge on one Attio
tracking-event ``external_id`` and never duplicate rows.

Three independently-runnable stages:

* ``extract`` — pull from both sources, dedupe via dlt, write
  ``out/rb2b_visits.jsonl``.
* (map) — ``map_record`` reshapes a JSONL row into the webhook envelope.
* ``send`` — POST each record one-by-one to the Modal webhook; idempotent,
  resumable, rate-limited.

The script is parameterized via :class:`BackfillConfig` so future "multi-source
raw → deduped JSONL → replay via webhook" backfills only swap the config.

Run via Infisical so GCP / Hookdeck / Modal credentials are injected:

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/rb2b-visits-backfill.py extract --limit 25
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/rb2b-visits-backfill.py send --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

# Anchor on the script's directory so output paths resolve regardless of the CWD
# `uv run` was invoked from, and make repo-local packages importable.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import dlt  # noqa: E402
import requests  # noqa: E402
from dlt.destinations import duckdb as duckdb_destination  # noqa: E402

from cli.webhook._hookdeck import HOOKDECK_API_BASE, PAGE_LIMIT  # noqa: E402
from cli.webhook._modal import modal_url_for_app  # noqa: E402
from libs.dlt.bucket_naming import raw_bucket_name  # noqa: E402
from libs.dlt.filesystem_gcp import GCPCredentials  # noqa: E402
from libs.rb2b import Webhook as Rb2bWebhook  # noqa: E402
from libs.rb2b import compute_event_id  # noqa: E402

# Reuse the model's timestamp normalizer so the synthesized envelope timestamp
# parses even when rb2b emits its documented `12:34:56:00.00+00.00` shape.
from libs.rb2b.models import normalize_rb2b_timestamp  # noqa: E402
from scripts.lib.env import infisical_run_example  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BackfillConfig:
    """Everything that makes a multi-source replay backfill source-specific.

    Swap these fields to retarget the same three-stage machinery at a different
    webhook source (e.g. octolens mentions, calcom bookings).
    """

    name: str
    raw_bucket: str
    """GCS bucket name (no ``gs://`` prefix) holding raw archived payloads."""
    hookdeck_source_name: str
    """Hookdeck source display name to filter events to."""
    dedup_key_fn: Callable[[dict[str, Any]], str]
    """Maps an *inner* payload dict to a stable dedup key."""
    output_path: Path
    webhook_app_name: str
    """Modal app name to resolve the replay URL from (``cli.webhook._modal``)."""
    table_name: str = "rb2b_visits"
    pipeline_name: str = "rb2b_visits_backfill"
    # Fallback envelope ``connection`` for flat archives with none captured —
    # mirrors the value the live model synthesizes, so replays match live rows.
    connection_label: str = "rb2b-direct"


RB2B_CONFIG = BackfillConfig(
    name="rb2b-visits",
    raw_bucket=raw_bucket_name(source="rb2b", entity_plural="visits"),
    # Hookdeck source display name (singular) — confirmed via the Hookdeck API.
    # Distinct from the connection names (rb2b-visits-attio / -raw / -etl).
    hookdeck_source_name="rb2b-visit",
    dedup_key_fn=compute_event_id,
    output_path=REPO_ROOT / "out" / "rb2b_visits.jsonl",
    webhook_app_name="export-to-attio-from-rb2b-visits",
)


# --------------------------------------------------------------------------- #
# Shared payload helpers
# --------------------------------------------------------------------------- #


def unwrap_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the inner visit payload, unwrapping an envelope if present.

    Both archives may store either the enveloped shape (``{event_id, timestamp,
    connection, payload}``) or the flat rb2b-direct shape. The dedup key and
    the replayed payload must be computed from the *inner* visit fields either
    way, so cross-source duplicates collapse.
    """
    inner = raw.get("payload")
    if isinstance(inner, dict):
        return inner
    return raw


def iter_json_objects(content: str) -> Iterator[dict[str, Any]]:
    """Yield JSON object(s) from a raw GCS object's text.

    ``export_to_gcp_raw`` writes one JSON object per ``.jsonl`` file, but be
    tolerant of NDJSON or a JSON array in case an object was written differently.
    """
    text = content.strip()
    if not text:
        return
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj
        return
    if isinstance(parsed, list):
        for obj in parsed:
            if isinstance(obj, dict):
                yield obj
    elif isinstance(parsed, dict):
        yield parsed


def _row(cfg: BackfillConfig, raw: dict[str, Any], source: str) -> dict[str, Any]:
    """Normalize one archived event into the dlt row shape.

    ``raw_payload`` is kept as an opaque JSON string so dlt does not normalize
    (snake_case) the PascalCase visit keys; the replay needs them verbatim.
    """
    payload = unwrap_payload(raw)
    return {
        "dedup_key": cfg.dedup_key_fn(payload),
        "source": source,
        "raw_payload": json.dumps(raw, sort_keys=True),
    }


# --------------------------------------------------------------------------- #
# Stage 1 — extract (dlt union + merge dedup)
# --------------------------------------------------------------------------- #


def _gcs_filesystem() -> Any:
    """Build a gcsfs filesystem, preferring an explicit service account.

    Two credential paths, in priority order:

    1. Service-account env vars (``GCP_PROJECT_ID`` / ``GCP_PRIVATE_KEY`` /
       ``GCP_CLIENT_EMAIL`` / ``GCP_PRIVATE_KEY_ID``) — the same shape the
       deployed webhook gets from its Modal secret. Used when all four are set.
    2. Application Default Credentials (``token="google_default"``) — picks up
       ``gcloud auth application-default login``. This is the local-operator
       path: no long-lived key material in a dotfile, just the gcloud identity.
       ``GCP_PROJECT_ID`` still scopes the request.
    """
    import gcsfs

    creds = GCPCredentials.get_env_vars()
    if all(
        (creds.project_id, creds.private_key, creds.client_email, creds.private_key_id),
    ):
        return gcsfs.GCSFileSystem(
            project=creds.project_id,
            token=creds.to_service_account_token(),
        )

    project = os.environ.get("GCP_PROJECT_ID") or creds.project_id
    if not project:
        msg = (
            "No GCP credentials: set the GCP_* service-account env vars, or run "
            "`gcloud auth application-default login` and set GCP_PROJECT_ID."
        )
        raise ValueError(msg)
    # "google_default" is gcsfs's ADC sentinel, not a credential.
    return gcsfs.GCSFileSystem(project=project, token="google_default")  # noqa: S106 # nosec B106


def gcs_resource(cfg: BackfillConfig) -> Any:
    """dlt resource yielding rows from every raw object in the GCS bucket."""

    @dlt.resource(
        name=cfg.table_name,
        primary_key="dedup_key",
        write_disposition="merge",
    )
    def gcs_visits() -> Iterator[dict[str, Any]]:
        fs = _gcs_filesystem()
        paths: list[str] = fs.glob(f"{cfg.raw_bucket}/*.jsonl")
        for path in paths:
            with fs.open(path, mode="r") as handle:
                content = handle.read()
            for raw in iter_json_objects(content):
                yield _row(cfg, raw, source="gcs")

    return gcs_visits()


def _retry_after_seconds(value: str | None, fallback: float) -> float:
    """Parse a ``Retry-After`` header into seconds, tolerant of both forms.

    RFC 7231 allows either ``<delay-seconds>`` (an int) or an HTTP-date. Parse
    both; on an absent or malformed header fall back to the caller's exponential
    delay rather than letting ``float("Wed, 21 Oct ...")`` raise and abort the
    whole retry path.
    """
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _hookdeck_get(
    path: str,
    api_key: str,
    params: dict[str, Any],
    *,
    max_attempts: int = 6,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """GET a Hookdeck endpoint, retrying 429/5xx with backoff.

    A multi-thousand-event backfill paginates many pages; Hookdeck rate-limits
    at 240 req/min and returns ``Retry-After`` (often 60s) on a 429. Honor it —
    a single un-retried 429 would otherwise abort the whole dlt extract.
    """
    last: requests.Response | None = None
    for attempt in range(max_attempts):
        resp = requests.get(
            f"{HOOKDECK_API_BASE}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            last = resp
            if attempt < max_attempts - 1:
                wait = _retry_after_seconds(
                    resp.headers.get("Retry-After"),
                    2.0**attempt,
                )
                sleep(wait)
                continue
        resp.raise_for_status()
        return resp.json()
    assert last is not None
    last.raise_for_status()
    return last.json()


def _resolve_hookdeck_source_id(name: str, api_key: str) -> str | None:
    """Resolve a Hookdeck source display name to its id (exact, unique match).

    Pages through *all* sources rather than trusting the first page — the
    server-side ``name`` filter is a prefix match on some Hookdeck endpoints, so
    we match exactly client-side. Raises on an ambiguous (duplicate-name) match
    so a silent wrong-source pick can't happen; returns None only when no source
    matches.
    """
    matches: list[str] = []
    next_cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": PAGE_LIMIT}
        if next_cursor:
            params["next"] = next_cursor
        body = _hookdeck_get("/sources", api_key, params)
        matches.extend(m["id"] for m in body.get("models", []) if m.get("name") == name)
        next_cursor = (body.get("pagination") or {}).get("next")
        if not next_cursor:
            break
    if len(matches) > 1:
        msg = (
            f"Hookdeck has {len(matches)} sources named {name!r}; cannot disambiguate."
        )
        raise RuntimeError(msg)
    return matches[0] if matches else None


def _extract_hookdeck_body(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the original request body out of a Hookdeck event.

    The events list endpoint returns the captured request inline under ``data``
    (we pass ``include=data``), so there's no need for an N+1 per-event detail
    fetch. Try the pre-parsed ``parsed_body`` dict first, then ``body`` (a dict
    for JSON sources, or a JSON string). A non-JSON ``body`` must not mask a
    usable ``parsed_body`` — only return None when *neither* representation
    yields a JSON object (e.g. a GET-style event).
    """
    data = event.get("data") or {}
    for candidate in (data.get("parsed_body"), data.get("body")):
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def hookdeck_resource(cfg: BackfillConfig, api_key: str, source_id: str) -> Any:
    """dlt resource yielding rows from archived Hookdeck events for the source.

    ``source_id`` is resolved (and validated) up-front in :func:`extract` so a
    missing source fails loudly *before* the pipeline runs, rather than silently
    producing an incomplete GCS-only replay.
    """

    @dlt.resource(
        name=cfg.table_name,
        primary_key="dedup_key",
        write_disposition="merge",
    )
    def hookdeck_visits() -> Iterator[dict[str, Any]]:
        next_cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "source_id": source_id,
                "limit": PAGE_LIMIT,
                # Return the captured request body inline so we don't issue an
                # N+1 detail fetch per event (that hammered the rate limit).
                "include": "data",
            }
            if next_cursor:
                params["next"] = next_cursor
            page = _hookdeck_get("/events", api_key, params)
            for event in page.get("models", []):
                raw = _extract_hookdeck_body(event)
                if raw is not None:
                    yield _row(cfg, raw, source="hookdeck")
            next_cursor = (page.get("pagination") or {}).get("next")
            if not next_cursor:
                return

    return hookdeck_visits()


def extract(cfg: BackfillConfig, *, limit: int | None, gcs_only: bool = False) -> int:
    """Run the dlt pipeline and write the deduped table to JSONL.

    Returns the number of deduped rows written. Hookdeck is included unless
    ``gcs_only`` is set (or no ``HOOKDECK_API_KEY`` is present); a Hookdeck
    source that can't be resolved is a hard error rather than a silent omission,
    since an incomplete replay looks identical to a complete one downstream.
    """
    (REPO_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
    cfg.output_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = REPO_ROOT / "tmp" / f"{cfg.pipeline_name}.duckdb"

    api_key = os.environ.get("HOOKDECK_API_KEY")
    resources = [gcs_resource(cfg)]
    if gcs_only:
        print("[extract] --gcs-only: skipping Hookdeck.", file=sys.stderr)
    elif not api_key:
        msg = (
            "HOOKDECK_API_KEY not set — refusing to run a silent GCS-only "
            "backfill. Provide the key (via Infisical or the env), or pass "
            "--gcs-only to intentionally skip Hookdeck."
        )
        raise SystemExit(msg)
    else:
        source_id = _resolve_hookdeck_source_id(cfg.hookdeck_source_name, api_key)
        if source_id is None:
            msg = (
                f"No Hookdeck source named {cfg.hookdeck_source_name!r}. The "
                "replay would silently miss Hookdeck history. Fix the source "
                "name in the config, or pass --gcs-only to skip Hookdeck."
            )
            raise SystemExit(msg)
        resources.append(hookdeck_resource(cfg, api_key, source_id))
    if limit is not None:
        resources = [r.add_limit(limit) for r in resources]

    pipeline = dlt.pipeline(
        pipeline_name=cfg.pipeline_name,
        destination=duckdb_destination(str(db_path)),
        # Distinct from the duckdb catalog name (the file stem == pipeline_name)
        # — duckdb raises "Ambiguous reference" if the schema and catalog match.
        dataset_name=f"{cfg.pipeline_name}_ds",
    )
    # `refresh="drop_sources"` drops the table + resource state before loading
    # so each extract reflects the *current* archives — the duckdb file is
    # reused across runs, and merge would otherwise let rows from a prior
    # (e.g. larger-`--limit`) run survive and be re-emitted.
    info = pipeline.run(resources, refresh="drop_sources")
    print(f"[extract] {info}", file=sys.stderr)

    # Read the deduped table back through dlt's dataset relation API (not raw
    # SQL) so we don't hand-build a query string and we stay on the blessed
    # read path. Merge has already collapsed cross-source duplicates by
    # `dedup_key`. When the same visit exists in both archives, which source's
    # `raw_payload` bytes survive the merge isn't pinned — but that's benign:
    # the `dedup_key`/`event_id` and the model-normalized `Seen At` are
    # identical either way, so the Attio upsert and re-run idempotency are
    # unaffected; only the stored body_json timestamp *string* format may vary.
    relation = pipeline.dataset()[cfg.table_name]
    columns = list(relation.columns_schema.keys())
    key_idx = columns.index("dedup_key")
    payload_idx = columns.index("raw_payload")

    count = 0
    with cfg.output_path.open("w", encoding="utf-8") as out:
        for record in relation.fetchall():
            out.write(
                json.dumps(
                    {
                        "dedup_key": record[key_idx],
                        "raw_payload": record[payload_idx],
                    },
                )
                + "\n",
            )
            count += 1
    print(f"[extract] wrote {count} deduped rows -> {cfg.output_path}")
    return count


# --------------------------------------------------------------------------- #
# Stage 2 — map (pure function)
# --------------------------------------------------------------------------- #


def _envelope_timestamp(
    payload: dict[str, Any],
    raw: dict[str, Any],
    now: datetime | None,
) -> str:
    """Best-effort ISO timestamp for the replayed envelope.

    Downstream Attio mapping keys ``event_timestamp`` off the payload's
    ``Seen At``, so the envelope timestamp is only a fallback; it just needs to
    parse. Prefer the visit's ``Seen At`` (normalized), then an archived
    envelope ``timestamp``, then now.
    """
    seen_at = normalize_rb2b_timestamp(payload.get("Seen At"))
    if isinstance(seen_at, str) and seen_at:
        return seen_at
    archived = raw.get("timestamp")
    if isinstance(archived, str) and archived:
        return archived
    return (now or datetime.now(timezone.utc)).isoformat()


def map_record(
    row: dict[str, Any],
    *,
    connection: str = "rb2b-direct",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reshape a JSONL row into the envelope the Modal webhook expects.

    The ``event_id`` is set explicitly to the precomputed ``dedup_key`` (==
    ``compute_event_id`` of the inner payload), so the replay is robust even if
    the live model's flat-detection heuristic changes, and the Attio
    tracking-event ``external_id`` is stable across runs.

    The replayed envelope reproduces live ingestion as faithfully as possible:
    the original ``connection`` is preserved when the archive captured one
    (Hookdeck envelopes), and flat archives fall back to ``connection`` —
    defaulting to the same ``"rb2b-direct"`` the live model synthesizes for flat
    deliveries. ``body_json`` is derived from the model downstream, so matching
    ``connection`` keeps replayed rows byte-identical to live ones (no churn).
    """
    raw = json.loads(row["raw_payload"])
    payload = unwrap_payload(raw)
    return {
        "event_id": row["dedup_key"],
        "timestamp": _envelope_timestamp(payload, raw, now),
        "connection": raw.get("connection") or connection,
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# Stage 3 — send
# --------------------------------------------------------------------------- #


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _load_sent(sent_log: Path) -> set[str]:
    if not sent_log.exists():
        return set()
    with sent_log.open(encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def _post_with_retries(
    url: str,
    envelope: dict[str, Any],
    *,
    max_attempts: int = 4,
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """POST one envelope, retrying transient failures with exponential backoff.

    Network errors, 5xx, and 429 (rate limit) are retried — a 429 honors the
    ``Retry-After`` header when present. Other 4xx are terminal (a malformed
    payload won't fix itself) and raise immediately so the row is logged and
    skipped.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        wait = backoff_base * (2**attempt)
        try:
            resp = requests.post(url, json=envelope, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code < 400:
                return
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                msg = f"HTTP {resp.status_code}: {resp.text[:500]}"
                raise RuntimeError(msg)
            # 429 or 5xx — transient; honor Retry-After on a 429.
            last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            wait = _retry_after_seconds(resp.headers.get("Retry-After"), wait)
        if attempt < max_attempts - 1:
            sleep(wait)
    raise RuntimeError(f"exhausted retries: {last_exc}")


def send(
    cfg: BackfillConfig,
    *,
    webhook_url: str | None,
    dry_run: bool,
    limit: int | None,
    rate_limit_s: float,
) -> tuple[int, int, int]:
    """Replay deduped records to the Modal webhook.

    Returns ``(sent, skipped, failed)``. Idempotent + resumable: already-sent
    ``dedup_key``s are skipped; failures are appended to ``failed.jsonl``.
    """
    if not cfg.output_path.exists():
        msg = f"{cfg.output_path} not found — run `extract` first."
        raise SystemExit(msg)

    url = webhook_url or modal_url_for_app(cfg.webhook_app_name)
    sent_log = cfg.output_path.with_name("sent.log")
    failed_log = cfg.output_path.with_name("failed.jsonl")
    already = _load_sent(sent_log)

    sent = skipped = failed = 0
    for row in _read_jsonl(cfg.output_path):
        if limit is not None and sent >= limit:
            break
        dedup_key = row["dedup_key"]
        if dedup_key in already:
            skipped += 1
            continue

        # Map + validate + POST inside one guard: a single malformed row (an
        # unforeseen payload that fails model validation, or a hard webhook
        # error) is logged to failed.jsonl and skipped, never aborting the
        # whole backfill. 4xx/validation errors won't retry; 429/5xx do.
        try:
            envelope = map_record(row, connection=cfg.connection_label)
            # Validate against the real model first — catches malformed rows
            # offline rather than as a webhook 422.
            Rb2bWebhook.model_validate(envelope)
            if dry_run:
                print(json.dumps(envelope))
                sent += 1
                continue
            _post_with_retries(url, envelope)
        except Exception as exc:  # noqa: BLE001 - record and continue the batch
            failed += 1
            with failed_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"dedup_key": dedup_key, "error": str(exc)}) + "\n",
                )
            print(f"[send] FAILED {dedup_key}: {exc}", file=sys.stderr)
            continue

        with sent_log.open("a", encoding="utf-8") as fh:
            fh.write(dedup_key + "\n")
        already.add(dedup_key)
        sent += 1
        if rate_limit_s > 0:
            time.sleep(rate_limit_s)

    verb = "would send" if dry_run else "sent"
    print(f"[send] {verb}={sent} skipped={skipped} failed={failed} url={url}")
    return sent, skipped, failed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    example = infisical_run_example(
        "scripts/rb2b-visits-backfill.py extract --limit 25",
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Example:\n  {example}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser(
        "extract",
        help="Pull GCS + Hookdeck, dedupe via dlt, write out/rb2b_visits.jsonl.",
    )
    p_extract.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap rows pulled per source (smoke testing). Default: all.",
    )
    p_extract.add_argument(
        "--gcs-only",
        action="store_true",
        help="Skip Hookdeck and backfill only from GCS (intentional opt-in).",
    )

    p_send = sub.add_parser(
        "send",
        help="Replay out/rb2b_visits.jsonl to the Modal webhook, one by one.",
    )
    p_send.add_argument(
        "--webhook-url",
        default=None,
        help="Override the resolved Modal webhook URL.",
    )
    p_send.add_argument(
        "--dry-run",
        action="store_true",
        help="Map + validate + print envelopes without POSTing.",
    )
    p_send.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N sends (skipped/already-sent records don't count).",
    )
    p_send.add_argument(
        "--rate-limit",
        type=float,
        default=0.2,
        help="Seconds to sleep between successful POSTs (default: 0.2).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = RB2B_CONFIG

    if args.command == "extract":
        extract(cfg, limit=args.limit, gcs_only=args.gcs_only)
        return 0
    if args.command == "send":
        send(
            cfg,
            webhook_url=args.webhook_url,
            dry_run=args.dry_run,
            limit=args.limit,
            rate_limit_s=args.rate_limit,
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
