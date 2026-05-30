from __future__ import annotations

import atexit
import json
import os
import signal
import sys
from typing import Any

_tracer = None
# Keyed by ``service_name`` rather than a single global so a process that
# initializes multiple services (e.g. a future entrypoint that calls
# ``init_log_exporter("cli")`` and then loads a webhook handler that calls
# ``init_log_exporter("attio-export")``) doesn't silently attribute the
# second service's records to the first one's Resource. Today's call sites
# are siloed (each webhook is a standalone Modal process, the CLI runs
# alone, ``src/app.py`` runs alone), so this also guards against future
# entrypoints that cross those boundaries.
_otlp_loggers: dict[str, Any] = {}

# Tracks whether we've already installed our SIGTERM bridge. We do it lazily
# (from ``init_log_exporter``) so processes that never wire OTLP don't get
# their signal handlers touched.
_sigterm_bridge_installed = False


def _install_sigterm_bridge_for_atexit() -> None:
    """Ensure ``atexit`` handlers fire on SIGTERM (Modal container recycle).

    Python's default SIGTERM handler calls ``os._exit``, which skips
    ``atexit`` — so the ``provider.shutdown`` we register on the
    ``LoggerProvider`` never flushes the last batch when Modal recycles a
    container with SIGTERM. Installing ``sys.exit`` as the SIGTERM handler
    makes the process raise ``SystemExit`` cleanly, which DOES run
    ``atexit``.

    Conditional install: if another framework (Modal's own runtime, a test
    harness, pytest) has already claimed SIGTERM, we leave their handler
    alone — they presumably have their own graceful-shutdown path that
    will end up triggering our atexit. We only bridge when SIGTERM is at
    its Python default.
    """
    global _sigterm_bridge_installed  # noqa: PLW0603
    if _sigterm_bridge_installed:
        return
    try:
        current = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        # signal.getsignal can raise in non-main threads or restricted envs.
        # Silently skip — atexit will still cover normal exits.
        return
    if current != signal.SIG_DFL:
        return
    try:
        signal.signal(
            signal.SIGTERM,
            lambda _sig, _frame: sys.exit(0),
        )
        _sigterm_bridge_installed = True
    except (ValueError, OSError):
        # signal.signal raises if called from a non-main thread. Same
        # rationale — atexit still runs on normal exits.
        return


def _endpoint_is_hyperdx_url(endpoint: str | None) -> bool:
    """Detect HyperDX by the resolved URL host, not just by which env var supplied it.

    Operators can legitimately point ``OTEL_EXPORTER_OTLP_ENDPOINT`` at a
    HyperDX collector and supply ``HYPERDX_API_KEY``; without host-aware
    detection, the Bearer header wouldn't be injected and HyperDX would
    reject every batch.

    Match against the parsed hostname, NOT a substring of the full URL —
    a path like ``https://example.com/proxy/hyperdx.io/forward`` must not
    be misclassified as HyperDX and have Bearer auth injected.
    """
    if not endpoint:
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
    except (ValueError, TypeError):
        return False
    host = (parsed.hostname or "").lower()
    return host == "hyperdx.io" or host.endswith(".hyperdx.io")


def _normalize_otlp_endpoint_to_logs(endpoint: str) -> str:
    """Coerce an OTLP endpoint URL to a logs-signal URL.

    Operator-provided ``OTEL_EXPORTER_OTLP_ENDPOINT`` values fall into three
    shapes in practice:

    1. A true base URL (``https://host:4318``) — append ``/v1/logs``.
    2. A full logs URL (``.../v1/logs``) — use as-is.
    3. A full traces or metrics URL (``.../v1/traces``, ``.../v1/metrics``)
       — copy-paste mistake from a different signal's docs. Rewrite the
       suffix to ``/v1/logs`` so the log export still works.

    The SDK only handles case 1 cleanly; the others would produce broken
    URLs like ``.../v1/traces/v1/logs``.
    """
    trimmed = endpoint.rstrip("/")
    if trimmed.endswith("/v1/logs"):
        return trimmed
    for wrong_signal in ("/v1/traces", "/v1/metrics"):
        if trimmed.endswith(wrong_signal):
            return trimmed[: -len(wrong_signal)] + "/v1/logs"
    return f"{trimmed}/v1/logs"


