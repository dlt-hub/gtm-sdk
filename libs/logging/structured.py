"""One JSON line per event, always on, never raises.

Modal renders stdout into its dashboard verbatim, so every call here becomes
one line of structured data we can filter on. The standard fields (`ts`,
`event`, `source`, `request_id`) come from contextvars set once per request
by `webhook_request_context`; callers add their own with `**fields`.

The logger must not be the reason a request fails — `log()` swallows all
serialisation errors and falls back to a minimal `{event, error}` line.
"""

from __future__ import annotations

import contextlib
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson
from uuid_extensions import uuid7

# OTLP attribute values must be one of {str, bool, int, float, bytes} or a
# homogeneous list of those — None values raise inside ``logger.emit`` and
# dicts/nested objects aren't accepted directly. The structured-log surface
# accepts anything, so we sanitize between the two contracts: drop None,
# pass primitives through, JSON-encode anything else as a string so the
# record still ships with the field name attached.
_OTLP_PRIMITIVES = (str, bool, int, float, bytes)

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi import Request


_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_SOURCE: ContextVar[str | None] = ContextVar("source", default=None)

_REQUEST_ID_HEADER = "x-request-id"


def set_source(source: str) -> None:
    """Set the `source` contextvar. Called once at module import per webhook file."""
    _SOURCE.set(source)


def set_request_id(request_id: str) -> None:
    """Set the `request_id` contextvar. Normally driven by `webhook_request_context`."""
    _REQUEST_ID.set(request_id)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def get_source() -> str | None:
    return _SOURCE.get()


def extract_or_generate_request_id(request: Request) -> str:
    """Return the inbound `X-Request-Id` header, falling back to a fresh uuid7."""
    header_value = request.headers.get(_REQUEST_ID_HEADER)
    if header_value:
        return header_value
    return str(uuid7())


