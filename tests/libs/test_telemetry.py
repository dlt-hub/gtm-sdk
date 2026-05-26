from __future__ import annotations

import libs.telemetry as telemetry_module
from libs.telemetry import (
    emit_cli_event,
    get_otlp_logger,
    init_log_exporter,
    init_tracer,
)


def test_init_tracer_noop_without_env(monkeypatch):
    monkeypatch.delenv("HYPERDX_API_KEY", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = init_tracer()
    assert tracer is None


def test_emit_cli_event_noop_without_init():
    # Should not raise even when tracer not initialized
    emit_cli_event("cli.test_event", {"key": "value"})


def test_init_tracer_returns_tracer_with_env(monkeypatch):
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    tracer = init_tracer()
    assert tracer is not None


def _reset_log_exporter(monkeypatch) -> None:
    """Clear the cached OTLP loggers so each test exercises a fresh init.

    ``init_log_exporter`` deliberately short-circuits for a ``service_name``
    that's already wired (prevents double-init across webhook container
    imports), so the per-service cache has to be cleared between tests that
    vary env var matrices.
    """
    monkeypatch.setattr(telemetry_module, "_otlp_loggers", {}, raising=False)


def test_init_log_exporter_noop_without_env(monkeypatch):
    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    assert init_log_exporter() is None
    assert get_otlp_logger() is None


def test_init_log_exporter_returns_logger_with_hyperdx_key(monkeypatch):
    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    # Init under the default service name and confirm get_otlp_logger picks
    # it up via that exact key. The no-arg lookup is now strict-None.
    logger = init_log_exporter("elvis-cli")
    assert logger is not None
    assert get_otlp_logger("elvis-cli") is logger
    assert get_otlp_logger() is None


def test_init_log_exporter_returns_logger_with_logs_endpoint(monkeypatch):
    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "http://localhost:4318/v1/logs",
    )
    logger = init_log_exporter()
    assert logger is not None


def test_init_log_exporter_returns_logger_with_base_endpoint(monkeypatch):
    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    # Base OTLP endpoint without a signal path — exporter should append /v1/logs.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    logger = init_log_exporter()
    assert logger is not None


def test_init_log_exporter_is_idempotent(monkeypatch):
    _reset_log_exporter(monkeypatch)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    first = init_log_exporter()
    second = init_log_exporter()
    assert first is second


def test_init_log_exporter_distinct_loggers_per_service_name(monkeypatch):
    """Two service names get two independent providers so a process that
    initializes more than one service doesn't attribute records to the
    wrong service.name Resource. Lookup is strict in both directions:
    an unknown name returns None, and a no-arg lookup returns None.
    Callers must bind the source contextvar to match their init key."""
    _reset_log_exporter(monkeypatch)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    a = init_log_exporter("service-a")
    b = init_log_exporter("service-b")
    assert a is not None
    assert b is not None
    assert a is not b
    assert get_otlp_logger("service-a") is a
    assert get_otlp_logger("service-b") is b
    # Unknown name -> None. Surfaces typos and source-contextvar drift.
    assert get_otlp_logger("unknown-source") is None
    # No-name lookup -> None. Eliminates the silent misattribution risk
    # where a source-less log() call would pick the first-registered
    # logger in a multi-service process.
    assert get_otlp_logger() is None


def test_get_otlp_logger_returns_none_without_init(monkeypatch):
    _reset_log_exporter(monkeypatch)
    assert get_otlp_logger() is None
    assert get_otlp_logger("any-name") is None


