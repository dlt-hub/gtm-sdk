#!/usr/bin/env python
"""Verify Dash0 telemetry integration by emitting logs, traces, and spans.

Usage:
  export DASH0_AUTH_TOKEN="your-token-here"
  export DASH0_OTLP_ENDPOINT="https://otel-ingest.us.dash0.com"
  export TELEMETRY_COLLECTOR_APP="otel-collector"  # or "otel-collector-dev" for testing
  uv run scripts/verify_dash0_telemetry.py
"""

import os
import sys
import time
from pathlib import Path

# Add gtm-sdk to path so we can import libs
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.telemetry import emit_cli_event, init_log_exporter, init_tracer
from libs.logging.structured import log, set_source


def verify_environment() -> dict[str, str]:
    """Verify required environment variables are set."""
    required = {
        "DASH0_AUTH_TOKEN": "Dash0 authentication token",  # nosec: not a password
        "DASH0_OTLP_ENDPOINT": "Dash0 OTLP endpoint (e.g., https://otel-ingest.us.dash0.com)",
    }
    optional = {
        "DASH0_DATASET": "Dash0 dataset name (defaults to 'default')",
        "TELEMETRY_COLLECTOR_APP": "Modal app hosting the collector (if using collector fan-out)",
    }

    config = {}
    missing = []

    print("🔍 Checking environment variables...")
    for key, desc in required.items():
        value = os.environ.get(key, "").strip()
        if not value:
            missing.append(f"{key}: {desc}")
        else:
            config[key] = value
            # Don't print tokens
            if "TOKEN" in key:
                print(f"  ✓ {key}: (set, {len(value)} chars)")
            else:
                print(f"  ✓ {key}: {value}")

    if missing:
        print("\n❌ Missing required environment variables:")
        for msg in missing:
            print(f"  - {msg}")
        print("\nSet them with:")
        print('  export DASH0_AUTH_TOKEN="your-token"')
        print('  export DASH0_OTLP_ENDPOINT="https://otel-ingest.us.dash0.com"')
        return {}

    print("\n📋 Optional environment variables:")
    for key, desc in optional.items():
        value = os.environ.get(key, "").strip()
        if value:
            print(f"  ✓ {key}: {value}")
            config[key] = value
        else:
            print(f"  - {key}: (not set) {desc}")

    return config


def verify_collector_config() -> bool:
    """Verify the collector configuration includes Dash0."""
    from src.otel_collector import build_collector_config

    config = build_collector_config()
    exporters = config.get("exporters", {})

    if "otlphttp/dash0" not in exporters:
        print("\n❌ Dash0 exporter not found in collector config!")
        print(f"   Available exporters: {list(exporters.keys())}")
        return False

    print("\n✓ Dash0 exporter configured in collector")

    dash0_config = exporters["otlphttp/dash0"]
    endpoint = dash0_config.get("endpoint")
    headers = dash0_config.get("headers", {})

    print(f"  Endpoint: {endpoint}")
    print(f"  Dataset: {headers.get('Dash0-Dataset', 'default')}")
    print(f"  Auth: {headers.get('Authorization', 'not configured')[:20]}...")

    return True


def emit_test_telemetry():
    """Emit test logs, traces, and spans."""
    print("\n📤 Emitting test telemetry...")

    # Initialize tracer and logger
    tracer = init_tracer("verify-dash0-telemetry")
    logger = init_log_exporter("verify-dash0-telemetry")

    if logger:
        set_source("verify-dash0-telemetry")

    # Emit a test log
    if logger:
        log(
            "dash0_test_log",
            level="info",
            message="Test log for Dash0 verification",
            test_id="verify-001",
        )
        print("  ✓ Emitted test log")

    # Emit a test trace/span
    if tracer:
        with tracer.start_as_current_span("verify-dash0-span") as span:
            span.set_attribute("test_id", "verify-002")
            span.set_attribute("message", "Test span for Dash0 verification")
            time.sleep(0.1)  # Make span duration visible
        print("  ✓ Emitted test trace/span")

    # Emit a CLI event
    emit_cli_event(
        "dash0_verification",
        {
            "test_id": "verify-003",
            "message": "Test CLI event",
            "timestamp": time.time(),
        },
    )
    print("  ✓ Emitted CLI event")

    # Give time for batching and export
    print("\n⏳ Waiting for telemetry to be batched and exported (5 seconds)...")
    time.sleep(5)

    print("\n✅ Test telemetry emitted. Check your Dash0 dashboard for:")
    print("  - Logs with message='Test log for Dash0 verification'")
    print("  - Traces/spans with name='verify-dash0-span'")
    print("  - Events with name='dash0_verification'")
    print("\n📊 Verification checklist:")
    print("  □ Logs appear in Dash0")
    print("  □ Traces appear in Dash0")
    print("  □ Spans appear in Dash0")
    print("  □ Metrics appear in Dash0")


def main():
    """Run the verification."""
    print("🎯 Dash0 Telemetry Verification Tool\n")

    # Check environment
    config = verify_environment()
    if not config:
        sys.exit(1)

    # Verify collector config
    if not verify_collector_config():
        print("\n💡 Tip: Make sure Dash0 secrets are set in Infisical:")
        print("   - DASH0_AUTH_TOKEN")
        print("   - DASH0_OTLP_ENDPOINT")
        sys.exit(1)

    # Emit test telemetry
    try:
        emit_test_telemetry()
    except Exception as e:
        print(f"\n❌ Error emitting telemetry: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    print("\n✅ Verification complete!")


if __name__ == "__main__":
    main()
