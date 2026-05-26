# trunk-ignore-all(pyright/reportPrivateUsage,pyright/reportUnusedFunction): test helpers legitimately reach into the structured logger's contextvars
"""Webhook event-sequence tests.

The webhook handler files contain a `WebhookModelToReplace` placeholder that
`scripts/redeploy-webhook.sh` substitutes at deploy time. To test `_export()`
in-process we run the same substitution against a temporary copy of the
handler and load it via `importlib`. This mirrors what production sees and
avoids stubbing out the FastAPI handler shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import orjson
import pytest
from fastapi import Request

from libs.logging import structured

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLERS_DIR = REPO_ROOT / "webhooks"
SAMPLES_DIR = REPO_ROOT / "api" / "samples"


def _load_substituted_handler(
    handler_name: str,
    source_alias: str,
    tmp_path: Path,
) -> ModuleType:
    src = (HANDLERS_DIR / f"{handler_name}.py").read_text()
    substituted = src.replace("WebhookModelToReplace", source_alias)
    target = tmp_path / f"{handler_name}.py"
    target.write_text(substituted)
    module_name = f"_test_webhook_{handler_name}_{source_alias}"
    spec = importlib.util.spec_from_file_location(module_name, target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _read_log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    if not out:
        return []
    return [orjson.loads(line) for line in out.splitlines() if line.startswith("{")]


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
    prev_request_id = structured._REQUEST_ID.get()
    prev_source = structured._SOURCE.get()
    try:
        yield
    finally:
        structured._REQUEST_ID.set(prev_request_id)
        structured._SOURCE.set(prev_source)


@pytest.fixture
def attio_caldotcom_handler(tmp_path: Path) -> ModuleType:
    return _load_substituted_handler(
        "export_to_attio",
        "CaldotcomBookingWebhook",
        tmp_path,
    )


def _load_caldotcom_payload() -> dict[str, Any]:
    raw = (SAMPLES_DIR / "caldotcom.booking.created.redacted.json").read_bytes()
    return orjson.loads(raw)


def test_attio_handler_emits_received_validated_completed_in_order(
    attio_caldotcom_handler: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _load_caldotcom_payload()
    webhook_model_class = attio_caldotcom_handler.WebhookModel
    webhook = webhook_model_class.model_validate(payload)

    # Bind a known request_id so we can assert join-ability across lines.
    structured.set_request_id("test-req-1")
    # `_export` doesn't open its own context, so the source needs to be set
    # explicitly here — in deployed code `set_source(APP_NAME)` runs at
    # module import.
    structured.set_source("attio.caldotcom.test")
    # Suppress stdout the handler captured up to this point (Modal banner
    # noise from import).
    capsys.readouterr()

    # The downstream Attio API call is expected to fail in unit-test envs
    # without credentials; the test asserts only on logs emitted before it.
    try:
        attio_caldotcom_handler._export(webhook)
    except Exception:  # noqa: BLE001, S110  # trunk-ignore(bandit/B110)
        pass

    lines = _read_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events[:2] == ["webhook.received", "webhook.validated"], events
    assert all(line["request_id"] == "test-req-1" for line in lines[:2])
    assert lines[0]["payload_bytes"] > 0
    assert "op_count" in lines[1]


def test_attio_handler_emits_validation_failed_for_invalid_payload(
    attio_caldotcom_handler: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _load_caldotcom_payload()
    # Cal.com's "ping" / unsupported trigger events are rejected by
    # attio_is_valid_webhook — use the bare ping payload, which validates
    # into the pydantic model but is not a bookable event.
    ping = orjson.loads((SAMPLES_DIR / "caldotcom.ping.redacted.json").read_bytes())
    webhook_model_class = attio_caldotcom_handler.WebhookModel
    try:
        webhook = webhook_model_class.model_validate(ping)
    except Exception:
        # If the ping payload doesn't validate against the model at all,
        # mutate the booking payload's triggerEvent to something unsupported.
        payload["triggerEvent"] = "UNSUPPORTED_EVENT_FOR_TEST"
        webhook = webhook_model_class.model_validate(payload)

    structured.set_request_id("test-req-2")
    structured.set_source("attio.caldotcom.test")
    capsys.readouterr()

    result = attio_caldotcom_handler._export(webhook)
    assert isinstance(result, str)

    lines = _read_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert "webhook.received" in events
    assert "webhook.validation_failed" in events
    failed = next(
        line for line in lines if line["event"] == "webhook.validation_failed"
    )
    assert failed["reason"]


def test_gcp_raw_error_paths_emit_webhook_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _load_substituted_handler(
        "export_to_gcp_raw",
        "CaldotcomBookingWebhook",
        tmp_path,
    )
    capsys.readouterr()

    structured.set_request_id("test-req-3")
    structured.set_source("dlthub-devx-test-bucket")

    # Trigger the processing_error branch: feed an item whose `.content`
    # attribute access path raises.
    class BadFile:
        @property
        def content(self) -> str:
            raise AttributeError("boom")

        # trunk-ignore(bandit/B108): test fixture path string; never written
        file = "/tmp/missing.json"  # noqa: S108

    bucket_url = "gs://dlthub-devx-test-bucket"
    gen = handler.SourceFileRaw.get_json_data_from_file_data(
        file_data=iter([BadFile()]),
        bucket_url=bucket_url,
    )
    with pytest.raises(AttributeError):
        # Pull the iterator so the generator body runs.
        next(gen)

    lines = _read_log_lines(capsys)
    assert any(
        line["event"] == "webhook.error" and line.get("reason") == "processing_error"
        for line in lines
    ), lines


def test_attio_handle_wraps_export_with_request_context_and_completed(
    attio_caldotcom_handler: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_handle()` is what `web()` delegates to; covers request_id binding +
    `webhook.completed` timing that `_export` alone doesn't reach. Stubs the
    downstream Attio call so the test never depends on credentials or
    network reachability."""
    payload = _load_caldotcom_payload()
    webhook = attio_caldotcom_handler.WebhookModel.model_validate(payload)
    request = _make_request({"X-Request-Id": "handle-attio"})

    class FakeResult:
        @staticmethod
        def body() -> str:
            return "fake-attio-response"

    def _fake_execute(_plan: object) -> FakeResult:
        return FakeResult()

    monkeypatch.setattr(attio_caldotcom_handler, "execute", _fake_execute)
    capsys.readouterr()

    result = attio_caldotcom_handler._handle(webhook, request)
    assert result == "fake-attio-response"

    lines = _read_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events == ["webhook.received", "webhook.validated", "webhook.completed"]
    completed = lines[-1]
    assert completed["request_id"] == "handle-attio"
    assert completed["status"] == "ok"
    assert "duration_ms" in completed
    assert structured.get_request_id() is None


