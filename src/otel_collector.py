"""Telemetry collector: an OpenTelemetry Collector running on Modal that fans
out to all providers, reached purely over Modal RPC (no public endpoint).

Architecture (see ``libs/telemetry.py`` for the app side):

    app/webhook/CLI ──.spawn(signal, otlp_bytes)──▶  fan_out (Modal function)
      (custom OTEL exporter serializes each batch)        │  POST localhost:4318/v1/{signal}
                                                          ▼
                                                   otelcol (localhost sidecar in this
                                                   same always-warm container)
                                                     batch + retry_on_failure + sending_queue
                                                          ├─▶ Dash0   (Dash0-Dataset header)
                                                          ├─▶ HyperDX
                                                          └─▶ Logfire

Why this shape: apps reach the collector through Modal's authenticated RPC
(``.spawn``), so there is **no inbound web URL** — the otelcol OTLP receiver is
bound to ``127.0.0.1`` and is unreachable from outside the container. Yet the
heavy lifting (batching, retry, queueing, fan-out to N providers) is done by a
real OpenTelemetry Collector rather than hand-rolled Python.

``min_containers=1`` + ``max_containers=1`` pin this to exactly one
always-warm container, so the otelcol process and its in-memory queue are a
single shared buffer (rather than Modal scaling out N containers, each with its
own isolated queue). The queue is in-memory (not Volume-backed): a container
recycle can lose an unflushed batch, which is fine for non-load-bearing
telemetry.

This is a **standalone** Modal app (its own ``modal.App``), NOT registered in
``src/app.py``'s ``_ENDPOINT_MODULES``. Deploy it on its own:

    infisical run --projectId "$INFISICAL_PROJECT_ID" --token "$INFISICAL_TOKEN" \\
        --env=dev -- uv run modal deploy src/otel_collector.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import modal

APP_NAME = "otel-collector"

# The otelcol OTLP/HTTP receiver — bound to localhost only, so it is reachable
# from the ``fan_out`` function in the same container but never from outside.
_OTLP_HOST = "127.0.0.1"
_OTLP_PORT = 4318
# Container-local scratch for the generated config (resolves to /tmp in-container).
_CONFIG_PATH = Path(tempfile.gettempdir()) / "otelcol-config.json"
_OTELCOL_BIN = "/usr/local/bin/otelcol"
_OTELCOL_VERSION = "0.119.0"

# Pinned core collector release. Core is sufficient: the otlp receiver, the
# otlphttp exporter (with custom headers), and the batch processor are all in
# the core distribution — no contrib build needed. The artifact is verified at
# build time against the publisher's checksums file (``sha256sum -c``) before
# extraction, so a corrupted/tampered download fails the build closed instead
# of injecting an unaudited binary into the image.
_OTELCOL_RELEASE_BASE = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/"
    f"download/v{_OTELCOL_VERSION}"
)
_OTELCOL_TARBALL = f"otelcol_{_OTELCOL_VERSION}_linux_amd64.tar.gz"
# The core distribution's checksums file is named per-distribution (and carries
# no version in the name), covering every otelcol-core artifact for the release.
_OTELCOL_CHECKSUMS = "opentelemetry-collector-releases_otelcol_checksums.txt"

# Every provider credential the collector forwards with. These live on the
# collector only — never on the app containers (which just spawn ``fan_out``).
_PROVIDER_SECRET_KEYS = (
    "DASH0_AUTH_TOKEN",
    "DASH0_OTLP_ENDPOINT",
    "DASH0_DATASET",
    "HYPERDX_API_KEY",
    "HYPERDX_OTLP_ENDPOINT",
    "LOGFIRE_WRITE_TOKEN",
    "LOGFIRE_OTLP_ENDPOINT",
)


def _collector_secret_payload() -> dict[str, str | None]:
    """Collect the present provider creds from the host env (deploy-time)."""
    payload: dict[str, str | None] = {}
    for opt in _PROVIDER_SECRET_KEYS:
        v = os.environ.get(opt, "").strip()
        if v:
            payload[opt] = v
    return payload


def _collector_secret() -> modal.Secret:
    """Inline Modal Secret carrying every provider's credentials.

    Built at deploy time from the host env (run under ``infisical run``). These
    env vars are read at runtime BOTH by ``build_collector_config`` (to decide
    which exporters to include) and by otelcol itself via ``${env:...}``
    substitution (so raw tokens never appear in the generated config). NOT
    ``modal.Secret.from_name`` (see AGENTS.md / ai-672).
    """
    return modal.Secret.from_dict(_collector_secret_payload())


def _base_endpoint(url: str) -> str:
    """Strip any ``/v1/<signal>`` suffix so otlphttp can append its own.

    The otlphttp exporter takes a base endpoint and appends ``/v1/traces`` /
    ``/v1/logs`` itself; a pasted full-signal URL would otherwise double up.
    """
    trimmed = url.rstrip("/")
    for suffix in ("/v1/traces", "/v1/logs", "/v1/metrics"):
        if trimmed.endswith(suffix):
            return trimmed[: -len(suffix)]
    return trimmed


def _retrying_otlphttp(endpoint: str, headers: dict[str, str]) -> dict[str, Any]:
    """An otlphttp exporter block with batching-friendly retry + sending queue."""
    return {
        "endpoint": endpoint,
        "headers": headers,
        "retry_on_failure": {"enabled": True},
        "sending_queue": {"enabled": True},
    }


def build_collector_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build the otelcol config (as a dict) for whichever providers are present.

    Returns a config with an otlp/http receiver on localhost, a batch processor,
    and one ``otlphttp/<provider>`` exporter per configured provider, wired into
    both the traces and logs pipelines. Auth tokens are referenced via
    ``${env:VAR}`` so the raw secret never lands in the rendered config; only
    non-secret values (endpoints, the Dash0 dataset name) are inlined.

    JSON is valid YAML, so ``_render_config`` dumps this dict as JSON and otelcol
    loads it directly — no YAML dependency needed.
    """
    e = env if env is not None else dict(os.environ)
    exporters: dict[str, Any] = {}
    names: list[str] = []

    if e.get("HYPERDX_API_KEY"):
        base = _base_endpoint(
            e.get("HYPERDX_OTLP_ENDPOINT") or "https://in-otel.hyperdx.io",
        )
        exporters["otlphttp/hyperdx"] = _retrying_otlphttp(
            base,
            {"Authorization": "Bearer ${env:HYPERDX_API_KEY}"},
        )
        names.append("otlphttp/hyperdx")

    if e.get("DASH0_AUTH_TOKEN") and e.get("DASH0_OTLP_ENDPOINT"):
        exporters["otlphttp/dash0"] = _retrying_otlphttp(
            _base_endpoint(e["DASH0_OTLP_ENDPOINT"]),
            {
                "Authorization": "Bearer ${env:DASH0_AUTH_TOKEN}",
                "Dash0-Dataset": e.get("DASH0_DATASET") or "default",
            },
        )
        names.append("otlphttp/dash0")

    if e.get("LOGFIRE_WRITE_TOKEN"):
        base = _base_endpoint(
            e.get("LOGFIRE_OTLP_ENDPOINT") or "https://logfire-us.pydantic.dev",
        )
        exporters["otlphttp/logfire"] = _retrying_otlphttp(
            base,
            {"Authorization": "Bearer ${env:LOGFIRE_WRITE_TOKEN}"},
        )
        names.append("otlphttp/logfire")

    pipeline = {"receivers": ["otlp"], "processors": ["batch"], "exporters": names}
    return {
        "receivers": {
            "otlp": {"protocols": {"http": {"endpoint": f"{_OTLP_HOST}:{_OTLP_PORT}"}}},
        },
        "processors": {"batch": {}},
        "exporters": exporters,
        "service": {
            "pipelines": {
                "traces": dict(pipeline),
                "logs": dict(pipeline),
            },
        },
    }


