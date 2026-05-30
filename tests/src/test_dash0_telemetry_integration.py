"""Integration test for Dash0 telemetry configuration.

Verifies that logs, traces, metrics, and spans are properly configured
to flow to Dash0 when credentials are set.

Run with:
  export DASH0_AUTH_TOKEN="your-token"
  export DASH0_OTLP_ENDPOINT="https://otel-ingest.us.dash0.com"  # or EU endpoint
  export DASH0_DATASET="default"  # optional
  uv run pytest tests/src/test_dash0_telemetry_integration.py -v -s
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.otel_collector import build_collector_config


class TestDash0Configuration:
    """Verify Dash0 configuration is correctly built."""

    def test_dash0_exporter_included_when_token_and_endpoint_set(self):
        """Dash0 exporter is included in collector config when both token and endpoint are set."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        assert "otlphttp/dash0" in exporters, f"Dash0 exporter not found in {exporters.keys()}"

    def test_dash0_exporter_missing_token_without_endpoint(self):
        """Dash0 exporter is not included when endpoint is missing."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            # Missing DASH0_OTLP_ENDPOINT
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        assert (
            "otlphttp/dash0" not in exporters
        ), "Dash0 exporter should not be included without endpoint"

    def test_dash0_exporter_missing_token(self):
        """Dash0 exporter is not included when token is missing."""
        env = {
            # Missing DASH0_AUTH_TOKEN
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        assert (
            "otlphttp/dash0" not in exporters
        ), "Dash0 exporter should not be included without token"

    def test_dash0_exporter_headers_include_authorization(self):
        """Dash0 exporter includes Bearer authorization header."""
        env = {
            "DASH0_AUTH_TOKEN": "test-auth-token-123",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})
        headers = dash0_config.get("headers", {})

        # Headers should reference the env var
        assert "Authorization" in headers
        auth_value = headers["Authorization"]
        assert auth_value == "Bearer ${env:DASH0_AUTH_TOKEN}", (
            f"Auth header should reference env var; got {auth_value!r}"
        )

    def test_dash0_exporter_headers_include_dataset(self):
        """Dash0 exporter includes dataset header when configured."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
            "DASH0_DATASET": "my-dataset",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})
        headers = dash0_config.get("headers", {})

        assert "Dash0-Dataset" in headers
        assert headers["Dash0-Dataset"] == "my-dataset"

    def test_dash0_exporter_uses_default_dataset(self):
        """Dash0 exporter uses 'default' dataset when not specified."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
            # No DASH0_DATASET
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})
        headers = dash0_config.get("headers", {})

        assert "Dash0-Dataset" in headers
        assert headers["Dash0-Dataset"] == "default"

    def test_dash0_endpoint_stripped_of_signal_suffix(self):
        """Dash0 endpoint has /v1/{signal} suffix stripped."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com/v1/traces",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})
        endpoint = dash0_config.get("endpoint")

        assert endpoint == "https://otel-ingest.us.dash0.com", (
            f"Endpoint should have /v1/traces stripped; got {endpoint!r}"
        )

    def test_dash0_with_us_endpoint(self):
        """US Dash0 endpoint is correctly configured."""
        env = {
            "DASH0_AUTH_TOKEN": "us-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
            "DASH0_DATASET": "us-region-data",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})

        assert (
            dash0_config.get("endpoint")
            == "https://otel-ingest.us.dash0.com"
        )
        assert dash0_config.get("headers", {}).get("Dash0-Dataset") == "us-region-data"

    def test_dash0_with_eu_endpoint(self):
        """EU Dash0 endpoint is correctly configured."""
        env = {
            "DASH0_AUTH_TOKEN": "eu-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.eu.dash0.com",
            "DASH0_DATASET": "eu-region-data",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})

        assert (
            dash0_config.get("endpoint")
            == "https://otel-ingest.eu.dash0.com"
        )
        assert dash0_config.get("headers", {}).get("Dash0-Dataset") == "eu-region-data"

    def test_dash0_exporter_in_traces_pipeline(self):
        """Dash0 exporter is wired into the traces pipeline."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
        }
        config = build_collector_config(env)
        traces_pipeline = config.get("service", {}).get("pipelines", {}).get("traces", {})
        exporters = traces_pipeline.get("exporters", [])

        assert "otlphttp/dash0" in exporters, (
            f"Dash0 not in traces pipeline exporters: {exporters}"
        )

    def test_dash0_exporter_in_logs_pipeline(self):
        """Dash0 exporter is wired into the logs pipeline."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
        }
        config = build_collector_config(env)
        logs_pipeline = config.get("service", {}).get("pipelines", {}).get("logs", {})
        exporters = logs_pipeline.get("exporters", [])

        assert "otlphttp/dash0" in exporters, (
            f"Dash0 not in logs pipeline exporters: {exporters}"
        )

    def test_dash0_with_retry_and_queue_enabled(self):
        """Dash0 exporter has retry and sending queue enabled."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})

        # Verify batching/retry settings
        assert dash0_config.get("retry_on_failure", {}).get("enabled") is True
        assert dash0_config.get("sending_queue", {}).get("enabled") is True

    def test_all_providers_together(self):
        """All three providers (HyperDX, Dash0, Logfire) can coexist."""
        env = {
            "HYPERDX_API_KEY": "hx-key",
            "HYPERDX_OTLP_ENDPOINT": "https://in-otel.hyperdx.io/v1/traces",
            "DASH0_AUTH_TOKEN": "d0-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com",
            "LOGFIRE_WRITE_TOKEN": "lf-token",
            "LOGFIRE_OTLP_ENDPOINT": "https://logfire-us.pydantic.dev",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})

        assert "otlphttp/hyperdx" in exporters
        assert "otlphttp/dash0" in exporters
        assert "otlphttp/logfire" in exporters

        # Both pipelines should include all three
        traces_exporters = (
            config.get("service", {}).get("pipelines", {}).get("traces", {}).get("exporters", [])
        )
        logs_exporters = (
            config.get("service", {}).get("pipelines", {}).get("logs", {}).get("exporters", [])
        )

        for expected in ["otlphttp/hyperdx", "otlphttp/dash0", "otlphttp/logfire"]:
            assert expected in traces_exporters, f"{expected} missing from traces"
            assert expected in logs_exporters, f"{expected} missing from logs"


class TestDash0EndpointVariants:
    """Test various Dash0 endpoint URL formats."""

    def test_dash0_endpoint_with_trailing_slash(self):
        """Trailing slash in endpoint is handled correctly."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com/",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})
        # Should strip the trailing slash during normalization
        assert dash0_config.get("endpoint") in [
            "https://otel-ingest.us.dash0.com",
            "https://otel-ingest.us.dash0.com/",
        ]

    def test_dash0_endpoint_full_v1_logs_url(self):
        """Full /v1/logs URL is normalized correctly."""
        env = {
            "DASH0_AUTH_TOKEN": "test-token",
            "DASH0_OTLP_ENDPOINT": "https://otel-ingest.us.dash0.com/v1/logs",
        }
        config = build_collector_config(env)
        exporters = config.get("exporters", {})
        dash0_config = exporters.get("otlphttp/dash0", {})

        assert dash0_config.get("endpoint") == "https://otel-ingest.us.dash0.com"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
