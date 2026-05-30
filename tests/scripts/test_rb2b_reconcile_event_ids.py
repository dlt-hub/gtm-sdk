"""Offline tests for scripts/rb2b-reconcile-event-ids.py.

Covers the network-free reconciliation planner. Synthetic ``body`` envelopes are
built by round-tripping the real ``Rb2bWebhook`` model so the stored snake_case
key shape matches production exactly. The Attio I/O (paging, delete, patch) is
exercised live during the dev smoke test, not here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from libs.rb2b import Webhook as Rb2bWebhook

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "rb2b-reconcile-event-ids.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rb2b_reconcile_event_ids",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's dataclasses (string annotations under
    # `from __future__ import annotations`) resolve via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recon = _load_script_module()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _body_and_deterministic(flat: dict[str, Any]) -> tuple[str, str]:
    """Build a production-shaped ``body`` envelope + its deterministic id.

    Mirrors what ``src/rb2b/webhook/visit.py`` stores:
    ``json.dumps(Webhook.model_dump(mode="json"))``.
    """
    webhook = Rb2bWebhook.model_validate(flat)
    body = json.dumps(webhook.model_dump(mode="json"))
    return body, f"rb2b:{webhook.event_id}"


_IDENTIFIED_VISIT = {
    "Business Email": "ada@example.com",
    "LinkedIn URL": "https://linkedin.com/in/ada",
    "Captured URL": "https://dlthub.com/pricing",
    "Seen At": "2026-05-11 21:04:43 +0000",
    "Company Name": "Example Inc",
}


# --------------------------------------------------------------------------- #
# expected_external_id
# --------------------------------------------------------------------------- #


def test_recompute_matches_live_deterministic_id() -> None:
    body, deterministic = _body_and_deterministic(_IDENTIFIED_VISIT)
    expected, status = recon.expected_external_id(body)
    assert status == "ok"
    assert expected == deterministic


def test_anonymous_body_is_flagged_not_recomputed() -> None:
    # No identity field set — only triggers the flat-wrap via Company Name.
    body, _ = _body_and_deterministic({"Company Name": "Anon Co"})
    expected, status = recon.expected_external_id(body)
    assert status == "anonymous"
    assert expected is None


def test_unparseable_bodies() -> None:
    assert recon.expected_external_id("not json")[1] == "unparseable"
    assert recon.expected_external_id('{"no": "payload"}')[1] == "unparseable"


# --------------------------------------------------------------------------- #
# plan_reconciliation
# --------------------------------------------------------------------------- #


def test_old_duplicate_deleted_when_canonical_twin_exists() -> None:
    body, deterministic = _body_and_deterministic(_IDENTIFIED_VISIT)
    rows = [
        # old record sorts first, but the canonical row must still survive.
        recon.Row("aaa-old", "rb2b:evt_" + "0" * 32, body),
        recon.Row("zzz-canonical", deterministic, body),
    ]
    plan = recon.plan_reconciliation(rows)

    assert plan.promotions == []
    assert len(plan.deletes) == 1
    deleted = plan.deletes[0]
    assert deleted["record_id"] == "aaa-old"
    assert deleted["survivor_record_id"] == "zzz-canonical"
    assert deleted["reason"] == "superseded_by_canonical"
    assert plan.noop_count == 0


def test_orphan_old_row_is_promoted_not_deleted() -> None:
    body, deterministic = _body_and_deterministic(_IDENTIFIED_VISIT)
    rows = [recon.Row("rec-old", "rb2b:evt_" + "1" * 32, body)]
    plan = recon.plan_reconciliation(rows)

    assert plan.deletes == []
    assert len(plan.promotions) == 1
    promo = plan.promotions[0]
    assert promo["record_id"] == "rec-old"
    assert promo["to_external_id"] == deterministic
    assert promo["from_external_id"] == "rb2b:evt_" + "1" * 32


def test_lone_canonical_row_is_noop() -> None:
    body, deterministic = _body_and_deterministic(_IDENTIFIED_VISIT)
    plan = recon.plan_reconciliation([recon.Row("rec", deterministic, body)])

    assert plan.deletes == []
    assert plan.promotions == []
    assert plan.noop_count == 1


def test_duplicate_canonical_rows_keep_one() -> None:
    body, deterministic = _body_and_deterministic(_IDENTIFIED_VISIT)
    rows = [
        recon.Row("rec-a", deterministic, body),
        recon.Row("rec-b", deterministic, body),
    ]
    plan = recon.plan_reconciliation(rows)

    assert plan.promotions == []
    assert len(plan.deletes) == 1
    assert plan.deletes[0]["record_id"] == "rec-b"
    assert plan.deletes[0]["survivor_record_id"] == "rec-a"
    assert plan.deletes[0]["reason"] == "duplicate_canonical"


def test_anonymous_and_unparseable_rows_are_reported_not_touched() -> None:
    anon_body, _ = _body_and_deterministic({"Company Name": "Anon Co"})
    rows = [
        recon.Row("rec-anon", "rb2b:evt_" + "2" * 32, anon_body),
        recon.Row("rec-bad", "rb2b:evt_" + "3" * 32, "not json"),
    ]
    plan = recon.plan_reconciliation(rows)

    assert plan.deletes == []
    assert plan.promotions == []
    assert [r["record_id"] for r in plan.skipped_anonymous] == ["rec-anon"]
    assert [r["record_id"] for r in plan.unparseable] == ["rec-bad"]


def test_independent_visits_do_not_interfere() -> None:
    body_a, det_a = _body_and_deterministic(_IDENTIFIED_VISIT)
    body_b, det_b = _body_and_deterministic(
        {**_IDENTIFIED_VISIT, "Business Email": "grace@example.com"},
    )
    assert det_a != det_b
    rows = [
        recon.Row("a-canon", det_a, body_a),
        recon.Row("a-old", "rb2b:evt_" + "4" * 32, body_a),
        recon.Row("b-canon", det_b, body_b),
    ]
    plan = recon.plan_reconciliation(rows)

    assert len(plan.deletes) == 1
    assert plan.deletes[0]["record_id"] == "a-old"
    assert plan.noop_count == 1  # the lone b-canon row


# --------------------------------------------------------------------------- #
# fetch_rows limit ordering + CLI guardrails
# --------------------------------------------------------------------------- #


class _FakeRecord:
    def __init__(self, record_id: str) -> None:
        self.id = type("Id", (), {"record_id": record_id})()
        self._values = {
            "external_id": [{"value": f"rb2b:evt_{record_id}"}],
            "body": [{"value": "{}"}],
        }

    def model_dump(self) -> dict[str, Any]:
        return {"values": self._values}


class _FakeResp:
    def __init__(self, data: list[_FakeRecord]) -> None:
        self.data = data


class _FakeRecords:
    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records

    def post_v2_objects_object_records_query(self, **_kwargs: Any) -> _FakeResp:
        # One page, smaller than the page size, so paging stops after it.
        return _FakeResp(self._records)


class _FakeClient:
    """Minimal stand-in: one short page of records, ignores filter/offset."""

    def __init__(self, count: int) -> None:
        self.records = _FakeRecords([_FakeRecord(str(i)) for i in range(count)])


def test_fetch_rows_limit_zero_scans_nothing() -> None:
    # Guards the off-by-one: the cap must be checked before appending.
    assert recon.fetch_rows(_FakeClient(5), 0) == []


def test_fetch_rows_limit_caps_exactly() -> None:
    rows = recon.fetch_rows(_FakeClient(5), 3)
    assert [r.record_id for r in rows] == ["0", "1", "2"]


def test_apply_with_limit_is_rejected() -> None:
    with pytest.raises(SystemExit):
        recon.main(["--apply", "--limit", "5"])


def test_nonpositive_limit_is_rejected() -> None:
    with pytest.raises(SystemExit):
        recon.main(["--limit", "0"])