def _emit_to_otlp(
    event: str,
    source: str | None,
    request_id: str | None,
    fields: dict[str, Any],
) -> None:
    """Mirror a structured event into the OTLP log pipeline if a sink is wired.

    No-op when ``libs.telemetry.init_log_exporter`` hasn't been called or the
    env vars aren't set. Imported lazily so importing this module on the hot
    path doesn't pull in the OTEL SDK when the sink is disabled.

    Severity mapping is intentionally tiny — three levels covering the events
    the webhook handlers actually emit. A larger taxonomy would invite drift
    between the JSON-line schema (which has no severity) and the OTLP
    attributes.

    Never raises — wrapped in a bare except because the OTLP SDK can refuse
    attribute values it doesn't know how to serialize, and the structured
    logger's contract is that emission cannot fail a request.
    """
    try:
        from libs.telemetry import get_otlp_logger
    except Exception:  # noqa: BLE001 - telemetry import must never break logging
        return

    # Look up the per-service logger keyed by the source contextvar so the
    # OTLP Resource (service.name) attribution matches the emit site. Falls
    # back to any-initialized-logger when the caller hasn't set a source —
    # see ``libs.telemetry.get_otlp_logger`` for the lookup contract.
    logger = get_otlp_logger(source)
    if logger is None:
        return

    try:
        from opentelemetry._logs import LogRecord, SeverityNumber
    except Exception:  # noqa: BLE001 - sdk import must never break logging
        return

    # Severity inference matches on substrings of the event name so
    # non-webhook emitters (``src/attio/export.py``, ``src/enrichment.py``,
    # future call sites) get useful OTLP severities without per-event
    # maintenance. Substring match (not last-segment) so compound suffixes
    # like ``attio.handler_exception`` or ``enrichment.partial_failed``
    # classify correctly.
    #
    # Order matters: ``validation_failed`` / ``validation_error`` are soft
    # rejects (sender's payload was malformed) and are checked first so
    # they map to WARN rather than ERROR — matching the original webhook
    # taxonomy where ``webhook.validation_failed`` was a warning. After
    # that, ``error`` / ``failed`` / ``exception`` / ``fatal`` → ERROR;
    # ``warning`` / ``skipped`` / ``retry`` → WARN; everything else → INFO.
    # The ``status="error"`` sentinel still maps a ``*.completed`` event
    # to ERROR so the webhook taxonomy keeps working.
    event_lower = event.lower()
    if "validation_failed" in event_lower or "validation_error" in event_lower:
        severity_number, severity_text = SeverityNumber.WARN, "WARN"
    elif (
        any(tag in event_lower for tag in ("error", "failed", "exception", "fatal"))
        or fields.get("status") == "error"
    ):
        severity_number, severity_text = SeverityNumber.ERROR, "ERROR"
    elif any(tag in event_lower for tag in ("warning", "skipped", "retry")):
        severity_number, severity_text = SeverityNumber.WARN, "WARN"
    else:
        severity_number, severity_text = SeverityNumber.INFO, "INFO"

    attributes: dict[str, Any] = {}
    if source is not None:
        attributes["source"] = source
    if request_id is not None:
        attributes["request_id"] = request_id
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, _OTLP_PRIMITIVES):
            attributes[key] = value
            continue
        if (
            isinstance(value, (list, tuple))
            and value
            and all(isinstance(v, _OTLP_PRIMITIVES) for v in value)
            and len({type(v) for v in value}) == 1
        ):
            # OTLP requires homogeneous primitive arrays — a mixed-type list
            # like ``[1, "2"]`` passes the primitive check but still violates
            # the spec, so the JSON-encode branch below catches that case.
            # Empty lists also fall through to JSON encoding so the field
            # value stays unambiguous (``"[]"`` rather than dropping it).
            attributes[key] = list(value)
            continue
        # Anything else (dicts, nested objects, mixed lists): JSON-encode
        # so the field still ships rather than getting the whole record
        # rejected by the OTLP exporter.
        try:
            attributes[key] = orjson.dumps(value).decode("utf-8")
        except (TypeError, ValueError):
            attributes[key] = repr(value)

    try:
        now_ns = time.time_ns()
        logger.emit(
            LogRecord(
                timestamp=now_ns,
                observed_timestamp=now_ns,
                body=event,
                severity_number=severity_number,
                severity_text=severity_text,
                attributes=attributes,
            ),
        )
    except Exception:  # noqa: BLE001 - last-resort fallback, never raise from log()
        return


def log(event: str, **fields: Any) -> None:
    """Emit one JSON line to stdout, mirror to OTLP if configured. Never raises."""
    source = _SOURCE.get()
    request_id = _REQUEST_ID.get()
    try:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "source": source,
            "request_id": request_id,
        }
        payload.update(fields)
        line = orjson.dumps(payload).decode("utf-8")
    except (TypeError, ValueError) as exc:
        # orjson refused something in `fields`. Emit a fallback so the event
        # is still visible without taking down the request.
        try:
            line = orjson.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "event": event,
                    "source": source,
                    "request_id": request_id,
                    "log_error": f"{type(exc).__name__}: {exc}",
                },
            ).decode("utf-8")
        except Exception:  # noqa: BLE001 - last-resort fallback, never raise from log()
            return

    # sys.stdout.write + flush rather than print() so a caller monkeypatching
    # print() can't accidentally recurse into us. Failure here must not stop
    # the OTLP mirror that follows — the JSON line and the OTLP record are
    # independent transports and one going down shouldn't black out the
    # other.
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - last-resort fallback, never raise from log()  # trunk-ignore(bandit/B110): pass is intentional — stdout failure must not silence the OTLP mirror
        pass

    _emit_to_otlp(event, source, request_id, fields)


@contextlib.contextmanager
def webhook_request_context(request: Request) -> Generator[str, None, None]:
    """Bind `request_id` for the duration of a webhook call.

    Reads `X-Request-Id` from the inbound request (uuid7 fallback), sets the
    contextvar, yields the id so the caller can log it, and resets the
    contextvar on exit so a recycled Modal container starts clean.
    """
    request_id = extract_or_generate_request_id(request)
    token = _REQUEST_ID.set(request_id)
    try:
        yield request_id
    finally:
        _REQUEST_ID.reset(token)
