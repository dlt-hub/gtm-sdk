# Dash0 Telemetry Setup

This guide walks through setting up Dash0 as a telemetry provider for the otel-collector.

## Endpoints

Dash0 provides regional endpoints:

- **US**: `https://otel-ingest.us.dash0.com`
- **EU**: `https://otel-ingest.eu.dash0.com`

Choose the one nearest your data location.

## Setting Up Secrets in Infisical

Add three secrets to your Infisical project (dev environment):

### 1. DASH0_AUTH_TOKEN

- **Name**: `DASH0_AUTH_TOKEN`
- **Value**: Your Dash0 API token (looks like a long token string)
- **Type**: Secret
- **Environment**: dev

### 2. DASH0_OTLP_ENDPOINT

- **Name**: `DASH0_OTLP_ENDPOINT`
- **Value**: Your regional endpoint
  - US: `https://otel-ingest.us.dash0.com`
  - EU: `https://otel-ingest.eu.dash0.com`
- **Type**: Secret
- **Environment**: dev

### 3. DASH0_DATASET (Optional)

- **Name**: `DASH0_DATASET`
- **Value**: `default` (or your dataset name)
- **Type**: Secret
- **Environment**: dev

## Setting Secrets via CLI

```bash
cd ~/Documents/ai

# Source your Infisical credentials
set -a && source .env.local && set +a

# Set each secret
infisical secrets set \
  DASH0_AUTH_TOKEN "your-token-here" \
  --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"

infisical secrets set \
  DASH0_OTLP_ENDPOINT "https://otel-ingest.us.dash0.com" \
  --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"

infisical secrets set \
  DASH0_DATASET "default" \
  --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"
```

## Deploying the Collector

Once secrets are set, deploy the otel-collector:

```bash
cd ~/Documents/ai/gtm-sdk

# Source Infisical credentials
set -a && source ../ai/.env.local && set +a

# Deploy the collector (it will pick up Dash0 secrets)
infisical run --env=dev -- uv run modal deploy src/otel_collector.py
```

The collector image build fetches the secrets and embeds them, so when the container starts, Dash0 is already configured.

## Verifying Configuration

Check that Dash0 was included in the collector config:

```bash
# From gtm-sdk root:
uv run python -c "
from src.otel_collector import build_collector_config
config = build_collector_config()
exporters = config.get('exporters', {})
print('Exporters:', list(exporters.keys()))
if 'otlphttp/dash0' in exporters:
    print('✓ Dash0 is configured')
else:
    print('✗ Dash0 is NOT configured')
"
```

## Testing Telemetry Flow

Use the verification script to test end-to-end telemetry:

```bash
cd ~/Documents/ai/gtm-sdk

# Set environment (usually injected via Infisical, but can override)
export DASH0_AUTH_TOKEN="your-token"
export DASH0_OTLP_ENDPOINT="https://otel-ingest.us.dash0.com"
export TELEMETRY_COLLECTOR_APP="otel-collector"

# Run tests
uv run pytest tests/src/test_dash0_telemetry_integration.py -v

# Or emit test telemetry (requires collector running on Modal)
uv run scripts/verify_dash0_telemetry.py
```

## Collector Architecture

When the collector is deployed:

```text
App/Webhook/CLI
    ↓ (Modal RPC spawn)
otel-collector function (Modal)
    ↓ (localhost OTLP)
otelcol sidecar (in container)
    ├─→ Dash0
    ├─→ HyperDX (if configured)
    └─→ Logfire (if configured)
```

The app-side exporter serializes each batch to OTLP protobuf and spawns the collector function fire-and-forget. The collector function hands it to a localhost otelcol process, which handles retry, queueing, and fan-out to all providers.

See `libs/telemetry.py` and `src/otel_collector.py` for implementation details.

## Troubleshooting

### Secrets Not Found

```bash
# Verify secrets exist in Infisical:
set -a && source .env.local && set +a
infisical secrets get DASH0_AUTH_TOKEN --env=dev \
  --projectId "$INFISICAL_PROJECT_ID" \
  --token "$INFISICAL_TOKEN"
```

### Collector Not Starting

Check the Modal logs for the otel-collector app:

```bash
modal logs otel-collector --follow
```

Look for errors like "exporters config: *conf.ExportersConfig.Validate()..." which indicates a config build error.

### No Telemetry in Dash0

1. Verify the collector is running: `modal serve src/otel_collector.py`
2. Check Modal logs for export errors
3. Verify the OTLP endpoint is reachable from the collector container
4. Check that the Bearer token is valid

### Mixing Dash0 with Other Providers

You can enable multiple providers simultaneously — the collector will fan-out to all configured exporters:

```bash
# These don't conflict; all three will export
DASH0_AUTH_TOKEN=...
HYPERDX_API_KEY=...
LOGFIRE_WRITE_TOKEN=...
```

## Reference

- [Dash0 Docs](https://dash0.com/docs)
- [OpenTelemetry Collector OTLPHTTP Exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)
- `src/otel_collector.py` — Collector app and config builder
- `libs/telemetry.py` — App-side telemetry initialization
