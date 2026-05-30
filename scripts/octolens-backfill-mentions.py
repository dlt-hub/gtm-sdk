#!/usr/bin/env -S uv run python
"""Backfill historical Octolens dlt/dlthub mentions into Attio.

Two phases in one script:

1. **build** — read the Octolens CSV exports, keep only the real dlt/dlthub
   mentions (see ``src/octolens/backfill.py::include_mention``), dedup by URL,
   and write one ``mentions.jsonl`` to ``out/octolens-backfill/``. A review
   table (``url | source | keyword | reason``) is printed so the content-signal
   list can be audited and tuned.
2. **send** — map each JSONL row to the Octolens webhook payload, validate it
   locally against the real ``Webhook`` model, and POST it one-by-one to the
   deployed Attio Modal endpoint. Resumable via a per-out-dir ``sent.log``.

This script needs **no secrets**: the build is purely local and the send POSTs
to the *public* Modal endpoint (which holds its own ATTIO_API_KEY). Run it
directly — no ``infisical run`` wrapper required. Point it at the CSV directory
with ``--data-dir`` or the ``OCTOLENS_DATA_DIR`` env var (the exports live in the
parent ``ai/`` repo, so there is no portable default):

    export OCTOLENS_DATA_DIR=~/Documents/ai/data/octolens
    uv run python scripts/octolens-backfill-mentions.py                 # build + preview
    uv run python scripts/octolens-backfill-mentions.py --send          # dry-run send
    uv run python scripts/octolens-backfill-mentions.py --send --apply  # real POST (dev first!)
    uv run python scripts/octolens-backfill-mentions.py --send --apply --limit 1

dev vs prod is selected by ``--endpoint-url`` (or the ``MODAL_WORKSPACE`` env var
that ``modal_url_for_app`` reads). Confirm which endpoint targets dev Attio
before a full ``--apply`` run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Anchor on the script's directory so paths resolve regardless of the CWD
# `uv run` was invoked from, and so local `src`/`cli`/`libs` imports work.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import orjson  # noqa: E402

from cli.webhook._modal import modal_url_for_app  # noqa: E402
from src.octolens.backfill import build_webhook_payload, include_mention  # noqa: E402
from src.octolens.webhook import Webhook  # noqa: E402

# Some mention bodies exceed Python's default 128 KiB CSV field cap.
csv.field_size_limit(10**9)

# The legacy export has an opaque, non-conforming schema — never load it.
LEGACY_FILES = frozenset({"octolens-mentions-202603082207.csv"})
# The CSV exports live in the parent `ai/` repo (outside this repo), so there is
# no portable default. Read the dir from OCTOLENS_DATA_DIR, else require
# --data-dir; main() fails fast with a clear message when neither is set.
_ENV_DATA_DIR = os.environ.get("OCTOLENS_DATA_DIR")
DEFAULT_DATA_DIR = Path(_ENV_DATA_DIR) if _ENV_DATA_DIR else None
DEFAULT_OUT_DIR = REPO_ROOT / "out" / "octolens-backfill"


def build_jsonl(data_dir: Path, out_dir: Path, *, rebuild: bool) -> Path:
    """Phase 1: filter + dedup the CSV exports into a single JSONL."""
    jsonl_path = out_dir / "mentions.jsonl"
    if jsonl_path.exists() and not rebuild:
        print(f"[build] reusing {jsonl_path} (pass --rebuild to regenerate)")
        return jsonl_path

    files = sorted(
        p
        for p in data_dir.glob("octolens-mentions-*.csv")
        if p.name not in LEGACY_FILES
    )
    if not files:
        raise SystemExit(f"[build] no Octolens CSVs found under {data_dir}")

    # Dedup by URL FIRST — the newest export row wins on a collision (files are
    # sorted by their timestamped name), regardless of whether it passes the
    # keyword filter — so a newer export always supersedes an older row for the
    # same URL. Only then apply the inclusion predicate, to the deduped (latest)
    # row. (Filtering before dedup could keep a stale older row when the newest
    # export no longer matches.) The first header carries a UTF-8 BOM ("﻿URL"),
    # so we MUST read with utf-8-sig or the URL dedup key silently misses.
    latest: dict[str, dict[str, Any]] = {}
    read = 0
    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                read += 1
                url = (row.get("URL") or "").strip()
                if url:
                    row["_source_file"] = path.name
                    latest[url] = row

    seen: dict[str, dict[str, Any]] = {}
    for row in latest.values():
        keep, reason = include_mention(row)
        if keep:
            row["_include_reason"] = reason
            seen[(row.get("URL") or "").strip()] = row

    out_dir.mkdir(parents=True, exist_ok=True)
    reasons: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    with jsonl_path.open("wb") as out:
        for row in seen.values():
            out.write(orjson.dumps(row))
            out.write(b"\n")
            reasons[row["_include_reason"]] += 1
            sources[(row.get("Source") or "").strip().lower()] += 1

    print(f"[build] files={[p.name for p in files]}")
    print(f"[build] read={read} unique_mentions={len(seen)} -> {jsonl_path}")
    print(f"[build] by reason: {dict(reasons)}")
    print(f"[build] by source: {dict(sources.most_common())}")
    _print_review_table(seen.values())
    return jsonl_path


def _print_review_table(rows) -> None:
    """Print `reason | source | keyword | url` so the signal list is auditable."""
    print("\n[review] included mentions (tune src/octolens/backfill.py::DLT_SIGNALS):")
    print(f"  {'reason':<15} {'source':<10} {'keyword':<22} url")
    for row in rows:
        reason = row.get("_include_reason", "")
        source = (row.get("Source") or "").strip().lower()
        keyword = (row.get("Keyword") or "").strip()
        url = (row.get("URL") or "").strip()
        print(f"  {reason:<15} {source:<10} {keyword[:22]:<22} {url[:90]}")


def _load_rows(jsonl_path: Path) -> list[dict[str, Any]]:
    return [
        orjson.loads(line)
        for line in jsonl_path.read_bytes().splitlines()
        if line.strip()
    ]


def _parse_envelope(resp: httpx.Response) -> dict[str, Any] | None:
    """Decode the operation envelope from a 2xx response.

    The endpoint returns ``execute(plan).body()`` as a JSON-*encoded* string
    (the HTTP body is a quoted string), or a plain reason string when the
    webhook was filtered/invalid. Returns the envelope dict, or None when the
    body is a non-envelope reason string.
    """
    try:
        parsed: Any = resp.json()
    except Exception:  # noqa: BLE001 — fall back to raw text
        parsed = resp.text
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict) and "success" in parsed:
        return parsed
    return None


def _deliver(
    client: httpx.Client,
    url: str,
    body: bytes,
    *,
    retries: int = 3,
) -> tuple[str, str]:
    """POST one mention and classify the result as ``(status, detail)``.

    status:
      - ``"ok"``      — envelope ``success == true`` (record written).
      - ``"failed"``  — HTTP 4xx/5xx after retries, OR envelope
                        ``success == false`` (e.g. a failed Attio op).
                        **HTTP 200 alone is NOT success**: this endpoint returns
                        200 with ``success=false`` when an operation errors.
      - ``"skipped"`` — endpoint returned a plain reason string (filtered or
                        invalid webhook); nothing was written.
    Retries only transient HTTP 5xx / timeouts; an envelope ``success=false`` is
    deterministic and not retried.
    """
    detail = "no attempt"
    for attempt in range(retries + 1):
        try:
            resp = client.post(
                url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            detail = type(exc).__name__
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            return "failed", detail
        if resp.status_code >= 500:
            detail = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            return "failed", detail
        if resp.status_code >= 400:
            return "failed", f"HTTP {resp.status_code}: {resp.text[:160]}"
        envelope = _parse_envelope(resp)
        if envelope is None:
            return "skipped", resp.text[:160]
        if envelope.get("success") is True:
            outcomes = envelope.get("outcomes") or []
            summary = ", ".join(
                f"{o.get('op_type')}={o.get('record_id') or o.get('success')}"
                for o in outcomes
            )
            return "ok", summary
        return "failed", json.dumps(envelope.get("outcomes") or envelope)[:300]
    return "failed", detail


def send(
    jsonl_path: Path,
    *,
    endpoint_url: str,
    relevance: str,
    apply: bool,
    limit: int | None,
    timeout: float,
    sent_log: Path,
) -> None:
    """Phase 2: map → validate → POST each mention (dry-run unless ``apply``)."""
    already: set[str] = set()
    if sent_log.exists():
        already = {
            line.strip() for line in sent_log.read_text().splitlines() if line.strip()
        }

    rows = _load_rows(jsonl_path)
    todo = [r for r in rows if (r.get("URL") or "").strip() not in already]
    if limit is not None:
        todo = todo[:limit]

    mode = "APPLY (real POST)" if apply else "DRY-RUN (no POST)"
    print(f"\n[send] {mode}  endpoint={endpoint_url}")
    print(
        f"[send] relevance={relevance}  total={len(rows)} already_sent={len(already)} "
        f"to_process={len(todo)}",
    )

    ok = skipped = failed = 0
    client = httpx.Client(timeout=timeout)
    try:
        for i, row in enumerate(todo, start=1):
            url = (row.get("URL") or "").strip()
            payload = build_webhook_payload(
                row,
                relevance=relevance,
                source_file=row.get("_source_file", "unknown"),
            )
            # Identity fields must be non-empty. build_webhook_payload coerces
            # missing values to "" and Mention accepts empty strings, so the
            # model alone would let a blank source_id through — guard explicitly.
            data = payload["data"]
            missing = [
                field
                for field in ("url", "source", "source_id")
                if not str(data.get(field) or "").strip()
            ]
            if missing:
                print(
                    f"  [skip] missing required {missing} url={url[:70] or '(blank)'}",
                )
                skipped += 1
                continue
            try:
                webhook = Webhook.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 — log + skip any invalid row
                print(f"  [skip] invalid payload url={url}: {str(exc)[:160]}")
                skipped += 1
                continue
            if not webhook.attio_is_valid_webhook():
                print(
                    f"  [skip] {webhook.attio_get_invalid_webhook_error_msg()} url={url}",
                )
                skipped += 1
                continue

            if not apply:
                if i <= 5:
                    print(
                        f"  [dry] would POST source={payload['data']['source']} "
                        f"relevance={payload['data']['relevance_score']} url={url}",
                    )
                ok += 1
                continue

            status, detail = _deliver(client, endpoint_url, orjson.dumps(payload))
            if status == "ok":
                ok += 1
                with sent_log.open("a") as fh:
                    fh.write(url + "\n")
                if i <= 5:
                    print(f"  [ok] {url[:70]} :: {detail[:120]}")
            elif status == "skipped":
                skipped += 1
                print(f"  [skip] {detail[:140]} url={url[:70]}")
            else:
                failed += 1
                print(f"  [FAIL] {url[:70]} :: {detail[:200]}")
            if i % 20 == 0:
                print(
                    f"  [progress] {i}/{len(todo)} ok={ok} failed={failed} skipped={skipped}",
                )
    finally:
        client.close()

    verb = "sent" if apply else "would send"
    print(f"[send] done: {verb}={ok} skipped={skipped} failed={failed}")
    if not apply:
        print("[send] dry-run only — re-run with --apply to POST for real.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="directory of octolens-mentions-*.csv (or set OCTOLENS_DATA_DIR)",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--rebuild", action="store_true", help="regenerate the JSONL")
    parser.add_argument(
        "--relevance",
        choices=("low", "medium", "high", "unknown"),
        default="unknown",
        help="relevance_score stamped on every backfilled mention",
    )
    parser.add_argument("--send", action="store_true", help="run the send phase")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually POST (else dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap mentions processed",
    )
    parser.add_argument(
        "--endpoint-url",
        default=modal_url_for_app(Webhook.attio_get_app_name()),
        help="Attio webhook URL (defaults from MODAL_WORKSPACE; override for prod)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.data_dir is None:
        raise SystemExit(
            "No data dir: pass --data-dir or set OCTOLENS_DATA_DIR "
            "(e.g. ~/Documents/ai/data/octolens).",
        )
    jsonl_path = build_jsonl(args.data_dir, args.out_dir, rebuild=args.rebuild)

    if not args.send:
        # Preview a few mapped+validated payloads so the mapper is verifiable
        # without sending anything.
        print("\n[preview] sample mapped payloads:")
        for row in _load_rows(jsonl_path)[:3]:
            payload = build_webhook_payload(
                row,
                relevance=args.relevance,
                source_file=row.get("_source_file", "unknown"),
            )
            Webhook.model_validate(payload)  # raises if the mapper is wrong
            print("  " + orjson.dumps(payload["data"]).decode()[:200] + " ...")
        print(
            "\n[preview] build complete. Re-run with --send (then --apply) to deliver.",
        )
        return

    send(
        jsonl_path,
        endpoint_url=args.endpoint_url,
        relevance=args.relevance,
        apply=args.apply,
        limit=args.limit,
        timeout=args.timeout,
        sent_log=args.out_dir / "sent.log",
    )


if __name__ == "__main__":
    main()