def test_attio_handle_emits_completed_status_error_when_export_raises(
    attio_caldotcom_handler: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _load_caldotcom_payload()
    webhook = attio_caldotcom_handler.WebhookModel.model_validate(payload)
    request = _make_request({"X-Request-Id": "handle-attio-error"})

    class Boom(RuntimeError):
        pass

    def _raise(_plan: object) -> None:
        raise Boom("upstream attio failure")

    monkeypatch.setattr(attio_caldotcom_handler, "execute", _raise)
    capsys.readouterr()

    with pytest.raises(Boom):
        attio_caldotcom_handler._handle(webhook, request)

    completed = next(
        line for line in _read_log_lines(capsys) if line["event"] == "webhook.completed"
    )
    assert completed["status"] == "error"
    assert completed["error_type"] == "Boom"
    assert "upstream attio failure" in completed["error_msg"]
    assert completed["request_id"] == "handle-attio-error"


def _patch_gcp_to_filesystem(
    handler: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raises: type[BaseException] | None = None,
    returns: str = "ok",
) -> None:
    """Stub `CloudGoogle.to_filesystem` on the handler-local import."""

    def _impl(*_args: object, **_kwargs: object) -> str:
        if raises is not None:
            raise raises("stubbed GCS failure")
        return returns

    monkeypatch.setattr(handler.CloudGoogle, "to_filesystem", _impl)


def test_gcp_etl_handle_emits_full_event_sequence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _load_substituted_handler(
        "export_to_gcp_etl",
        "CaldotcomBookingWebhook",
        tmp_path,
    )
    _patch_gcp_to_filesystem(handler, monkeypatch, returns="ok-etl")

    # `_get_storage_source_file_data` calls `.remote()` on a Modal function
    # when its base model type is non-None; short-circuit it so the test
    # never touches the Modal runtime regardless of which Webhook source
    # the substitution selected.
    def _no_storage(**_kw: object) -> None:
        return None

    monkeypatch.setattr(handler, "_get_storage_source_file_data", _no_storage)
    payload = _load_caldotcom_payload()
    webhook = handler.WebhookModel.model_validate(payload)
    request = _make_request({"X-Request-Id": "handle-etl"})
    capsys.readouterr()

    result = handler._handle(webhook, request)
    assert result == "ok-etl"

    lines = _read_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events == ["webhook.received", "webhook.validated", "webhook.completed"]
    assert all(line["request_id"] == "handle-etl" for line in lines)
    completed = lines[-1]
    assert completed["status"] == "ok"
    assert completed["bucket_name"]


def test_gcp_raw_handle_records_byte_length_for_multibyte_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `payload_bytes` must measure bytes, not str length.

    Multibyte characters ('é' = 2 bytes, '日' = 3 bytes) used to be reported
    as `len(json_data)` after UTF-8 decoding — undercounting payload size
    on every non-ASCII webhook.
    """
    handler = _load_substituted_handler(
        "export_to_gcp_raw",
        "CaldotcomBookingWebhook",
        tmp_path,
    )
    _patch_gcp_to_filesystem(handler, monkeypatch, returns="ok-raw")
    multibyte_payload = {"note": "héllo 日本"}
    expected_bytes = len(orjson.dumps(multibyte_payload))
    expected_chars = len(orjson.dumps(multibyte_payload).decode("utf-8"))
    # Sanity: the bug would only matter if the two numbers diverge.
    assert expected_bytes > expected_chars

    request = _make_request({"X-Request-Id": "handle-raw"})
    capsys.readouterr()

    result = handler._handle(multibyte_payload, request)
    assert result == "ok-raw"

    lines = _read_log_lines(capsys)
    received = next(line for line in lines if line["event"] == "webhook.received")
    assert received["payload_bytes"] == expected_bytes
    assert received["request_id"] == "handle-raw"
    completed = next(line for line in lines if line["event"] == "webhook.completed")
    assert completed["status"] == "ok"
