"""Shared JSON payload validation telemetry for `--json` CLI paths."""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from libs.telemetry import emit_cli_event


def _top_level_keys_from_raw(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return list(parsed.keys()) if isinstance(parsed, dict) else []


def emit_json_payload_validation_telemetry(
    command: str,
    exc: Exception,
    model_cls: type[BaseModel],
    raw_json: str,
) -> None:
    """Emit ``cli.json_validation_error`` when a ``--json`` payload fails to parse or validate."""
    valid_fields = list(model_cls.model_fields.keys())
    raw_fields = _top_level_keys_from_raw(raw_json)
    if isinstance(exc, ValidationError):
        errors: list[dict[str, object]] = [
            {"field": list(e["loc"]), "type": e["type"]} for e in exc.errors()
        ]
    elif isinstance(exc, json.JSONDecodeError):
        errors = [{"field": [], "type": "json_invalid", "msg": exc.msg}]
    else:
        errors = [{"field": [], "type": type(exc).__name__, "msg": str(exc)}]

    emit_cli_event(
        "cli.json_validation_error",
        {
            "command": command,
            "errors": errors,
            "raw_fields": raw_fields,
            "valid_fields": valid_fields,
        },
    )