def test_init_log_exporter_installs_sigterm_bridge_when_default(monkeypatch):
    """SIGTERM with Python's default disposition skips atexit, so the OTLP
    batch buffer would be dropped on Modal container recycle. The bridge
    installs ``sys.exit(0)`` as the SIGTERM handler so atexit fires.

    Conditional: only installs when SIGTERM is at SIG_DFL — if another
    framework has claimed it (Modal runtime, pytest), we leave it alone.
    """
    import signal as signal_module

    import libs.telemetry as telemetry_module

    _reset_log_exporter(monkeypatch)
    monkeypatch.setattr(
        telemetry_module,
        "_sigterm_bridge_installed",
        False,
        raising=False,
    )

    def _getsignal_returns_default(_sig: int) -> object:
        return signal_module.SIG_DFL

    monkeypatch.setattr(signal_module, "getsignal", _getsignal_returns_default)

    installed: list[object] = []

    def _capture_signal(_sig: int, handler: object) -> object:
        installed.append(handler)
        return signal_module.SIG_DFL

    monkeypatch.setattr(signal_module, "signal", _capture_signal)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    init_log_exporter("sigterm-default-test")
    assert installed, "SIGTERM bridge must install when SIGTERM is at SIG_DFL"


def test_init_log_exporter_leaves_existing_sigterm_handler_alone(monkeypatch):
    """If another framework has installed a SIGTERM handler, we must NOT
    overwrite it. They presumably have their own graceful-shutdown path
    that will end up running our atexit."""
    import signal as signal_module

    import libs.telemetry as telemetry_module

    _reset_log_exporter(monkeypatch)
    monkeypatch.setattr(
        telemetry_module,
        "_sigterm_bridge_installed",
        False,
        raising=False,
    )

    def _existing_handler(_sig: int, _frame: object) -> None:
        pass

    def _getsignal_returns_existing(_sig: int) -> object:
        return _existing_handler

    monkeypatch.setattr(signal_module, "getsignal", _getsignal_returns_existing)

    installed: list[object] = []

    def _capture_signal(_sig: int, handler: object) -> object:
        installed.append(handler)
        return _existing_handler

    monkeypatch.setattr(signal_module, "signal", _capture_signal)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    init_log_exporter("sigterm-claimed-test")
    assert not installed, (
        "SIGTERM bridge must not overwrite an existing handler; "
        f"got installs={installed!r}"
    )


def test_init_log_exporter_registers_atexit_shutdown(monkeypatch):
    """The BatchLogRecordProcessor buffers records on a background thread;
    without an explicit shutdown the last batch is dropped when a short-
    lived CLI run exits or a Modal container is recycled. Verify atexit
    sees the registration so the buffer flushes on normal process exit."""
    from collections.abc import Callable

    _reset_log_exporter(monkeypatch)
    registered: list[Callable[..., object]] = []

    def _capture_atexit(
        fn: Callable[..., object],
        *_args: object,
        **_kwargs: object,
    ) -> Callable[..., object]:
        registered.append(fn)
        return fn

    monkeypatch.setattr("atexit.register", _capture_atexit)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-key")
    logger = init_log_exporter("atexit-test")
    assert logger is not None
    assert registered, "init_log_exporter must register an atexit shutdown"
    assert any(getattr(fn, "__name__", "") == "shutdown" for fn in registered), (
        f"expected shutdown handler in "
        f"{[getattr(f, '__name__', f) for f in registered]}"
    )


def test_init_log_exporter_normalizes_traces_endpoint_to_logs(monkeypatch):
    """Regression: a common operator footgun is setting
    ``OTEL_EXPORTER_OTLP_ENDPOINT=.../v1/traces`` (copy-paste from a traces
    example). Without normalization, the SDK would mangle this to
    ``.../v1/traces/v1/logs`` and OTLP log export would fail silently.
    The exporter must receive the logs-signal URL ``.../v1/logs``."""
    from unittest.mock import patch

    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "https://example.com/v1/traces",
    )

    with patch(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        autospec=True,
    ) as mock_exporter:
        init_log_exporter("traces-endpoint-rewrite-test")
        assert mock_exporter.called
        endpoint = mock_exporter.call_args.kwargs.get("endpoint")
        assert endpoint == "https://example.com/v1/logs", (
            f"traces-URL footgun must be rewritten to logs URL; got {endpoint!r}"
        )