def _hyperdx_auth_headers(hyperdx_key: str | None) -> dict[str, str]:
    """Build the Bearer auth header dict shared by trace and log exporters."""
    if not hyperdx_key:
        return {}
    value = (
        hyperdx_key
        if hyperdx_key.lower().startswith("bearer ")
        else f"Bearer {hyperdx_key}"
    )
    return {"authorization": value}


# --- Collector fan-out -----------------------------------------------------
#
# When ``TELEMETRY_COLLECTOR_APP`` is set, the app exports telemetry to a single
# middle layer instead of talking to providers directly: a custom OTEL exporter
# serializes each batch to OTLP protobuf and fire-and-forget ``.spawn()``s the
# collector Modal function (``src/otel_collector.py``). That function feeds the
# bytes to an OpenTelemetry Collector running as a localhost sidecar inside the
# (always-warm) collector container, which fans out to every configured provider
# (Dash0 + HyperDX + Logfire) with real batching/retry/queueing. This keeps
# provider credentials on the collector only, gives the app a single "write",
# centralizes fan-out, and uses Modal RPC as the ingress (no public endpoint).
#
# Environment variables:
# - TELEMETRY_COLLECTOR_APP: Name of the Modal app hosting the collector. Required to enable.
# - TELEMETRY_COLLECTOR_FUNCTION: Name of the function within the app (defaults to "fan_out").


def _collector_function() -> tuple[str, str] | None:
    """Return the ``(app, function)`` of the telemetry collector, or ``None``.

    When ``TELEMETRY_COLLECTOR_APP`` is set in the environment, enables the
    collector path; otherwise falls back to the direct single-endpoint OTLP
    behavior.
    """
    app = os.environ.get("TELEMETRY_COLLECTOR_APP", "").strip()
    if not app:
        return None
    fn = os.environ.get("TELEMETRY_COLLECTOR_FUNCTION", "").strip() or "fan_out"
    return (app, fn)


def _spawn_collector(
    collector: tuple[str, str],
    signal: str,
    payload: bytes,
) -> None:
    """Fire-and-forget ``.spawn()`` the collector Modal function with one batch.

    ``modal.Function.from_name`` is lazy (no network until the call), and
    ``.spawn`` enqueues server-side and returns immediately — so this is safe to
    call from the BatchProcessor's background export thread and survives the
    collector being scaled to zero (Modal queues the call).
    """
    import modal

    app_name, fn_name = collector
    handle = modal.Function.from_name(app_name, fn_name)
    handle.spawn(
        signal,
        payload,
    )


def _build_spawn_span_exporter(collector: tuple[str, str]):
    """A ``SpanExporter`` that serializes each batch and spawns the collector."""
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _ModalSpawnSpanExporter(SpanExporter):
        def export(self, spans: Any) -> Any:
            try:
                payload = encode_spans(spans).SerializeToString()
                _spawn_collector(collector, "traces", payload)
            except Exception:  # noqa: BLE001 — telemetry is never load-bearing
                return SpanExportResult.FAILURE
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return _ModalSpawnSpanExporter()


def _build_spawn_log_exporter(collector: tuple[str, str]):
    """A ``LogRecordExporter`` that serializes each batch and spawns the collector."""
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
    from opentelemetry.sdk._logs.export import (
        LogRecordExporter,
        LogRecordExportResult,
    )

    class _ModalSpawnLogExporter(LogRecordExporter):
        def export(self, batch: Any) -> Any:
            try:
                payload = encode_logs(batch).SerializeToString()
                _spawn_collector(collector, "logs", payload)
            except Exception:  # noqa: BLE001 — telemetry is never load-bearing
                return LogRecordExportResult.FAILURE
            return LogRecordExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

    return _ModalSpawnLogExporter()


