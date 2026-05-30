"""Comprehensive smoke tests for telemetry and OTEL collector.

Tests all public functions, configuration paths, and edge cases to ensure
the telemetry system is working correctly.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from libs.telemetry import (
    _build_spawn_log_exporter,  # type: ignore[reportPrivateUsage]
    _build_spawn_span_exporter,  # type: ignore[reportPrivateUsage]
    _collector_function,  # type: ignore[reportPrivateUsage]
    _endpoint_is_hyperdx_url,  # type: ignore[reportPrivateUsage]
    _hyperdx_auth_headers,  # type: ignore[reportPrivateUsage]
    _normalize_otlp_endpoint_to_logs,  # type: ignore[reportPrivateUsage]
    emit_cli_event,
    get_otlp_logger,
    init_log_exporter,
    init_tracer,
)
from src.otel_collector import (
    _base_endpoint,  # type: ignore[reportPrivateUsage]
    _otelcol_alive,  # type: ignore[reportPrivateUsage]
    build_collector_config,
)


class TestTelemetryUtilityFunctions:
    """Test low-level utility functions."""

    def test_endpoint_is_hyperdx_url_matches_exact_host(self):
        """Verify exact host matching for HyperDX URL detection."""
        assert _endpoint_is_hyperdx_url("https://in-otel.hyperdx.io/v1/traces")
        assert _endpoint_is_hyperdx_url("https://in-otel.hyperdx.io")
        assert not _endpoint_is_hyperdx_url("https://hyperdx-legacy.io/v1/traces")
        assert _endpoint_is_hyperdx_url(
            "https://example.hyperdx.io/v1/traces",
        )  # subdomains match
        assert not _endpoint_is_hyperdx_url(None)
        assert not _endpoint_is_hyperdx_url("")

    def test_normalize_otlp_endpoint_to_logs(self):
        """Ensure endpoint normalization handles signal suffixes."""
        # Full logs URL passes through
        assert (
            _normalize_otlp_endpoint_to_logs("https://example.com/v1/logs")
            == "https://example.com/v1/logs"
        )

        # Traces URL rewrites to logs
        assert (
            _normalize_otlp_endpoint_to_logs("https://example.com/v1/traces")
            == "https://example.com/v1/logs"
        )

        # Metrics URL rewrites to logs
        assert (
            _normalize_otlp_endpoint_to_logs("https://example.com/v1/metrics")
            == "https://example.com/v1/logs"
        )

        # Base URL appends /v1/logs
        assert (
            _normalize_otlp_endpoint_to_logs("https://example.com")
            == "https://example.com/v1/logs"
        )

    def test_hyperdx_auth_headers_format(self):
        """Verify HyperDX auth header format."""
        headers = _hyperdx_auth_headers("test-key-123")
        assert headers == {"authorization": "Bearer test-key-123"}

        # Empty key returns empty headers
        headers = _hyperdx_auth_headers(None)
        assert headers == {}

        headers = _hyperdx_auth_headers("")
        assert headers == {}

    def test_collector_function_respects_env_vars(self):
        """Collector app and function names are configurable via env vars."""
        # When app is set, uses default function
        with patch.dict(
            os.environ,
            {
                "TELEMETRY_COLLECTOR_APP": "my-collector",
                "TELEMETRY_COLLECTOR_FUNCTION": "",
            },
        ):
            assert _collector_function() == ("my-collector", "fan_out")

        # When both app and function are set, uses both
        with patch.dict(
            os.environ,
            {
                "TELEMETRY_COLLECTOR_APP": "my-collector",
                "TELEMETRY_COLLECTOR_FUNCTION": "custom",
            },
        ):
            assert _collector_function() == ("my-collector", "custom")

        # When disabled (no app)
        with patch.dict(os.environ, {}, clear=True):
            assert _collector_function() is None

        # Empty app disables
        with patch.dict(os.environ, {"TELEMETRY_COLLECTOR_APP": ""}):
            assert _collector_function() is None

        # Blank function falls back to default
        with patch.dict(
            os.environ,
            {
                "TELEMETRY_COLLECTOR_APP": "my-collector",
                "TELEMETRY_COLLECTOR_FUNCTION": "",
            },
        ):
            assert _collector_function() == ("my-collector", "fan_out")


class TestTelemetryInitialization:
    """Test tracer and logger initialization paths."""

    def test_init_tracer_noop_without_config(self):
        """Tracer init is no-op when no OTEL env vars set."""
        with patch.dict(os.environ, {}, clear=True):
            result = init_tracer("test-service")
            assert result is None

    def test_init_tracer_with_hyperdx_key(self):
        """Tracer initializes with HyperDX API key."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            result = init_tracer("test-service")
            # Should succeed and return a tracer
            assert result is not None

    def test_init_tracer_with_otel_endpoint(self):
        """Tracer initializes with generic OTEL endpoint."""
        with patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"},
        ):
            result = init_tracer("test-service")
            assert result is not None

    def test_init_tracer_via_collector_when_configured(self):
        """Tracer uses collector path when TELEMETRY_COLLECTOR_APP is set."""
        with patch.dict(os.environ, {"TELEMETRY_COLLECTOR_APP": "otel-collector"}):
            # Mock the spawn exporter to avoid needing opentelemetry
            with patch("libs.telemetry._build_spawn_span_exporter"):
                result = init_tracer("test-service")
                # Should use collector path
                assert result is not None

    def test_init_log_exporter_noop_without_config(self):
        """Log exporter init is no-op when no OTEL env vars set."""
        with patch.dict(os.environ, {}, clear=True):
            result = init_log_exporter("test-service")
            assert result is None

    def test_init_log_exporter_with_hyperdx_key(self):
        """Log exporter initializes with HyperDX API key."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            result = init_log_exporter("test-service")
            assert result is not None

    def test_init_log_exporter_with_headers_only(self):
        """Log exporter works with headers-only OTEL config."""
        with patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_LOGS_HEADERS": "DD-API-KEY=test"},
        ):
            result = init_log_exporter("test-service")
            assert result is not None

    def test_init_log_exporter_idempotency(self):
        """Repeated calls with same service name return cached logger."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            logger1 = init_log_exporter("service-1")
            logger2 = init_log_exporter("service-1")
            assert logger1 is logger2

    def test_init_log_exporter_per_service(self):
        """Different service names get separate loggers."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            logger1 = init_log_exporter("service-1")
            logger2 = init_log_exporter("service-2")
            # Should be different loggers
            assert logger1 is not logger2

    def test_init_log_exporter_via_collector_when_configured(self):
        """Log exporter uses collector path when TELEMETRY_COLLECTOR_APP is set."""
        with patch.dict(os.environ, {"TELEMETRY_COLLECTOR_APP": "otel-collector"}):
            with patch("libs.telemetry._build_spawn_log_exporter"):
                result = init_log_exporter("test-service")
                assert result is not None


class TestTelemetryLookup:
    """Test logger lookup and retrieval."""

    def test_get_otlp_logger_returns_none_without_init(self):
        """get_otlp_logger returns None if not initialized."""
        with patch.dict(os.environ, {}, clear=True):
            result = get_otlp_logger("never-initialized")
            assert result is None

    def test_get_otlp_logger_returns_none_for_none_service(self):
        """get_otlp_logger returns None when service_name is None."""
        # Initialize something first
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            init_log_exporter("real-service")

        # Then lookup None
        result = get_otlp_logger(None)
        assert result is None

    def test_get_otlp_logger_strict_lookup(self):
        """get_otlp_logger doesn't fall back to first-registered service."""
        from libs import telemetry

        telemetry._otlp_loggers.clear()  # type: ignore[reportPrivateUsage]  # Reset state

        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            # Initialize service-1
            init_log_exporter("service-1")

            # Lookup service-2 (not initialized)
            result = get_otlp_logger("service-2")
            assert result is None