def test_init_log_exporter_appends_logs_signal_to_base_endpoint(monkeypatch):
    """An ``OTEL_EXPORTER_OTLP_ENDPOINT`` base URL (no signal suffix) gets
    ``/v1/logs`` appended so we hand the exporter a complete URL rather
    than relying on the SDK's auto-append. This keeps the runtime URL
    deterministic from our side."""
    from unittest.mock import patch

    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com")

    with patch(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        autospec=True,
    ) as mock_exporter:
        init_log_exporter("base-endpoint-append-test")
        endpoint = mock_exporter.call_args.kwargs.get("endpoint")
        assert endpoint == "https://example.com/v1/logs", (
            f"base URL must get /v1/logs appended; got {endpoint!r}"
        )


def test_init_log_exporter_passes_through_full_logs_endpoint(monkeypatch):
    """An ``OTEL_EXPORTER_OTLP_ENDPOINT`` that already ends in ``/v1/logs``
    (someone treating the base var like a per-signal var) is passed through
    without doubling the signal path."""
    from unittest.mock import patch

    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "https://example.com/v1/logs",
    )

    with patch(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        autospec=True,
    ) as mock_exporter:
        init_log_exporter("logs-endpoint-passthrough-test")
        endpoint = mock_exporter.call_args.kwargs.get("endpoint")
        assert endpoint == "https://example.com/v1/logs", (
            f"full /v1/logs URL must pass through unchanged; got {endpoint!r}"
        )


def test_init_tracer_does_not_leak_hyperdx_auth_to_generic_endpoint(monkeypatch):
    """Regression: ai-uir's secret propagation now ships ``HYPERDX_API_KEY``
    into containers that may also have ``OTEL_EXPORTER_OTLP_ENDPOINT`` set
    to a non-HyperDX sink (Datadog OTLP intake, Grafana Cloud, etc.). The
    tracer must NOT inject the HyperDX Bearer header for those endpoints,
    otherwise every trace batch is rejected. Mirrors the log-exporter
    auth-isolation contract for symmetry."""
    from unittest.mock import patch

    monkeypatch.setenv("HYPERDX_API_KEY", "stale-hyperdx-key")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "https://logs.us3.datadoghq.com/api/v2/otel",
    )
    monkeypatch.delenv("HYPERDX_OTLP_ENDPOINT", raising=False)

    with patch(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        autospec=True,
    ) as mock_exporter:
        init_tracer("trace-isolation-test")
        assert mock_exporter.called
        kwargs = mock_exporter.call_args.kwargs
        assert kwargs.get("endpoint") == "https://logs.us3.datadoghq.com/api/v2/otel"
        assert "headers" not in kwargs, (
            f"HyperDX Bearer header must not leak into generic OTLP trace "
            f"exporter; got headers={kwargs.get('headers')!r}"
        )


def test_init_log_exporter_injects_hyperdx_auth_via_generic_endpoint(monkeypatch):
    """An operator who targets HyperDX via the standard OTel env vars
    (e.g. OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=https://in-otel.hyperdx.io/v1/logs
    + HYPERDX_API_KEY=...) must still get Bearer auth injected. HyperDX
    detection is host-aware, not bound to which env var supplied the URL."""
    from unittest.mock import patch

    _reset_log_exporter(monkeypatch)
    for key in ("HYPERDX_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HYPERDX_API_KEY", "test-hyperdx-key")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "https://in-otel.hyperdx.io/v1/logs",
    )

    with patch(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        autospec=True,
    ) as mock_exporter:
        init_log_exporter("hyperdx-via-generic-test")
        headers = mock_exporter.call_args.kwargs.get("headers", {})
        assert headers.get("authorization", "").startswith("Bearer "), (
            f"Bearer auth must be injected for hyperdx.io endpoints "
            f"regardless of which env var supplied the URL; got {headers!r}"
        )


