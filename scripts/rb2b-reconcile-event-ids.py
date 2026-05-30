#!/usr/bin/env -S uv run python
"""Reconcile pre-refactor rb2b tracking events that carry random external_ids.

Before the deterministic-id refactor (``libs/rb2b/models.py``), the rb2b webhook
minted a *random* ``evt_<uuid4().hex>`` for every flat visit, which became the
Attio tracking-event ``external_id = rb2b:{event_id}``. Post-refactor,
``compute_event_id`` derives a *content* hash ``evt_<sha256[:32]>`` from the
visit's identity ``(Business Email, LinkedIn URL, Captured URL, Seen At)``.

The visits→Attio backfill replayed historical visits through the webhook with
deterministic ids, so any visit that was *also* ingested live before the refactor
now has TWO rows: the old random-id one and the deterministic one. This script
reconciles them.

**Why we can't tell old from new by the id alone:** ``uuid4().hex`` and
``sha256[:32]`` are both 32 hex chars — the ``evt_<32 hex>`` shape is identical.
The only reliable signal is to *recompute* the deterministic id from each row's
stored ``body`` payload and compare it to the stored ``external_id``. A row is
canonical iff they match.

Reconciliation, grouped by the recomputed deterministic id:

* group has a canonical row → keep the first canonical row, DELETE the rest
  (the old random-id duplicates that the backfill superseded);
* group has only old rows (visit never re-created by the backfill) → PATCH one
  row's ``external_id`` to the deterministic value (promote it) and delete any
  other old dupes, so live + future replays converge on it;
* lone canonical row → no-op (the overwhelming majority).

Fully-anonymous visits (no identity field set) are SKIPPED, not deleted:
``compute_event_id`` hashes the whole payload for them, which we cannot faithfully
reproduce from the stored ``body``.

Dry-run by default — prints a summary and writes the full planned actions to
``out/rb2b_reconcile_plan.jsonl``. Pass ``--apply`` to execute the deletes/patches
(irreversible in Attio). Re-running after ``--apply`` is a no-op.

Run via Infisical so the workspace ``ATTIO_API_KEY`` is injected (``--env`` picks
dev vs prod — run dev first, confirm, then prod):

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/rb2b-reconcile-event-ids.py
    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- scripts/rb2b-reconcile-event-ids.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Anchor on the script's directory so output paths resolve regardless of the CWD
# `uv run` was invoked from, and make repo-local packages importable.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.attio.client import get_client  # noqa: E402
from libs.attio.sdk_boundary import (  # noqa: E402
    build_patch_record_request,
    is_unknown_filter_attribute,
)
from libs.rb2b import compute_event_id  # noqa: E402
from scripts.lib.env import infisical_run_example  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

_OBJECT = "tracking_events"
_RB2B_EVENT_TYPE = "rb2b_visit"
_RB2B_SOURCE = "rb2b"
_PAGE_SIZE = 100
# Audit log + planned actions land in the repo-root scratch dir (gitignored;
# AGENTS.md mandates tmp/ for run artifacts) so a run never dirties the tree.
_DEFAULT_OUT = REPO_ROOT / "tmp" / "rb2b_reconcile_plan.jsonl"

# Map the snake_case keys that ``Webhook.model_dump`` writes into the stored
# ``body`` envelope back to the PascalCase identity keys ``compute_event_id``
# reads. Both spellings are accepted defensively in case a body was ever stored
# with aliases.
_IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("Business Email", "business_email"),
    ("LinkedIn URL", "linkedin_url"),
    ("Captured URL", "captured_url"),
    ("Seen At", "seen_at"),
)


# --------------------------------------------------------------------------- #
# Row + plan data structures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Row:
    """A single rb2b tracking_events row, reduced to what reconciliation needs."""

    record_id: str
    external_id: str
    body_json: str


@dataclass
class ReconPlan:
    """The network-free reconciliation decision for a batch of rows."""

    deletes: list[dict[str, Any]] = field(default_factory=list)
    promotions: list[dict[str, Any]] = field(default_factory=list)
    skipped_anonymous: list[dict[str, Any]] = field(default_factory=list)
    unparseable: list[dict[str, Any]] = field(default_factory=list)
    noop_count: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "deletes": len(self.deletes),
            "promotions": len(self.promotions),
            "skipped_anonymous": len(self.skipped_anonymous),
            "unparseable": len(self.unparseable),
            "noop": self.noop_count,
        }


# --------------------------------------------------------------------------- #
# Pure planning logic (no network — unit-tested)
# --------------------------------------------------------------------------- #


def expected_external_id(body_json: str) -> tuple[str | None, str]:
    """Recompute the deterministic ``external_id`` from a stored ``body`` envelope.

    Returns ``(external_id, "ok")`` for an identifiable visit,
    ``(None, "anonymous")`` when no identity field is set, or
    ``(None, "unparseable")`` when the body isn't the expected envelope shape.

    **Anonymous rows are intentionally out of scope** (gated here, surfaced as a
    warning by ``main`` when the count is nonzero). A row counts as anonymous
    only when *all four* identity fields — Business Email, LinkedIn URL, Captured
    URL, **and Seen At** — are empty. Two reasons this is a documented limitation
    rather than a gap:

    1. *Practically empty.* Every real rb2b visit carries ``Seen At`` (the hit
       timestamp) and almost always ``Captured URL`` (the visited page), so a
       genuine visit is never anonymous. A nonzero count means a malformed /
       truncated body and warrants manual inspection, not automatic deletion.
    2. *Not faithfully recomputable anyway.* For the no-identity case
       ``compute_event_id`` hashes the *entire raw flat payload*. The stored
       ``body`` is ``Payload.model_dump()`` — lossy vs. that raw payload (keys
       are snake_case, ``employee_count`` is coerced int→str so it serializes as
       ``"50"`` not ``50``, defaulted fields are materialized), so the
       full-payload fallback hash cannot be reproduced byte-for-byte. Deleting on
       a best-effort reconstruction would risk dropping a non-duplicate row.
    """
    try:
        envelope = json.loads(body_json)
    except (json.JSONDecodeError, TypeError):
        return None, "unparseable"
    if not isinstance(envelope, dict):
        return None, "unparseable"
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None, "unparseable"

    recon: dict[str, Any] = {
        pascal: payload.get(snake) or payload.get(pascal)
        for pascal, snake in _IDENTITY_FIELDS
    }
    if not any(str(value or "") for value in recon.values()):
        return None, "anonymous"
    return f"{_RB2B_SOURCE}:{compute_event_id(recon)}", "ok"


def plan_reconciliation(rows: list[Row]) -> ReconPlan:
    """Decide the delete/promote actions for a batch of rb2b rows.

    Pure function: groups rows by their recomputed deterministic id and applies
    the keep-one-canonical / promote-orphan rules. No I/O so it can be tested
    against synthetic rows.
    """
    plan = ReconPlan()
    groups: dict[str, list[Row]] = {}

    for row in rows:
        expected, status = expected_external_id(row.body_json)
        if status == "unparseable":
            plan.unparseable.append(
                {"record_id": row.record_id, "external_id": row.external_id},
            )
            continue
        if status == "anonymous":
            plan.skipped_anonymous.append(
                {"record_id": row.record_id, "external_id": row.external_id},
            )
            continue
        assert expected is not None  # noqa: S101 - narrowed by status == "ok"
        groups.setdefault(expected, []).append(row)

    for expected, members in groups.items():
        # Stable survivor selection so the plan is reproducible across runs.
        members = sorted(members, key=lambda r: r.record_id)
        canonical = [r for r in members if r.external_id == expected]
        noncanonical = [r for r in members if r.external_id != expected]

        if canonical:
            survivor = canonical[0]
            if len(canonical) == 1 and not noncanonical:
                plan.noop_count += 1
                continue
            for row in canonical[1:]:
                plan.deletes.append(
                    _delete_entry(row, survivor, expected, "duplicate_canonical"),
                )
            for row in noncanonical:
                plan.deletes.append(
                    _delete_entry(row, survivor, expected, "superseded_by_canonical"),
                )
        else:
            survivor = noncanonical[0]
            plan.promotions.append(
                {
                    "record_id": survivor.record_id,
                    "from_external_id": survivor.external_id,
                    "to_external_id": expected,
                },
            )
            for row in noncanonical[1:]:
                plan.deletes.append(
                    _delete_entry(row, survivor, expected, "duplicate_orphan"),
                )

    return plan


def _delete_entry(
    row: Row,
    survivor: Row,
    expected: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "record_id": row.record_id,
        "external_id": row.external_id,
        "expected_external_id": expected,
        "survivor_record_id": survivor.record_id,
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# Attio I/O
# --------------------------------------------------------------------------- #


def _text(record: Any, slug: str) -> str:
    """Read a single-value text attribute off an Attio record, or "".

    Attio returns every attribute as a list of value dicts even for
    single-value text fields; the first entry's ``value`` is the active text.
    """
    dump = record.model_dump() if hasattr(record, "model_dump") else dict(record)
    values = (dump.get("values") or {}).get(slug) or []
    if not values:
        return ""
    first = values[0]
    if isinstance(first, dict):
        return first.get("value") or ""
    return ""


def _query_page(client: Any, filter_: dict[str, Any], offset: int) -> list[Any]:
    response = client.records.post_v2_objects_object_records_query(
        object=_OBJECT,
        filter_=filter_,
        limit=_PAGE_SIZE,
        offset=offset,
    )
    return list(response.data or [])


def _iter_records(client: Any) -> Iterator[Any]:
    """Page through rb2b tracking_events via the ``event_type`` server-side filter.

    ``event_type=rb2b_visit`` is the precise, schema-confirmed selector for rb2b
    visit rows (the webhook writes it on every row). We deliberately do NOT fall
    back to a full-table scan filtered on a select value client-side: select
    attributes come back as nested ``{"option": {...}}`` shapes (not the flat
    ``{"value": ...}`` that ``_text`` reads), so a naive client-side compare would
    silently match nothing. If Attio ever rejects this filter as an unknown
    attribute, that is a genuine schema misconfiguration — surface it loudly
    rather than papering over it with a broken scan.
    """
    try:
        yield from _iter_filtered(client, {"event_type": _RB2B_EVENT_TYPE})
    except Exception as exc:  # noqa: BLE001
        if is_unknown_filter_attribute(exc):
            raise RuntimeError(
                "tracking_events has no 'event_type' filter attribute on this "
                "workspace — the schema is not bootstrapped for rb2b. Bootstrap "
                "it before reconciling.",
            ) from exc
        raise


def _iter_filtered(client: Any, filter_: dict[str, Any]) -> Iterator[Any]:
    offset = 0
    while True:
        page = _query_page(client, filter_, offset)
        if not page:
            return
        yield from page
        if len(page) < _PAGE_SIZE:
            return
        offset += _PAGE_SIZE


def fetch_rows(client: Any, limit: int | None) -> list[Row]:
    rows: list[Row] = []
    for record in _iter_records(client):
        # Check before appending so `limit=0` scans zero rows (no off-by-one).
        if limit is not None and len(rows) >= limit:
            break
        rows.append(
            Row(
                record_id=record.id.record_id,
                external_id=_text(record, "external_id"),
                body_json=_text(record, "body"),
            ),
        )
    return rows


def apply_plan(client: Any, plan: ReconPlan, log_path: Path) -> dict[str, int]:
    """Execute deletes + promotions, one record at a time, logging each outcome.

    A single failing record is logged and skipped rather than aborting the batch.
    """
    deleted = 0
    promoted = 0
    failed = 0
    with log_path.open("a", encoding="utf-8") as log:
        for entry in plan.promotions:
            try:
                client.records.patch_v2_objects_object_records_record_id_(
                    object=_OBJECT,
                    record_id=entry["record_id"],
                    data=build_patch_record_request(
                        {"external_id": [{"value": entry["to_external_id"]}]},
                    ),
                )
                promoted += 1
                _log(log, {"action": "promoted", **entry})
            except Exception as exc:  # noqa: BLE001
                failed += 1
                _log(log, {"action": "promote_failed", "error": str(exc), **entry})

        for entry in plan.deletes:
            try:
                client.records.delete_v2_objects_object_records_record_id_(
                    object=_OBJECT,
                    record_id=entry["record_id"],
                )
                deleted += 1
                _log(log, {"action": "deleted", **entry})
            except Exception as exc:  # noqa: BLE001
                failed += 1
                _log(log, {"action": "delete_failed", "error": str(exc), **entry})

    return {"deleted": deleted, "promoted": promoted, "failed": failed}


def _log(handle: Any, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload) + "\n")


def write_plan(plan: ReconPlan, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for kind, entries in (
            ("delete", plan.deletes),
            ("promote", plan.promotions),
            ("skip_anonymous", plan.skipped_anonymous),
            ("unparseable", plan.unparseable),
        ):
            for entry in entries:
                handle.write(json.dumps({"planned": kind, **entry}) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  "
        + infisical_run_example("scripts/rb2b-reconcile-event-ids.py"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletes/patches. Default is a dry run (plan only).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of rb2b rows scanned. Dry-run smoke testing only — "
        "incompatible with --apply.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Where to write the planned-action JSONL (default: {_DEFAULT_OUT}).",
    )
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer.")
    # A partial scan can't be applied safely: an old row's canonical twin may
    # lie beyond the scanned window, so plan_reconciliation would wrongly promote
    # the old row (or delete the wrong member of a truncated group). --limit is
    # for dry-run smoke testing only.
    if args.apply and args.limit is not None:
        parser.error(
            "--apply cannot be combined with --limit: a truncated scan can "
            "mis-reconcile rows whose canonical twin is outside the window. "
            "Run --apply against the full dataset.",
        )

    with get_client() as client:
        rows = fetch_rows(client, args.limit)
        print(f"Scanned {len(rows)} rb2b tracking_events rows.")
        plan = plan_reconciliation(rows)
        write_plan(plan, args.out)

        summary = plan.summary()
        print(
            "Plan: "
            f"{summary['deletes']} delete, "
            f"{summary['promotions']} promote, "
            f"{summary['noop']} no-op, "
            f"{summary['skipped_anonymous']} skipped-anonymous, "
            f"{summary['unparseable']} unparseable.",
        )
        print(f"Full plan written to {args.out}")

        # Anonymous rows are never auto-reconciled (see expected_external_id).
        # A nonzero count is unexpected for real rb2b traffic — flag it loudly so
        # the operator inspects those bodies by hand rather than assuming the run
        # covered everything.
        if summary["skipped_anonymous"]:
            print(
                f"WARNING: {summary['skipped_anonymous']} anonymous row(s) skipped "
                "(no identity fields incl. Seen At). Inspect them in the plan "
                "JSONL; they are not reconciled automatically.",
            )

        if not args.apply:
            print("Dry run — no records modified. Re-run with --apply to execute.")
            return 0

        if not plan.deletes and not plan.promotions:
            print("Nothing to apply.")
            return 0

        result = apply_plan(client, plan, args.out)
        print(
            f"Applied: {result['promoted']} promoted, "
            f"{result['deleted']} deleted, {result['failed']} failed.",
        )
        return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