def _init_tracer_via_collector(service_name: str, collector: tuple[str, str]):
    """Build a tracer whose exporter spawns the collector. See ``init_tracer``."""
    global _tracer  # noqa: PLW0603
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.semconv.attributes import service_attributes
    except ModuleNotFoundError as exc:
        print(  # noqa: T201 — import-time, before any logger is wired
            "telemetry.otlp_disabled "
            f"reason=opentelemetry_not_installed missing_module={exc.name!r} "
            f"service_name={service_name!r}",
            file=sys.stderr,
        )
        return None
    resource = Resource.create(
        {
            service_attributes.SERVICE_NAME: service_name,
            service_attributes.SERVICE_VERSION: "0.1.0",
        },
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(_build_spawn_span_exporter(collector)),
    )
    trace.set_tracer_provider(provider)
    # Flush the BatchSpanProcessor's buffer on exit/recycle so a short-lived CLI
    # run doesn't drop its last span batch — mirrors the log path's wiring.
    atexit.register(provider.shutdown)
    _install_sigterm_bridge_for_atexit()
    _tracer = trace.get_tracer(service_name)
    return _tracer


def _init_log_exporter_via_collector(service_name: str, collector: tuple[str, str]):
    """Build a log exporter whose exporter spawns the collector.

    Mirrors ``init_log_exporter``'s provider/cache/atexit wiring.
    """
    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.semconv.attributes import service_attributes
    except ModuleNotFoundError as exc:
        print(  # noqa: T201 — import-time, before any logger is wired
            "telemetry.otlp_disabled "
            f"reason=opentelemetry_not_installed missing_module={exc.name!r} "
            f"service_name={service_name!r}",
            file=sys.stderr,
        )
        return None
    resource = Resource.create(
        {
            service_attributes.SERVICE_NAME: service_name,
            service_attributes.SERVICE_VERSION: "0.1.0",
        },
    )
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(_build_spawn_log_exporter(collector)),
    )
    atexit.register(provider.shutdown)
    _install_sigterm_bridge_for_atexit()
    logger = provider.get_logger("structured")
    _otlp_loggers[service_name] = logger
    return logger


def init_tracer(service_name: str = "elvis-cli"):
    """Initialize OTEL tracer.

    When ``TELEMETRY_COLLECTOR_APP`` is set, spans export to the collector Modal
    function (see ``_init_tracer_via_collector``). Otherwise falls back to the
    direct single-endpoint path below — a no-op if neither HYPERDX_API_KEY nor
    OTEL_EXPORTER_OTLP_ENDPOINT is set.

    Auth handling mirrors ``init_log_exporter``: the HyperDX Bearer header
    is only injected when the resolved trace endpoint is HyperDX (came
    from HyperDX shorthand vars or whose URL points at hyperdx.io). For
    generic OTEL endpoints (Datadog OTLP intake, Grafana Cloud, custom
    collectors), no explicit ``headers=`` is passed so the SDK reads
    ``OTEL_EXPORTER_OTLP_HEADERS`` / ``OTEL_EXPORTER_OTLP_TRACES_HEADERS``
    per the spec. That keeps a stale ``HYPERDX_API_KEY`` in the env from
    leaking Bearer auth to a non-HyperDX sink and getting batches rejected.
    """
    global _tracer  # noqa: PLW0603

    # Collector fan-out wins when configured: export to the single middle layer
    # (one Modal-function spawn per batch) instead of talking to a sink directly.
    collector = _collector_function()
    if collector is not None:
        return _init_tracer_via_collector(service_name, collector)

    hyperdx_key = os.environ.get("HYPERDX_API_KEY")
    otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    hyperdx_endpoint_env = os.environ.get("HYPERDX_OTLP_ENDPOINT")

    if not hyperdx_key and not otel_endpoint and not hyperdx_endpoint_env:
        return None

    # Telemetry must never be load-bearing for the caller. Same rationale as
    # ``init_log_exporter``: an OTLP sink env var can be baked into a Modal
    # Secret (or present in any CLI environment) whose image never installed
    # the ``opentelemetry-*`` packages. Letting the ImportError propagate turns
    # an absent optional dependency into an import-time crash — here it would
    # take down the CLI entrypoint (``cli/main.py``) that calls ``init_tracer``.
    # Degrade to no tracer instead.
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.semconv.attributes import service_attributes
    except ModuleNotFoundError as exc:
        print(  # noqa: T201 — import-time, before any logger is wired
            "telemetry.otlp_disabled "
            f"reason=opentelemetry_not_installed missing_module={exc.name!r} "
            f"service_name={service_name!r}",
            file=sys.stderr,
        )
        return None

    resource = Resource.create(
        {
            service_attributes.SERVICE_NAME: service_name,
            service_attributes.SERVICE_VERSION: "0.1.0",
        },
    )

    endpoint: str | None = otel_endpoint
    endpoint_is_hyperdx = False
    if not endpoint and (hyperdx_key or hyperdx_endpoint_env):
        endpoint = hyperdx_endpoint_env or "https://in-otel.hyperdx.io/v1/traces"
        endpoint_is_hyperdx = True
    if not endpoint:
        return None

    # Same HyperDX detection as init_log_exporter — host-aware so an
    # operator who points OTEL_EXPORTER_OTLP_ENDPOINT at HyperDX with
    # HYPERDX_API_KEY still gets Bearer auth.
    if not endpoint_is_hyperdx and _endpoint_is_hyperdx_url(endpoint):
        endpoint_is_hyperdx = True

    exporter_kwargs: dict[str, Any] = {"endpoint": endpoint}
    if endpoint_is_hyperdx:
        hyperdx_headers = _hyperdx_auth_headers(hyperdx_key)
        if hyperdx_headers:
            exporter_kwargs["headers"] = hyperdx_headers

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(**exporter_kwargs)),
    )
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    return _tracer