def test_init_log_exporter_does_not_leak_hyperdx_auth_to_generic_endpoint(
    monkeypatch,
):
    """Regression: a stale ``HYPERDX_API_KEY`` in the process env must not
    inject a Bearer header into requests bound for a generic OTLP endpoint
    (Datadog OTLP intake, Grafana Cloud, local collector). The HyperDX
    Bearer header is only valid against HyperDX endpoints — leaking it
    elsewhere causes those sinks to reject every log batch."""
    from unittest.mock import patch

    _reset_log_exporter(monkeypatch)
    monkeypatch.setenv("HYPERDX_API_KEY", "stale-hyperdx-key")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "https://logs.us3.datadoghq.com/api/v2/logs",
    )
    monkeypatch.delenv("HYPERDX_OTLP_ENDPOINT", raising=False)

    with patch(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        autospec=True,
    ) as mock_exporter:
        init_log_exporter("dd-test")
        assert mock_exporter.called
        kwargs = mock_exporter.call_args.kwargs
        assert kwargs.get("endpoint") == "https://logs.us3.datadoghq.com/api/v2/logs"
        assert "headers" not in kwargs, (
            f"HyperDX Bearer header must not be passed to a generic OTLP "
            f"endpoint; got headers={kwargs.get('headers')!r}"
        )


def test_init_log_exporter_returns_logger_with_headers_only(monkeypatch):
    """A standard OTLP config that supplies only headers (relying on the
    SDK default endpoint at ``http://localhost:4318/v1/logs``) is a
    legitimate setup — typical for local OTel Collector / agent deployments.
    The exporter must initialize, not silently fall back to stdout-only."""
    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_API_KEY",
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "DD-API-KEY=test-datadog-key",
    )
    logger = init_log_exporter()
    assert logger is not None


def test_init_log_exporter_headers_only_with_stale_hyperdx_key_uses_sdk_default(
    monkeypatch,
):
    """Regression: a stale ``HYPERDX_API_KEY`` in the env must not hijack a
    legitimate headers-only OTel config. The exporter should NOT route to
    HyperDX; it should let the SDK use its default endpoint and the
    operator-supplied OTel headers reach the configured backend."""
    from unittest.mock import patch

    _reset_log_exporter(monkeypatch)
    for key in (
        "HYPERDX_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HYPERDX_API_KEY", "stale-hyperdx-key")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "DD-API-KEY=test-datadog-key",
    )

    with patch(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter",
        autospec=True,
    ) as mock_exporter:
        init_log_exporter("headers-only-hyperdx-leak-test")
        assert mock_exporter.called
        kwargs = mock_exporter.call_args.kwargs
        endpoint = kwargs.get("endpoint")
        # No HyperDX URL — the headers-only setup must not be hijacked.
        assert endpoint is None or "hyperdx.io" not in endpoint, (
            f"headers-only OTel config must not be hijacked by stale "
            f"HYPERDX_API_KEY; got endpoint={endpoint!r}"
        )
        # And no HyperDX Bearer header injected either.
        assert "headers" not in kwargs, (
            f"no Bearer header should leak when endpoint is non-HyperDX; "
            f"got headers={kwargs.get('headers')!r}"
        )


def test_endpoint_is_hyperdx_url_matches_host_not_substring():
    """Regression: the HyperDX detector must match the URL's hostname
    component, not a substring of the full URL. A non-HyperDX URL that
    happens to contain ``hyperdx.io`` in a path/query is NOT HyperDX
    and must not get Bearer auth injected."""
    from libs.telemetry import (
        _endpoint_is_hyperdx_url,  # trunk-ignore(pyright/reportPrivateUsage): exercising the host-match invariant directly is more focused than driving init_log_exporter for each of these URL shapes.
    )

    # Genuine HyperDX URLs.
    assert _endpoint_is_hyperdx_url("https://in-otel.hyperdx.io/v1/logs")
    assert _endpoint_is_hyperdx_url("https://hyperdx.io/v1/logs")
    # Subdomain match.
    assert _endpoint_is_hyperdx_url("https://eu.hyperdx.io/v1/logs")
    # NOT HyperDX — substring in the path/query, host is different.
    assert not _endpoint_is_hyperdx_url(
        "https://example.com/proxy/hyperdx.io/forward",
    )
    assert not _endpoint_is_hyperdx_url("https://example.com/?ref=hyperdx.io")
    # NOT HyperDX — bait domain that contains the string but isn't the host.
    assert not _endpoint_is_hyperdx_url("https://hyperdx.io.evil.example.com/v1/logs")
    # Empty / None.
    assert not _endpoint_is_hyperdx_url(None)
    assert not _endpoint_is_hyperdx_url("")
