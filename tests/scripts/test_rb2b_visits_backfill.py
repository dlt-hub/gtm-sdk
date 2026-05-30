"""Offline tests for scripts/rb2b-visits-backfill.py.

Covers the network-free units: the dedup key / payload normalization, the
``map_record`` mapper against a known sample, and resume-log handling. The dlt
extract and the HTTP send are exercised live during the dev smoke test, not
here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from libs.rb2b import Webhook as Rb2bWebhook
from libs.rb2b import compute_event_id

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "rb2b-visits-backfill.py"
SAMPLES = REPO_ROOT / "api" / "samples"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("rb2b_visits_backfill", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module defines a dataclass with string
    # annotations (from __future__ import annotations), and dataclasses
    # resolves those by looking the module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_script_module()


def _sample(name: str) -> dict[str, Any]:
    return orjson.loads((SAMPLES / name).read_text())


# --------------------------------------------------------------------------- #
# unwrap_payload / dedup key
# --------------------------------------------------------------------------- #


def test_unwrap_payload_handles_envelope_and_flat() -> None:
    enveloped = _sample("rb2b.visit.person_and_company.redacted.json")
    inner = backfill.unwrap_payload(enveloped)
    assert inner["Business Email"] == "alice@example.test"
    # A flat payload is returned unchanged.
    assert backfill.unwrap_payload(inner) is inner


def test_dedup_key_matches_compute_event_id() -> None:
    """The script's dedup key must equal the live webhook's derived id."""
    enveloped = _sample("rb2b.visit.person_and_company.redacted.json")
    row = backfill._row(backfill.RB2B_CONFIG, enveloped, source="gcs")
    inner = backfill.unwrap_payload(enveloped)
    assert row["dedup_key"] == compute_event_id(inner)


def test_dedup_key_collides_across_envelope_and_flat() -> None:
    """The same visit archived flat (GCS) vs enveloped (Hookdeck) must collapse."""
    enveloped = _sample("rb2b.visit.person_and_company.redacted.json")
    flat = dict(enveloped["payload"])
    gcs_row = backfill._row(backfill.RB2B_CONFIG, enveloped, source="hookdeck")
    flat_row = backfill._row(backfill.RB2B_CONFIG, flat, source="gcs")
    assert gcs_row["dedup_key"] == flat_row["dedup_key"]


# --------------------------------------------------------------------------- #
# iter_json_objects
# --------------------------------------------------------------------------- #


def test_extract_hookdeck_body_prefers_parsed_then_body() -> None:
    payload = {"Captured URL": "u"}
    # parsed_body present as a dict.
    assert (
        backfill._extract_hookdeck_body({"data": {"parsed_body": payload}}) == payload
    )
    # body as a dict.
    assert backfill._extract_hookdeck_body({"data": {"body": payload}}) == payload
    # body as a JSON string.
    assert (
        backfill._extract_hookdeck_body({"data": {"body": json.dumps(payload)}})
        == payload
    )
    # A non-JSON body must not mask a usable parsed_body.
    assert (
        backfill._extract_hookdeck_body(
            {"data": {"parsed_body": payload, "body": "<<not json>>"}},
        )
        == payload
    )
    # Neither representation usable -> None.
    assert backfill._extract_hookdeck_body({"data": {"body": "<<not json>>"}}) is None
    assert backfill._extract_hookdeck_body({}) is None


def test_iter_json_objects_single_object() -> None:
    obj = {"Company Name": "Example Inc", "Seen At": "2026-05-14T09:45:00+00:00"}
    out = list(backfill.iter_json_objects(json.dumps(obj)))
    assert out == [obj]


def test_iter_json_objects_ndjson_and_empty() -> None:
    a = {"Captured URL": "https://example.test/a"}
    b = {"Captured URL": "https://example.test/b"}
    text = json.dumps(a) + "\n" + json.dumps(b)
    assert list(backfill.iter_json_objects(text)) == [a, b]
    assert list(backfill.iter_json_objects("   ")) == []


# --------------------------------------------------------------------------- #
# map_record
# --------------------------------------------------------------------------- #


def test_map_record_produces_valid_webhook_envelope() -> None:
    enveloped = _sample("rb2b.visit.person_and_company.redacted.json")
    row = backfill._row(backfill.RB2B_CONFIG, enveloped, source="gcs")
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    envelope = backfill.map_record(row, now=fixed_now)

    assert envelope["event_id"] == row["dedup_key"]
    # The sample is an envelope with connection "rb2b-direct" — preserved so the
    # replayed row matches what live ingestion produced.
    assert envelope["connection"] == "rb2b-direct"
    assert envelope["payload"]["Business Email"] == "alice@example.test"

    # The real model accepts it, preserves the explicit id, and the Attio
    # external_id is stable across runs.
    webhook = Rb2bWebhook.model_validate(envelope)
    assert webhook.event_id == row["dedup_key"]
    assert webhook.payload.captured_url == "https://example.test/pricing"


def test_map_record_connection_matches_live() -> None:
    """Flat archives fall back to the live default; envelopes keep their value."""
    flat = {"dedup_key": "evt_a", "raw_payload": json.dumps({"Captured URL": "u"})}
    assert backfill.map_record(flat)["connection"] == "rb2b-direct"

    enveloped = {
        "dedup_key": "evt_b",
        "raw_payload": json.dumps(
            {"connection": "rb2b-visits", "payload": {"Captured URL": "u"}},
        ),
    }
    assert backfill.map_record(enveloped)["connection"] == "rb2b-visits"


def test_map_record_uses_seen_at_for_timestamp() -> None:
    enveloped = _sample("rb2b.visit.person_and_company.redacted.json")
    row = backfill._row(backfill.RB2B_CONFIG, enveloped, source="gcs")
    envelope = backfill.map_record(row)
    # Seen At wins over a now() fallback.
    assert envelope["timestamp"].startswith("2026-05-14T09:45:00")


def test_map_record_falls_back_to_now_without_seen_at() -> None:
    row = {
        "dedup_key": "evt_x",
        "raw_payload": json.dumps({"Captured URL": "https://example.test/x"}),
    }
    fixed_now = datetime(2026, 3, 3, tzinfo=timezone.utc)
    envelope = backfill.map_record(row, now=fixed_now)
    assert envelope["timestamp"] == fixed_now.isoformat()


# --------------------------------------------------------------------------- #
# resume-log handling
# --------------------------------------------------------------------------- #


def test_load_sent_reads_keys(tmp_path: Path) -> None:
    log = tmp_path / "sent.log"
    log.write_text("evt_a\nevt_b\n\n")
    assert backfill._load_sent(log) == {"evt_a", "evt_b"}
    assert backfill._load_sent(tmp_path / "missing.log") == set()


def test_send_dry_run_skips_already_sent(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "rb2b_visits.jsonl"
    rows = [
        {"dedup_key": "evt_1", "raw_payload": json.dumps({"Captured URL": "u1"})},
        {"dedup_key": "evt_2", "raw_payload": json.dumps({"Captured URL": "u2"})},
    ]
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "sent.log").write_text("evt_1\n")

    cfg = backfill.BackfillConfig(
        name="t",
        raw_bucket="b",
        hookdeck_source_name="s",
        dedup_key_fn=compute_event_id,
        output_path=out,
        webhook_app_name="app",
    )
    sent, skipped, failed = backfill.send(
        cfg,
        webhook_url="https://example.test/hook",
        dry_run=True,
        limit=None,
        rate_limit_s=0,
    )
    assert (sent, skipped, failed) == (1, 1, 0)
    printed = capsys.readouterr().out
    assert "evt_2" in printed  # the not-yet-sent record was emitted
    assert "evt_1" not in printed  # the already-sent record was skipped


def _cfg_for(out: Path) -> Any:
    return backfill.BackfillConfig(
        name="t",
        raw_bucket="b",
        hookdeck_source_name="s",
        dedup_key_fn=compute_event_id,
        output_path=out,
        webhook_app_name="app",
    )


def test_send_logs_malformed_row_and_continues(tmp_path: Path) -> None:
    """One unparseable row must be logged to failed.jsonl, not abort the run."""
    out = tmp_path / "rb2b_visits.jsonl"
    rows = [
        # good: no Seen At -> envelope timestamp falls back to now() -> valid
        {"dedup_key": "evt_ok", "raw_payload": json.dumps({"Captured URL": "u"})},
        # bad: an unnormalizable Seen At -> envelope timestamp won't parse
        {
            "dedup_key": "evt_bad",
            "raw_payload": json.dumps({"Seen At": "not-a-date"}),
        },
    ]
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    sent, skipped, failed = backfill.send(
        _cfg_for(out),
        webhook_url="https://example.test/hook",
        dry_run=True,
        limit=None,
        rate_limit_s=0,
    )
    assert (sent, skipped, failed) == (1, 0, 1)
    failed_log = (tmp_path / "failed.jsonl").read_text()
    assert "evt_bad" in failed_log
    assert "evt_ok" not in failed_log


class _FakeResp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.text = f"HTTP {status} body"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return {"models": [], "ok": True}


def test_hookdeck_get_retries_on_429(monkeypatch: Any) -> None:
    """A 429 is retried (honoring Retry-After) instead of aborting extract."""
    responses = [
        _FakeResp(429, {"Retry-After": "0"}),
        _FakeResp(500),
        _FakeResp(200),
    ]
    calls = {"n": 0}

    def fake_get(*_a: Any, **_k: Any) -> _FakeResp:
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    slept: list[float] = []
    monkeypatch.setattr(backfill.requests, "get", fake_get)
    body = backfill._hookdeck_get("/events", "key", {}, sleep=slept.append)
    assert body == {"models": [], "ok": True}
    assert calls["n"] == 3  # 429, 500, then 200
    assert len(slept) == 2  # backed off before each retry


def test_retry_after_seconds_parses_both_forms() -> None:
    """Retry-After may be int seconds or an HTTP-date; neither should raise."""
    assert backfill._retry_after_seconds("12", 1.0) == 12.0
    assert backfill._retry_after_seconds(None, 3.0) == 3.0
    # A malformed value falls back rather than raising.
    assert backfill._retry_after_seconds("soon", 4.0) == 4.0
    # An HTTP-date in the past clamps to 0, not a ValueError.
    assert backfill._retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", 5.0) == 0.0


def test_post_with_retries_retries_429_honoring_retry_after(monkeypatch: Any) -> None:
    """A webhook 429 must retry (honoring Retry-After), not be treated terminal."""
    responses = [_FakeResp(429, {"Retry-After": "7"}), _FakeResp(200)]
    calls = {"n": 0}

    def fake_post(*_a: Any, **_k: Any) -> _FakeResp:
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    slept: list[float] = []
    monkeypatch.setattr(backfill.requests, "post", fake_post)
    backfill._post_with_retries(
        "https://example.test/hook",
        {"x": 1},
        sleep=slept.append,
    )
    assert calls["n"] == 2  # 429 then 200
    assert slept == [7.0]  # honored Retry-After, not the exponential default


def _noop_sleep(_seconds: float) -> None:
    return None


def test_resolve_source_id_pages_and_matches_exactly(monkeypatch: Any) -> None:
    """Resolver must page through all sources and match the name exactly."""
    pages = [
        {
            "models": [{"id": "src_a", "name": "rb2b-visit-mock"}],
            "pagination": {"next": "cur1"},
        },
        {"models": [{"id": "src_b", "name": "rb2b-visit"}], "pagination": {}},
    ]
    seq = iter(pages)

    def fake_get(_path: str, _key: str, _params: dict[str, Any]) -> dict[str, Any]:
        return next(seq)

    monkeypatch.setattr(backfill, "_hookdeck_get", fake_get)
    assert backfill._resolve_hookdeck_source_id("rb2b-visit", "key") == "src_b"


def test_resolve_source_id_raises_on_ambiguous(monkeypatch: Any) -> None:
    """Duplicate-named sources must raise rather than silently pick one."""
    page = {
        "models": [
            {"id": "src_a", "name": "rb2b-visit"},
            {"id": "src_b", "name": "rb2b-visit"},
        ],
        "pagination": {},
    }

    def fake_get(*_a: Any, **_k: Any) -> dict[str, Any]:
        return page

    monkeypatch.setattr(backfill, "_hookdeck_get", fake_get)
    try:
        backfill._resolve_hookdeck_source_id("rb2b-visit", "key")
    except RuntimeError as e:
        assert "2 sources" in str(e)
    else:
        raise AssertionError("expected RuntimeError on ambiguous match")


def test_post_with_retries_terminal_on_4xx(monkeypatch: Any) -> None:
    """Non-429 4xx is terminal — a malformed payload won't fix itself."""
    calls = {"n": 0}

    def fake_post(*_a: Any, **_k: Any) -> _FakeResp:
        calls["n"] += 1
        return _FakeResp(422)

    monkeypatch.setattr(backfill.requests, "post", fake_post)
    try:
        backfill._post_with_retries(
            "https://example.test/hook",
            {},
            sleep=_noop_sleep,
        )
    except RuntimeError as e:
        assert "422" in str(e)
    else:
        raise AssertionError("expected RuntimeError on 422")
    assert calls["n"] == 1  # no retry
