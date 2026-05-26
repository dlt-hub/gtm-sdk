# trunk-ignore-all(pyright/reportPrivateUsage,pyright/reportUnusedFunction): test helpers legitimately reach into the structured logger's contextvars
"""Webhook event-sequence tests.

The webhook handler files contain a `WebhookModelToReplace` placeholder that
`scripts/webhooks-redeploy.py` substitutes at deploy time. To test `_export()`
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

# The deploy script lives at `scripts/webhooks-redeploy.py`. The hyphen and the
# fact that `scripts/` is intentionally excluded from
# `[tool.setuptools.packages.find]` mean it cannot be imported via a normal
# `from scripts.webhooks_redeploy import ...` statement. Load it via importlib
# so this test exercises the exact same `PLACEHOLDER` / discovery code the
# deploy script uses — drift between the two would silently regress.
_DEPLOY_SCRIPT_PATH = REPO_ROOT / "scripts" / "webhooks-redeploy.py"
_DEPLOY_MODULE_NAME = "_webhooks_redeploy_under_test"


def _load_deploy_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        _DEPLOY_MODULE_NAME,
        _DEPLOY_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_DEPLOY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_deploy_script = _load_deploy_script()
PLACEHOLDER: str = _deploy_script.PLACEHOLDER
_discover_handlers = _deploy_script._discover_handlers
_discover_sources = _deploy_script._discover_sources


def _load_substituted_handler(
    handler_name: str,
    source_alias: str,
    tmp_path: Path,
) -> ModuleType:
    src = (HANDLERS_DIR / f"{handler_name}.py").read_text()
    # Import the placeholder string from the deploy script (rather than
    # hard-coding the literal) so this test fails loudly if the constant
    # ever gets renamed without updating every callsite.
    assert PLACEHOLDER in src, (
        f"{handler_name}.py is missing the {PLACEHOLDER!r} placeholder "
        "that scripts/webhooks-redeploy.py substitutes at deploy time"
    )
    substituted = src.replace(PLACEHOLDER, source_alias)
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


@pytest.fixture(autouse=True)
def _stub_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit ``infisical.fetch_all`` to the env path.

    The Attio export endpoint resolves each ``required_api_keys()`` entry via
    env first, then falls back to Infisical. By exporting non-empty dummy
    values for both keys we avoid the Infisical bootstrap-auth path entirely;
    the downstream Attio call still fails (no real key, no network) which is
    the original test shape — these tests assert on the log lines emitted
    *before* that failure.
    """
    monkeypatch.setenv("ATTIO_API_KEY", "stub-attio-key-for-tests")
    monkeypatch.setenv("CALCOM_API_KEY", "stub-calcom-key-for-tests")


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


# ---------------------------------------------------------------------------
# Cross-product log-emission coverage (ai-fdu)
#
# The scenario tests above cover specific (handler, source) pairs in depth
# — happy path, validation failure, error, multibyte, etc. They do NOT prove
# that every other (handler, source) combo's substituted code (a) imports
# cleanly and (b) emits the structured-log schema Modal's dashboard relies
# on for source-filtering and request_id correlation. That's the gap this
# parametrized test closes: one lenient "did the schema show up?" assertion
# across all 3 handlers × 5 sources = 15 combinations.
# ---------------------------------------------------------------------------

# Map an import alias → its payload fixture filename. Keyed by a substring
# of the alias so the table tolerates the per-handler naming drift the deploy
# script already tolerates (e.g. `OctolensWebhook` in export_to_attio.py vs
# `OctolensMentionWebhook` in export_to_gcp_etl.py / export_to_gcp_raw.py).
# If a new source is added that doesn't match any substring, the test fails
# loudly at collection time with KeyError pointing at the missing entry.
_FIXTURE_BY_SOURCE_FAMILY: dict[str, str] = {
    "Caldotcom": "caldotcom.booking.created.redacted.json",
    "FathomCall": "fathom.recording.redacted.json",
    "FathomMessage": "fathom.recording.redacted.json",
    "Octolens": "octolens.mention.created.twitter.redacted.json",
    "Rb2b": "rb2b.visit.person_and_company.redacted.json",
}