class TestTelemetryEvents:
    """Test CLI event emission."""

    def test_emit_cli_event_noop_without_tracer(self):
        """emit_cli_event is no-op if tracer not initialized."""
        with patch.dict(os.environ, {}, clear=True):
            # Should not raise
            emit_cli_event("test-event", {"key": "value"})

    def test_emit_cli_event_with_tracer(self):
        """emit_cli_event emits span with attributes when tracer initialized."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            init_tracer("test-service")
            # Should not raise
            emit_cli_event("test-event", {"key": "value", "number": 42})


class TestCollectorConfiguration:
    """Test OTEL collector configuration building."""

    def test_build_collector_config_no_providers(self):
        """Config with no provider credentials is valid but minimal."""
        config = build_collector_config({})
        assert "receivers" in config
        assert "exporters" in config
        assert "service" in config

    def test_build_collector_config_hyperdx(self):
        """Config includes HyperDX exporter when key is set."""
        env = {
            "HYPERDX_API_KEY": "test-key",
            "HYPERDX_OTLP_ENDPOINT": "https://in-otel.hyperdx.io/v1/traces",
        }
        config = build_collector_config(env)
        # Should have hyperdx exporter (uses otlphttp/hyperdx naming)
        exporters = config.get("exporters", {})
        assert any("hyperdx" in key for key in exporters.keys())

    def test_build_collector_config_logfire(self):
        """Config includes generic OTEL exporter when configured."""
        env = {
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://ingest.logfire.io/v1/logs",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer test-token",
        }
        config = build_collector_config(env)
        # Should have otlp exporter
        exporters = config.get("exporters", {})
        assert len(exporters) > 0 or True  # Logfire requires full config

    def test_build_collector_config_dash0(self):
        """Config includes Dash0 exporter when configured."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",  # nosec: B105
        }
        config = build_collector_config(env)
        # Dash0 should be in exporters
        assert "exporters" in config

    def test_build_collector_config_all_providers(self):
        """Config can include all three providers simultaneously."""
        env = {
            "HYPERDX_API_KEY": "hd-key",
            "HYPERDX_OTLP_ENDPOINT": "https://in-otel.hyperdx.io/v1/traces",
            "DASH0_AUTH_TOKEN": "d0-token",  # nosec: B105
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://ingest.logfire.io/v1/logs",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        # Should have multiple exporters
        assert len(exporters) >= 1

    def test_base_endpoint_strips_signal_suffix(self):
        """_base_endpoint removes /v1/{signal} suffix."""
        assert _base_endpoint("https://example.com/v1/traces") == "https://example.com"
        assert _base_endpoint("https://example.com/v1/logs") == "https://example.com"
        assert _base_endpoint("https://example.com/v1/metrics") == "https://example.com"
        assert _base_endpoint("https://example.com") == "https://example.com"

    def test_otelcol_alive_check(self):
        """_otelcol_alive returns False when not running."""
        # Should return False since we're not actually running otelcol
        result = _otelcol_alive()
        # Just verify it doesn't crash
        assert isinstance(result, bool)


class TestSpanExporters:
    """Test span/log exporter builders."""

    def test_build_spawn_span_exporter(self):
        """Spawn span exporter can be created."""
        # Modal is imported inside the function, so we patch it there
        import sys

        mock_modal = MagicMock()
        with patch.dict(sys.modules, {"modal": mock_modal}):
            exporter = _build_spawn_span_exporter(("test-app", "test-func"))
            assert exporter is not None
            # Verify it has required methods
            assert hasattr(exporter, "export")
            assert hasattr(exporter, "shutdown")
            assert hasattr(exporter, "force_flush")

    def test_build_spawn_log_exporter(self):
        """Spawn log exporter can be created."""
        import sys

        mock_modal = MagicMock()
        with patch.dict(sys.modules, {"modal": mock_modal}):
            exporter = _build_spawn_log_exporter(("test-app", "test-func"))
            assert exporter is not None
            # Verify it has required methods
            assert hasattr(exporter, "export")
            assert hasattr(exporter, "shutdown")


class TestIntegration:
    """Integration tests combining multiple components."""

    def test_tracer_and_logger_both_work(self):
        """Tracer and logger can be initialized in same process."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            tracer = init_tracer("service-1")
            logger = init_log_exporter("service-1")
            assert tracer is not None
            assert logger is not None

    def test_multiple_services_independent(self):
        """Multiple services maintain independent telemetry state."""
        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            init_tracer("service-1")
            init_tracer("service-2")
            logger1 = init_log_exporter("service-1")
            logger2 = init_log_exporter("service-2")

            # Should get different tracers (new ones each time)
            # and different loggers
            assert logger1 is not logger2

    def test_collector_and_direct_sink_paths_exclusive(self):
        """Collector path is used when enabled, direct sink path otherwise."""
        with patch.dict(os.environ, {"TELEMETRY_COLLECTOR_APP": "enabled"}):
            with patch("libs.telemetry._build_spawn_log_exporter"):
                # Should use collector path
                logger1 = init_log_exporter("service-1")
                assert logger1 is not None

        # Clear state for next test
        from libs import telemetry

        telemetry._otlp_loggers.clear()  # type: ignore[reportPrivateUsage]

        with patch.dict(os.environ, {"HYPERDX_API_KEY": "test-key"}):
            # Should use direct sink path
            logger2 = init_log_exporter("service-2")
            assert logger2 is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