def init_log_exporter(service_name: str = "elvis-cli"):
    """Initialize an OTLP log exporter alongside ``libs/logging/structured.log()``.

    When ``TELEMETRY_COLLECTOR_APP`` is set, logs export to the collector Modal
    function (see ``_init_log_exporter_via_collector``); the direct-sink path
    below is the fallback.

    Provider-agnostic: any OTLP-compatible HTTP sink (HyperDX, Datadog OTLP
    intake, Grafana Cloud, a local OTel Collector) works. No-op unless one of
    ``HYPERDX_API_KEY``, ``HYPERDX_OTLP_ENDPOINT``,
    ``OTEL_EXPORTER_OTLP_ENDPOINT``, or ``OTEL_EXPORTER_OTLP_LOGS_ENDPOINT``
    is set, matching ``init_tracer``'s gate.

    Endpoint resolution prefers per-signal config, then falls back to the
    shared OTEL base endpoint (with ``/v1/logs`` appended), then to
    ``HYPERDX_OTLP_ENDPOINT`` (rewritten from ``/v1/traces`` to ``/v1/logs``
    if needed).

    Auth headers are only forwarded for HyperDX (Bearer ``HYPERDX_API_KEY``).
    For other sinks that need custom auth, set the standard OTel env vars
    (``OTEL_EXPORTER_OTLP_HEADERS`` or ``OTEL_EXPORTER_OTLP_LOGS_HEADERS``)
    — the underlying ``OTLPLogExporter`` reads them automatically when no
    explicit ``headers=`` is passed, so a Datadog operator can set
    ``OTEL_EXPORTER_OTLP_LOGS_HEADERS=DD-API-KEY=...`` without changing this
    code.

    Repeat calls with the **same** ``service_name`` short-circuit so
    container-import init can't pile up BatchLogRecordProcessor threads.
    Repeat calls with a **different** ``service_name`` get their own
    provider, so a process that initializes more than one service attributes
    each record to the right Resource — emit-site lookup happens via
    ``get_otlp_logger(service_name)``.
    """
    if service_name in _otlp_loggers:
        return _otlp_loggers[service_name]

    # Collector fan-out wins when configured: export to the single middle layer
    # (one Modal-function spawn per batch) instead of talking to a sink directly.
    collector = _collector_function()
    if collector is not None:
        return _init_log_exporter_via_collector(service_name, collector)

    hyperdx_key = os.environ.get("HYPERDX_API_KEY")
    logs_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    hyperdx_endpoint = os.environ.get("HYPERDX_OTLP_ENDPOINT")
    # OTel header env vars count as a valid "sink is wired" signal — a
    # headers-only config (e.g. ``OTEL_EXPORTER_OTLP_LOGS_HEADERS=DD-API-KEY=...``
    # with no explicit endpoint) is a legitimate OTLP setup that relies on
    # the SDK's default endpoint (``http://localhost:4318/v1/logs`` for the
    # HTTP exporter). Without this, headers-only configs would silently fall
    # back to stdout-only logging.
    otel_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    otel_logs_headers = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_HEADERS")

    if not (
        hyperdx_key
        or logs_endpoint
        or otel_endpoint
        or hyperdx_endpoint
        or otel_headers
        or otel_logs_headers
    ):
        return None

    # Track whether the endpoint resolved from HyperDX shorthand vars or from
    # a generic OTEL endpoint. The HyperDX Bearer auth header is only valid
    # against HyperDX endpoints — forwarding it to a Datadog / Grafana /
    # local-collector URL when an unrelated ``HYPERDX_API_KEY`` is also in
    # the process env would make those sinks reject every batch. So we only
    # inject the Bearer header when the endpoint *itself* came from HyperDX
    # resolution; generic OTEL endpoints get the standard OTLP headers path
    # (no explicit headers → SDK reads ``OTEL_EXPORTER_OTLP_*_HEADERS``).
    endpoint: str | None = None
    endpoint_is_hyperdx = False
    # Precedence order matters here. Explicit OTel configuration wins
    # over HyperDX shorthand vars: a headers-only setup
    # (``OTEL_EXPORTER_OTLP_HEADERS=...``) with a stale ``HYPERDX_API_KEY``
    # in the env must NOT silently get routed to HyperDX. We only fall
    # back to the HyperDX collector URL when NO OTel endpoint OR headers
    # config is present.
    has_otel_config = bool(
        logs_endpoint or otel_endpoint or otel_headers or otel_logs_headers,
    )
    if logs_endpoint:
        # Per OTel spec, the per-signal env var is the FULL logs URL — no
        # path mangling on our side.
        endpoint = logs_endpoint
    elif otel_endpoint:
        # ``OTEL_EXPORTER_OTLP_ENDPOINT`` is the base URL per spec, BUT
        # operators routinely paste a full traces URL here when copying
        # from a traces example. If we hand the SDK ``.../v1/traces``, it
        # blindly appends ``/v1/logs`` and we ship to
        # ``.../v1/traces/v1/logs`` — broken. Normalize: rewrite a known
        # traces/metrics signal suffix to ``/v1/logs``, leave an existing
        # ``/v1/logs`` untouched, and otherwise treat the value as a base
        # and append the logs path ourselves.
        endpoint = _normalize_otlp_endpoint_to_logs(otel_endpoint)
    elif not has_otel_config and (hyperdx_key or hyperdx_endpoint):
        # HyperDX shorthand fallback. Only reached when the operator has
        # NOT supplied any OTel-spec endpoint/headers — otherwise we'd
        # silently override their generic-OTLP intent with a HyperDX URL.
        base = hyperdx_endpoint or "https://in-otel.hyperdx.io/v1/traces"
        endpoint = _normalize_otlp_endpoint_to_logs(base)
        endpoint_is_hyperdx = True

    # Also classify a generic OTel-configured endpoint as HyperDX if its
    # URL actually points at hyperdx.io — that way an operator who uses
    # the standard OTel env vars to target HyperDX (e.g.
    # ``OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=https://in-otel.hyperdx.io/v1/logs``
    # + ``HYPERDX_API_KEY=...``) still gets Bearer auth injected and
    # HyperDX accepts the batch.
    if not endpoint_is_hyperdx and _endpoint_is_hyperdx_url(endpoint):
        endpoint_is_hyperdx = True
    # If still no endpoint, we don't fail — the SDK has its own default
    # (``http://localhost:4318/v1/logs``) and the operator may be using a
    # local collector. Pass ``endpoint=None`` so OTLPLogExporter resolves
    # it from env vars / default.

    # Telemetry must never be load-bearing for the caller. This function runs
    # at container/CLI import time, and an OTLP sink env var can be present
    # (baked into a Modal Secret by ``src.secrets_bootstrap.bootstrap_secret``
    # from the deploy-time shell) in an environment whose image never installed
    # the ``opentelemetry-*`` packages. Letting the ImportError propagate turns
    # an absent optional dependency into an import-time crash that takes down
    # the whole webhook container in a restart loop (observed on
    # ``export-to-attio-from-rb2b-visits``). Degrade to stdout-only logging
    # instead — the structured ``log()`` transport stays unaffected.
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.semconv.attributes import service_attributes
    except ModuleNotFoundError as exc:
        print(  # noqa: T201 — import-time, before any logger is wired
            "telemetry.otlp_disabled "
            f"reason=opentelemetry_not_installed missing_module={exc.name!r} "
            f"service_name={service_name!r}",
            file=sys.stderr,
        )
        return None

    resource = Resource.create(
        {
            service_attributes.SERVICE_NAME: service_name,
            service_attributes.SERVICE_VERSION: "0.1.0",
        },
    )

    # Only inject the HyperDX Bearer header when the endpoint is HyperDX —
    # see the ``endpoint_is_hyperdx`` rationale above. For all other
    # endpoints (generic OTEL, default-resolved), pass no explicit headers
    # so the SDK reads ``OTEL_EXPORTER_OTLP_HEADERS`` /
    # ``OTEL_EXPORTER_OTLP_LOGS_HEADERS`` per the spec.
    exporter_kwargs: dict[str, Any] = {}
    if endpoint is not None:
        exporter_kwargs["endpoint"] = endpoint
    if endpoint_is_hyperdx:
        hyperdx_headers = _hyperdx_auth_headers(hyperdx_key)
        if hyperdx_headers:
            exporter_kwargs["headers"] = hyperdx_headers
    exporter = OTLPLogExporter(**exporter_kwargs)

    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    # ``BatchLogRecordProcessor`` buffers records and exports on a background
    # thread; without an explicit shutdown the last batch is dropped when a
    # short-lived CLI run exits or a Modal container is recycled.
    # ``atexit.register(provider.shutdown)`` covers normal exits and any
    # ``SystemExit`` path. SIGTERM doesn't run atexit by default, so we
    # also install a conditional SIGTERM bridge (see
    # ``_install_sigterm_bridge_for_atexit``) that turns SIGTERM into a
    # clean ``sys.exit(0)`` so atexit fires on container recycle. SIGKILL
    # still loses the buffer, but that's outside our reach.
    atexit.register(provider.shutdown)
    _install_sigterm_bridge_for_atexit()

    logger = provider.get_logger("structured")
    _otlp_loggers[service_name] = logger
    return logger