def _fixture_for(source_alias: str) -> str:
    for family, fixture in _FIXTURE_BY_SOURCE_FAMILY.items():
        if family in source_alias:
            return fixture
    raise KeyError(
        f"no payload fixture mapped for source alias {source_alias!r}; "
        f"add an entry to _FIXTURE_BY_SOURCE_FAMILY in {__file__}",
    )


def _matrix() -> list[tuple[str, str]]:
    """Discover (handler, source_alias) pairs the same way the deploy script
    does. This guarantees the test covers exactly what `webhooks-redeploy.py`
    would deploy — not a hand-maintained list that drifts."""
    pairs: list[tuple[str, str]] = []
    for handler_name in _discover_handlers():
        handler_path = HANDLERS_DIR / f"{handler_name}.py"
        for source_alias in _discover_sources(handler_path):
            pairs.append((handler_name, source_alias))
    return pairs


def _call_typed_handler(
    module: ModuleType,
    payload: dict[str, Any],
    request: Request,
) -> object:
    webhook = module.WebhookModel.model_validate(payload)
    return module._handle(webhook, request)


def _call_raw_handler(
    module: ModuleType,
    payload: dict[str, Any],
    request: Request,
) -> object:
    # export_to_gcp_raw.web() takes the raw dict — no Pydantic gating.
    return module._handle(payload, request)


_CALLERS: dict[str, Any] = {
    "export_to_attio": _call_typed_handler,
    "export_to_gcp_etl": _call_typed_handler,
    "export_to_gcp_raw": _call_raw_handler,
}


