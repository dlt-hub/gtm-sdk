from __future__ import annotations

from typing import Any

import src.otel_collector as otel_collector
from src.otel_collector import (  # exercising collector internals directly
    _base_endpoint,  # trunk-ignore(pyright/reportPrivateUsage)
    _collector_secret_payload,  # trunk-ignore(pyright/reportPrivateUsage)
    _ensure_otelcol_running,  # trunk-ignore(pyright/reportPrivateUsage)
    _post_local,  # trunk-ignore(pyright/reportPrivateUsage)
    build_collector_config,
)


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


_ALL_PROVIDER_ENV = {
    "HYPERDX_API_KEY": "hx",  # nosec: B105
    "DASH0_AUTH_TOKEN": "d0",  # nosec: B105
    "DASH0_OTLP_ENDPOINT": "https://ingress.us-west-2.aws.dash0.com",
    "DASH0_DATASET": "prod",
    "LOGFIRE_WRITE_TOKEN": "lf",  # nosec: B105
    "LANGSMITH_API_KEY": "ls",  # nosec: B105
    "LANGSMITH_PROJECT": "gtm-sdk",
}


def test_build_config_fans_out_to_all_providers():
    cfg = build_collector_config(_ALL_PROVIDER_ENV)
    exporters = cfg["exporters"]
    assert set(exporters) == {
        "otlphttp/hyperdx",
        "otlphttp/dash0",
        "otlphttp/logfire",
        "otlphttp/langsmith",
    }
    pipelines = cfg["service"]["pipelines"]
    # Traces fan out to every provider...
    assert set(pipelines["traces"]["exporters"]) == set(exporters)
    # ...but LangSmith has no OTLP logs endpoint, so the logs pipeline skips it.
    assert set(pipelines["logs"]["exporters"]) == {
        "otlphttp/hyperdx",
        "otlphttp/dash0",
        "otlphttp/logfire",
    }
    for signal in ("traces", "logs"):
        pipeline = pipelines[signal]
        assert pipeline["receivers"] == ["otlp"]
        assert pipeline["processors"] == ["batch"]
    # Receiver is bound to localhost only (no public exposure).
    http = cfg["receivers"]["otlp"]["protocols"]["http"]
    assert http["endpoint"] == "127.0.0.1:4318"


def test_build_config_endpoints_and_headers():
    cfg = build_collector_config(_ALL_PROVIDER_ENV)
    exp = cfg["exporters"]
    # Tokens are referenced via ${env:...}, never inlined.
    assert exp["otlphttp/hyperdx"]["endpoint"] == "https://in-otel.hyperdx.io"
    assert (
        exp["otlphttp/hyperdx"]["headers"]["Authorization"]
        == "Bearer ${env:HYPERDX_API_KEY}"
    )
    assert (
        exp["otlphttp/dash0"]["endpoint"] == "https://ingress.us-west-2.aws.dash0.com"
    )
    assert (
        exp["otlphttp/dash0"]["headers"]["Authorization"]
        == "Bearer ${env:DASH0_AUTH_TOKEN}"
    )
    assert exp["otlphttp/dash0"]["headers"]["Dash0-Dataset"] == "prod"
    assert exp["otlphttp/logfire"]["endpoint"] == "https://logfire-us.pydantic.dev"
    # LangSmith uses x-api-key (not Bearer) + a Langsmith-Project header.
    assert (
        exp["otlphttp/langsmith"]["endpoint"] == "https://api.smith.langchain.com/otel"
    )
    assert (
        exp["otlphttp/langsmith"]["headers"]["x-api-key"] == "${env:LANGSMITH_API_KEY}"
    )
    assert exp["otlphttp/langsmith"]["headers"]["Langsmith-Project"] == "gtm-sdk"
    # Every exporter has retry + sending queue.
    for block in exp.values():
        assert block["retry_on_failure"]["enabled"] is True
        assert block["sending_queue"]["enabled"] is True


def test_build_config_dash0_dataset_defaults():
    env = {
        "DASH0_AUTH_TOKEN": "d0",  # trunk-ignore(bandit/B105): test fixture
        "DASH0_OTLP_ENDPOINT": "https://ingress.dash0.com",
    }
    cfg = build_collector_config(env)
    assert cfg["exporters"]["otlphttp/dash0"]["headers"]["Dash0-Dataset"] == "default"


def test_build_config_langsmith_project_defaults():
    env = {
        "LANGSMITH_API_KEY": "ls",  # nosec: B105
    }
    cfg = build_collector_config(env)
    headers = cfg["exporters"]["otlphttp/langsmith"]["headers"]
    assert headers["Langsmith-Project"] == "gtm-sdk"


def test_build_config_langsmith_excluded_from_logs_pipeline():
    # LangSmith implements OTLP traces only (/otel/v1/logs 404s), so it must be
    # absent from the logs pipeline even when other providers are present.
    cfg = build_collector_config(_ALL_PROVIDER_ENV)
    assert "otlphttp/langsmith" in cfg["service"]["pipelines"]["traces"]["exporters"]
    assert "otlphttp/langsmith" not in cfg["service"]["pipelines"]["logs"]["exporters"]