def get_otlp_logger(service_name: str | None = None):
    """Return the initialized OTLP logger for ``service_name``, or ``None``.

    Called from ``libs/logging/structured.log()`` to decide whether to mirror
    a stdout line into the OTLP pipeline. Kept as a separate accessor so the
    log path never has to import OTEL SDK symbols when the sink is disabled.

    Lookup is **strict in both directions**: a ``service_name`` that doesn't
    match any initialized service returns ``None``, and a ``None`` lookup
    also returns ``None``. There's no "first-registered" any-logger
    fallback — that would silently misattribute records to whichever
    service happened to register first in a process that ran more than
    one ``init_log_exporter``. Callers are expected to bind the source
    contextvar (via ``libs.logging.structured.set_source(<APP_NAME>)``)
    with the same name they pass to ``init_log_exporter(<APP_NAME>)``,
    so the source contextvar is the per-emit lookup key.
    """
    if service_name is None:
        return None
    return _otlp_loggers.get(service_name)


def emit_cli_event(name: str, attributes: dict[str, Any]) -> None:
    """Fire-and-forget OTEL span event. No-op if tracer not initialized."""
    if _tracer is None:
        return
    with _tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if isinstance(value, (list, dict)):
                span.set_attribute(key, json.dumps(value))
            else:
                span.set_attribute(key, value)