def _render_config(config: dict[str, Any]) -> str:
    """Render the config dict to a file-loadable string (JSON is valid YAML)."""
    return json.dumps(config, indent=2)


_otelcol_lock = threading.Lock()
# The running otelcol sidecar process, or None if it has never started. Tracked
# by handle (not a bool) so a crashed sidecar can be detected and restarted —
# otherwise, with min_containers=1, a single crash would silently drop telemetry
# for the rest of the (long-lived) container.
_otelcol_proc: subprocess.Popen[bytes] | None = None


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    """Block until ``host:port`` accepts connections, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    msg = f"otelcol did not start listening on {host}:{port} within {timeout}s"
    raise TimeoutError(msg)


def _otelcol_alive() -> bool:
    """True if the sidecar process exists and has not exited (``poll() is None``)."""
    return _otelcol_proc is not None and _otelcol_proc.poll() is None


def _ensure_otelcol_running() -> bool:
    """(Re)start the otelcol sidecar as needed; return whether it is running.

    Returns ``True`` when otelcol is up (so the caller may POST to it) and
    ``False`` when no providers are configured — in that case we deliberately do
    NOT start otelcol, because a pipeline with an empty ``exporters`` list is
    invalid and otelcol would refuse to boot. A collector deployed without any
    provider creds therefore degrades to a clean no-op rather than a boot loop.

    Liveness is checked by process handle, not a one-shot flag: if the sidecar
    crashed since the last call, it is restarted. Launched lazily on the first
    ``fan_out`` call (not at import) so the module stays importable off-Modal
    (tests, dev) where the binary is absent.
    """
    global _otelcol_proc  # noqa: PLW0603
    if _otelcol_alive():
        return True
    with _otelcol_lock:
        if _otelcol_alive():
            return True
        config = build_collector_config()
        if not config["exporters"]:
            return False
        _CONFIG_PATH.write_text(_render_config(config))
        _otelcol_proc = subprocess.Popen(  # noqa: S603 — fixed binary path + our own config file
            [_OTELCOL_BIN, "--config", str(_CONFIG_PATH)],
        )
        _wait_for_port(_OTLP_HOST, _OTLP_PORT)
        return True


def _post_local(signal: str, payload: bytes, post: Any = None) -> None:
    """POST one OTLP batch to the local otelcol receiver.

    A failed local handoff must not propagate (``fan_out`` is fire-and-forget),
    but it must not be silent either: a non-2xx response or an exception means
    that batch was lost before otelcol's retry/queue could take ownership, so we
    log it to stderr (visible in the collector's Modal logs) rather than swallow.
    """
    if post is None:
        import requests

        post = requests.post
    url = f"http://{_OTLP_HOST}:{_OTLP_PORT}/v1/{signal}"
    try:
        resp = post(
            url,
            data=payload,
            headers={"Content-Type": "application/x-protobuf"},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — must not propagate to the spawner
        print(  # noqa: T201 — surfaced via the collector's Modal logs
            f"otel_collector.local_handoff_failed signal={signal!r} error={exc!r}",
            file=sys.stderr,
        )
        return
    status = getattr(resp, "status_code", 0)
    if not 200 <= status < 300:
        print(  # noqa: T201 — surfaced via the collector's Modal logs
            f"otel_collector.local_handoff_rejected signal={signal!r} status={status}",
            file=sys.stderr,
        )


# Single chained RUN: download tarball + checksums, verify the tarball against
# the publisher's checksum (``--ignore-missing`` so the multi-artifact checksums
# file matches on just our tarball), then extract. Any 404, mismatch, or missing
# entry fails the build (``set -e`` semantics via ``&&``).
_OTELCOL_INSTALL = (
    "cd /tmp"
    f" && curl -fsSL -O {_OTELCOL_RELEASE_BASE}/{_OTELCOL_TARBALL}"
    f" && curl -fsSL -O {_OTELCOL_RELEASE_BASE}/{_OTELCOL_CHECKSUMS}"
    f" && sha256sum --ignore-missing -c {_OTELCOL_CHECKSUMS}"
    f" && tar -xzf {_OTELCOL_TARBALL} -C /usr/local/bin otelcol"
    f" && chmod +x {_OTELCOL_BIN}"
    f" && rm -f {_OTELCOL_TARBALL} {_OTELCOL_CHECKSUMS}"
)

image = (
    modal.Image.debian_slim()
    .uv_pip_install("requests")
    .apt_install("curl", "ca-certificates")
    .run_commands(_OTELCOL_INSTALL)
    .add_local_python_source("libs")
)
app = modal.App(name=APP_NAME, image=image)


@app.function(secrets=[_collector_secret()], min_containers=1, max_containers=1)
def fan_out(signal: str, payload: bytes) -> None:
    """Modal entrypoint: hand one OTLP batch to the local otelcol sidecar.

    Invoked fire-and-forget by the app-side exporter via
    ``modal.Function.from_name(<TELEMETRY_COLLECTOR_APP>, "fan_out").spawn(...)``.
    The sidecar fans out to every configured provider with real retry/queueing.
    No-op when no providers are configured (see ``_ensure_otelcol_running``).
    """
    if not _ensure_otelcol_running():
        return
    _post_local(signal, payload)
