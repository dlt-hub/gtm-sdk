# trunk-ignore-all(pyright/reportPrivateUsage,pyright/reportUnusedFunction): test fixtures legitimately reach into the structured logger's contextvars to isolate state
from __future__ import annotations

import re
from typing import Any

import orjson
import pytest
from fastapi import Request

from libs.logging import structured
from libs.logging.structured import (
    extract_or_generate_request_id,
    get_request_id,
    get_source,
    log,
    set_request_id,
    set_source,
    webhook_request_context,
)


def _read_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    if not out:
        return []
    return [orjson.loads(line) for line in out.splitlines()]


def _make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "headers": raw_headers,
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)  # pyright: ignore[reportArgumentType]


@pytest.fixture(autouse=True)
def _isolated_contextvars() -> Any:
    # Pytest runs tests in a single asyncio-free thread, so contextvars set in
    # one test (or at module-import time by something like
    # ``src/app.py:set_source(MODAL_APP)``) leak into the next. Snapshot and
    # **clear** at the start so each test sees a clean slate; restore the
    # snapshot on teardown so we don't disturb anything that ran before.
    prev_request_id = structured._REQUEST_ID.get()
    prev_source = structured._SOURCE.get()
    structured._REQUEST_ID.set(None)
    structured._SOURCE.set(None)
    try:
        yield
    finally:
        structured._REQUEST_ID.set(prev_request_id)
        structured._SOURCE.set(prev_source)


def test_log_emits_json_with_standard_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log("test.event", foo="bar", count=3)
    [payload] = _read_lines(capsys)
    assert payload["event"] == "test.event"
    assert payload["foo"] == "bar"
    assert payload["count"] == 3
    assert "ts" in payload
    assert "source" in payload
    assert "request_id" in payload


def test_log_uses_contextvars_for_source_and_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_source("rb2b")
    set_request_id("abc-123")
    log("test.event")
    [payload] = _read_lines(capsys)
    assert payload["source"] == "rb2b"
    assert payload["request_id"] == "abc-123"


def test_log_does_not_raise_on_unserializable_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Unserialisable:
        pass

    log("test.event", obj=Unserialisable())
    [payload] = _read_lines(capsys)
    # Fallback path drops the bad field but keeps the event recognisable.
    assert payload["event"] == "test.event"
    assert "log_error" in payload


def test_extract_or_generate_request_id_uses_header_when_present() -> None:
    request = _make_request({"X-Request-Id": "known-id"})
    assert extract_or_generate_request_id(request) == "known-id"


def test_extract_or_generate_request_id_falls_back_to_uuid7() -> None:
    request = _make_request()
    generated = extract_or_generate_request_id(request)
    # uuid7 string format: 8-4-4-4-12 hex. version nibble == 7.
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}",
        generated,
    )


def test_webhook_request_context_resets_on_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _make_request({"X-Request-Id": "ctx-id"})
    assert get_request_id() is None
    with webhook_request_context(request) as rid:
        assert rid == "ctx-id"
        assert get_request_id() == "ctx-id"
        log("test.event")
    assert get_request_id() is None
    [payload] = _read_lines(capsys)
    assert payload["request_id"] == "ctx-id"


def test_set_source_persists_in_current_context() -> None:
    assert get_source() is None
    set_source("attio")
    assert get_source() == "attio"


def test_log_includes_iso8601_timestamp_with_timezone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log("test.event")
    [payload] = _read_lines(capsys)
    assert payload["ts"].endswith("+00:00")


class _CaptureLogger:
    """OTLP logger stand-in that records every emit so severity tests can
    assert on the LogRecord without spinning up a real exporter."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def emit(self, record: Any) -> None:
        self.records.append(record)


@pytest.fixture
def _otlp_capture(monkeypatch: pytest.MonkeyPatch) -> _CaptureLogger:
    import libs.telemetry as telemetry_module

    capture = _CaptureLogger()

    def _always_return_capture(_name: str | None = None) -> _CaptureLogger:
        return capture

    monkeypatch.setattr(telemetry_module, "get_otlp_logger", _always_return_capture)
    return capture


@pytest.mark.parametrize(
    ("event", "expected_severity"),
    [
        # Webhook taxonomy (legacy).
        ("webhook.received", "INFO"),
        ("webhook.validation_failed", "WARN"),
        ("webhook.error", "ERROR"),
        # Generalized suffix patterns — non-webhook emitters get useful
        # severities without per-event-name maintenance.
        ("enrichment.failed", "ERROR"),
        ("attio.handler_exception", "ERROR"),
        ("apollo.warning", "WARN"),
        ("enrichment.skipped", "WARN"),
        # Untagged events default to INFO.
        ("enrichment.started", "INFO"),
        ("apollo.lookup_succeeded", "INFO"),
    ],
)
def test_log_otlp_severity_inference_generalizes_beyond_webhooks(
    event: str,
    expected_severity: str,
    _otlp_capture: _CaptureLogger,
) -> None:
    """Severity classification is suffix-based so any emitter — webhook
    handlers, src/attio/export.py, src/enrichment.py, future call sites —
    gets useful OTLP severities without adding per-event-name special
    cases. Errors must still alert; warnings must still warn."""
    set_source("severity-test")
    set_request_id("req-severity")
    log(event)
    assert len(_otlp_capture.records) == 1
    assert _otlp_capture.records[0].severity_text == expected_severity, (
        f"event {event!r}: expected {expected_severity}, "
        f"got {_otlp_capture.records[0].severity_text}"
    )


def test_log_otlp_severity_honors_explicit_status_error_field(
    _otlp_capture: _CaptureLogger,
) -> None:
    """The webhook.completed taxonomy maps to ERROR when ``status='error'``
    is set as a field; preserve that contract for non-suffix-matched
    events that still convey failure via a status field."""
    set_source("severity-status-test")
    set_request_id("req-status")
    log("webhook.completed", status="error")
    assert _otlp_capture.records[0].severity_text == "ERROR"


def test_module_export_surface_matches_documented_api() -> None:
    # Guard against accidental rename of the public functions documented in
    # design/backlog-202605171803-webhook_structured_logs-plan-01.md.
    for name in (
        "log",
        "set_source",
        "set_request_id",
        "get_request_id",
        "extract_or_generate_request_id",
        "webhook_request_context",
    ):
        assert hasattr(structured, name), name