def test_build_config_langsmith_only_has_no_logs_pipeline():
    # A LangSmith-only collector has nothing to feed the logs pipeline, which
    # otelcol would reject if emitted empty — so it must be omitted entirely.
    cfg = build_collector_config({"LANGSMITH_API_KEY": "ls"})  # nosec: B105
    pipelines = cfg["service"]["pipelines"]
    assert set(pipelines["traces"]["exporters"]) == {"otlphttp/langsmith"}
    assert "logs" not in pipelines


def test_build_config_subset_and_empty():
    assert build_collector_config({})["exporters"] == {}
    # Dash0 needs both token and endpoint; token alone is skipped.
    env = {
        "LOGFIRE_WRITE_TOKEN": "lf",  # trunk-ignore(bandit/B105): test fixture
        "DASH0_AUTH_TOKEN": "d0-no-endpoint",  # trunk-ignore(bandit/B105): test fixture
    }
    assert set(build_collector_config(env)["exporters"]) == {"otlphttp/logfire"}


def test_build_config_strips_full_signal_url_to_base():
    env = {
        "HYPERDX_API_KEY": "hx",  # nosec: B105
        "HYPERDX_OTLP_ENDPOINT": "https://in-otel.hyperdx.io/v1/traces",
    }
    cfg = build_collector_config(env)
    # otlphttp appends the signal path itself, so we must hand it the base.
    assert (
        cfg["exporters"]["otlphttp/hyperdx"]["endpoint"] == "https://in-otel.hyperdx.io"
    )


def test_base_endpoint_strips_known_signal_suffixes():
    assert _base_endpoint("https://x.io/v1/traces") == "https://x.io"
    assert _base_endpoint("https://x.io/v1/logs/") == "https://x.io"
    assert _base_endpoint("https://x.io") == "https://x.io"


def test_post_local_targets_localhost_receiver():
    calls: list[dict[str, Any]] = []

    def _post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})

    _post_local("traces", b"OTLP", post=_post)
    assert calls == [
        {
            "url": "http://127.0.0.1:4318/v1/traces",
            "data": b"OTLP",
            "headers": {"Content-Type": "application/x-protobuf"},
        },
    ]


def test_post_local_swallows_errors():
    def _boom(*_a, **_k):
        raise RuntimeError("otelcol not up")

    # Must not raise — otelcol owns reliability once handed off.
    _post_local("logs", b"x", post=_boom)


def test_ensure_otelcol_running_noops_without_providers(monkeypatch):
    """With no provider creds, the collector must NOT start otelcol (an empty
    exporter pipeline is invalid and would boot-loop); it degrades to a no-op.
    Safe to call directly — no providers means no subprocess is launched."""
    monkeypatch.setattr(otel_collector, "_otelcol_proc", None, raising=False)
    for k in _ALL_PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)
    assert _ensure_otelcol_running() is False


class _FakeProc:
    def __init__(self) -> None:
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 1


def test_ensure_otelcol_restarts_crashed_sidecar(monkeypatch):
    """Liveness is by process handle, not a one-shot flag: a crashed sidecar
    (poll() != None) is restarted instead of silently dropping forever."""
    monkeypatch.setattr(otel_collector, "_otelcol_proc", None, raising=False)
    for k, v in _ALL_PROVIDER_ENV.items():
        monkeypatch.setenv(k, v)

    procs: list[_FakeProc] = []

    def _fake_popen(*_a, **_k):
        proc = _FakeProc()
        procs.append(proc)
        return proc

    def _noop_wait(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(otel_collector.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(otel_collector, "_wait_for_port", _noop_wait)

    assert _ensure_otelcol_running() is True
    assert len(procs) == 1  # started once
    assert _ensure_otelcol_running() is True
    assert len(procs) == 1  # still alive -> no restart
    procs[-1].alive = False  # sidecar crashes
    assert _ensure_otelcol_running() is True
    assert len(procs) == 2  # restarted


def test_post_local_logs_non_2xx(monkeypatch, capsys):
    def _post_500(*_a, **_k):
        return _Resp(503)

    _post_local("traces", b"x", post=_post_500)
    err = capsys.readouterr().err
    assert "local_handoff_rejected" in err
    assert "503" in err


def test_collector_secret_payload_reads_env(monkeypatch):
    for k in _ALL_PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DASH0_AUTH_TOKEN", "d0")
    monkeypatch.setenv("DASH0_OTLP_ENDPOINT", "https://ingress.dash0.com")
    monkeypatch.setenv("LOGFIRE_WRITE_TOKEN", "lf")
    assert _collector_secret_payload() == {
        "DASH0_AUTH_TOKEN": "d0",  # trunk-ignore(bandit/B105): test fixture
        "DASH0_OTLP_ENDPOINT": "https://ingress.dash0.com",
        "LOGFIRE_WRITE_TOKEN": "lf",  # trunk-ignore(bandit/B105): test fixture
    }