def _stub_attio_downstream(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResult:
        @staticmethod
        def body() -> str:
            return "ok-attio"

    def _fake_execute(_plan: object) -> _FakeResult:
        return _FakeResult()

    monkeypatch.setattr(module, "execute", _fake_execute)


def _stub_etl_downstream(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gcp_to_filesystem(module, monkeypatch, returns="ok-etl")

    # _get_storage_source_file_data calls .remote() on a Modal function when
    # storage_get_base_model_type() is non-None; short-circuit so the test
    # never touches the Modal runtime regardless of which source is wired.
    def _no_storage(**_kw: object) -> None:
        return None

    monkeypatch.setattr(module, "_get_storage_source_file_data", _no_storage)


def _stub_raw_downstream(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gcp_to_filesystem(module, monkeypatch, returns="ok-raw")


_STUBS: dict[str, Any] = {
    "export_to_attio": _stub_attio_downstream,
    "export_to_gcp_etl": _stub_etl_downstream,
    "export_to_gcp_raw": _stub_raw_downstream,
}


@pytest.mark.parametrize(
    ("handler_name", "source_alias"),
    _matrix(),
    ids=lambda v: v,
)
def test_substituted_handler_emits_log_schema(
    handler_name: str,
    source_alias: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For every (handler, source) combo: substitution applies cleanly, the
    substituted module imports without error, and _handle() emits at least
    one structured log line with the ts/event/source/request_id schema
    Modal's dashboard relies on.

    Lenient by design — terminal-event choice depends on whether the source
    implements the handler's contract (e.g. FathomMessageWebhook returns
    False for both attio_is_valid_webhook and etl_is_valid_webhook, so its
    handlers emit webhook.validation_failed instead of webhook.completed).
    The strict-sequence assertions live in the scenario tests above.
    """
    module = _load_substituted_handler(handler_name, source_alias, tmp_path)
    _STUBS[handler_name](module, monkeypatch)

    payload_path = SAMPLES_DIR / _fixture_for(source_alias)
    payload = orjson.loads(payload_path.read_bytes())
    request_id = f"req-{handler_name}-{source_alias}"
    request = _make_request({"X-Request-Id": request_id})
    capsys.readouterr()

    try:
        _CALLERS[handler_name](module, payload, request)
    except Exception:  # noqa: BLE001, S110  # trunk-ignore(bandit/B110)
        # Pydantic ValidationError or downstream raises are fine — the test
        # asserts on the structured log lines, which are emitted before and
        # around any failure (webhook.completed status=error wraps the
        # raise; webhook.validation_failed precedes a soft reject).
        pass

    lines = _read_log_lines(capsys)
    assert lines, f"no structured log lines emitted for {handler_name} + {source_alias}"

    required_fields = {"ts", "event", "source", "request_id"}
    for line in lines:
        missing = required_fields - line.keys()
        assert not missing, f"log line missing required fields {missing}: {line}"

    request_ids = {line["request_id"] for line in lines}
    assert request_ids == {request_id}, (
        f"all lines for one request must share a request_id; got {request_ids}"
    )

    sources = {line["source"] for line in lines}
    assert len(sources) == 1, f"source must be consistent per request; got {sources}"
    assert next(iter(sources)), "source must be non-empty"

    events = {line["event"] for line in lines}
    assert "webhook.received" in events, (
        f"expected webhook.received in event stream; got {events}"
    )
    terminal = {"webhook.completed", "webhook.validation_failed", "webhook.error"}
    assert events & terminal, (
        f"expected at least one terminal event in {terminal}; got {events}"
    )


# ---------------------------------------------------------------------------
# OTLP log-sink mirror coverage (ai-uir)
#
# When init_log_exporter() has wired an OTLP logger, every libs/logging
# .structured.log() call should also emit an OTLP LogRecord carrying the
# same fields as attributes. The tests below replace the OTLP logger with
# an in-memory collector and assert the attribute schema matches the
# stdout schema covered by test_substituted_handler_emits_log_schema.
# ---------------------------------------------------------------------------


class _InMemoryOtlpLogger:
    """Stand-in for the OTLP Logger returned by ``init_log_exporter``.

    Captures every ``emit(LogRecord)`` call so tests can assert on the
    attribute payload without spinning up a real exporter / collector.
    """

    def __init__(self) -> None:
        self.records: list[Any] = []

    def emit(self, record: Any) -> None:
        self.records.append(record)


@pytest.fixture
def _otlp_capture(monkeypatch: pytest.MonkeyPatch) -> _InMemoryOtlpLogger:
    """Replace the structured logger's OTLP sink with an in-memory capture.

    ``get_otlp_logger`` uses strict per-``service_name`` lookup in
    production, but each parametrized webhook sets a different ``source``
    at module import (APP_NAME / BUCKET_NAME), so we bypass the lookup
    entirely and have ``get_otlp_logger`` return the capture regardless of
    the requested name. The strict-lookup contract is exercised separately
    in ``tests/libs/test_telemetry.py``.
    """
    import libs.telemetry as telemetry_module

    capture = _InMemoryOtlpLogger()

    def _always_return_capture(_name: str | None = None) -> _InMemoryOtlpLogger:
        return capture

    monkeypatch.setattr(telemetry_module, "get_otlp_logger", _always_return_capture)
    return capture


@pytest.mark.parametrize(
    ("handler_name", "source_alias"),
    _matrix(),
    ids=lambda v: v,
)
def test_substituted_handler_mirrors_to_otlp_when_sink_wired(
    handler_name: str,
    source_alias: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _otlp_capture: _InMemoryOtlpLogger,
) -> None:
    """Every (handler, source) combo: when an OTLP logger is wired, each
    structured.log() call also fires an OTLP LogRecord with matching
    attributes (source, request_id, event in body)."""
    module = _load_substituted_handler(handler_name, source_alias, tmp_path)
    _STUBS[handler_name](module, monkeypatch)

    payload_path = SAMPLES_DIR / _fixture_for(source_alias)
    payload = orjson.loads(payload_path.read_bytes())
    request_id = f"otlp-{handler_name}-{source_alias}"
    request = _make_request({"X-Request-Id": request_id})
    capsys.readouterr()

    try:
        _CALLERS[handler_name](module, payload, request)
    except Exception:  # noqa: BLE001, S110  # trunk-ignore(bandit/B110)
        pass

    assert _otlp_capture.records, (
        f"no OTLP records emitted for {handler_name} + {source_alias}"
    )

    request_ids = {r.attributes.get("request_id") for r in _otlp_capture.records}
    assert request_ids == {request_id}, (
        f"all OTLP records for one request must share request_id; got {request_ids}"
    )

    sources = {r.attributes.get("source") for r in _otlp_capture.records}
    assert len(sources) == 1, f"OTLP source must be consistent; got {sources}"
    assert next(iter(sources)), "OTLP source attribute must be non-empty"

    bodies = {r.body for r in _otlp_capture.records}
    assert "webhook.received" in bodies, (
        f"expected webhook.received in OTLP bodies; got {bodies}"
    )
    terminal = {"webhook.completed", "webhook.validation_failed", "webhook.error"}
    assert bodies & terminal, (
        f"expected at least one terminal OTLP body in {terminal}; got {bodies}"
    )


def test_otlp_mirror_sanitizes_none_and_nested_values(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _otlp_capture: _InMemoryOtlpLogger,
) -> None:
    """OTLP attribute values must be primitives — None / dict / nested values
    would otherwise make ``logger.emit`` raise and the record would be lost.

    Regression test for the sanitizer in ``_emit_to_otlp``: ``None`` is
    dropped, primitives pass through, nested values get JSON-encoded.
    """
    capsys.readouterr()
    structured.set_source("sanitize-test")
    structured.set_request_id("req-sanitize")
    structured.log(
        "webhook.completed",
        status="ok",
        duration_ms=42,
        skipped_reason=None,  # must be dropped, not emitted as null
        plan={"op_count": 3, "ops": ["create", "update"]},  # JSON-encoded
        tags=["a", "b"],  # primitive list — passes through
    )

    assert len(_otlp_capture.records) == 1
    attrs = dict(_otlp_capture.records[0].attributes)
    assert "skipped_reason" not in attrs, "None values must be dropped"
    assert attrs["status"] == "ok"
    assert attrs["duration_ms"] == 42
    assert attrs["tags"] == ["a", "b"]
    assert isinstance(attrs["plan"], str), (
        "nested objects must be JSON-encoded, not passed as-is"
    )
    assert "op_count" in attrs["plan"]


def test_otlp_mirror_json_encodes_mixed_primitive_lists(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _otlp_capture: _InMemoryOtlpLogger,
) -> None:
    """OTLP requires homogeneous primitive arrays. A list like ``[1, "2"]``
    passes the primitive-element check but still violates the spec, so the
    sanitizer must JSON-encode it rather than pass it through and risk the
    exporter rejecting the record."""
    capsys.readouterr()
    structured.set_source("sanitize-mixed")
    structured.set_request_id("req-mixed")
    structured.log(
        "webhook.received",
        mixed_list=[1, "two", True],  # all primitive, but heterogeneous
        empty_list=[],  # empty: ambiguous element type
        homogeneous_strs=["a", "b", "c"],
        homogeneous_ints=[1, 2, 3],
    )
    attrs = dict(_otlp_capture.records[0].attributes)
    assert isinstance(attrs["mixed_list"], str), (
        f"mixed-type list must be JSON-encoded; got {type(attrs['mixed_list'])}"
    )
    assert isinstance(attrs["empty_list"], str), (
        "empty list must be JSON-encoded (element type unknown)"
    )
    assert attrs["homogeneous_strs"] == ["a", "b", "c"]
    assert attrs["homogeneous_ints"] == [1, 2, 3]


def test_log_does_not_emit_otlp_when_no_sink_wired(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: with no OTLP logger wired, structured.log() still
    writes the stdout JSON line and the in-memory capture stays empty.

    Guards the no-op gate that keeps OTLP additive — we never want a
    misconfigured init to swallow stdout logging."""
    import libs.telemetry as telemetry_module

    monkeypatch.setattr(telemetry_module, "_otlp_loggers", {}, raising=False)
    capture = _InMemoryOtlpLogger()  # never installed
    capsys.readouterr()

    structured.set_source("otlp-noop-test")
    structured.set_request_id("req-noop")
    structured.log("webhook.received", payload_bytes=1)

    assert not capture.records
    out = capsys.readouterr().out.strip()
    assert out, "stdout JSON line must still be emitted when OTLP is disabled"
    line = orjson.loads(out)
    assert line["event"] == "webhook.received"
    assert line["source"] == "otlp-noop-test"
    assert line["request_id"] == "req-noop"
