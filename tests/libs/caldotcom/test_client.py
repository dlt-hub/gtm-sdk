"""Cal.com client unit tests using ``httpx.MockTransport``.

No network. Mirrors the pattern in ``libs/harvest/`` for HTTP client tests.
"""

from __future__ import annotations

import httpx
import pytest

from libs.caldotcom.client import CALCOM_API_BASE, CALCOM_API_VERSION, CalcomClient
from libs.caldotcom.models import BookingCreatedPayload

_FAKE_BOOKING = {
    "id": 1,
    "uid": "calcom-booking-xyz",
    "title": "Discovery call",
    "description": "",
    "status": "accepted",
    "start": "2026-06-01T15:00:00.000Z",
    "end": "2026-06-01T15:30:00.000Z",
    "hosts": [
        {
            "id": 1,
            "name": "Host Person",
            "email": "host@dlthub.com",
            "displayEmail": "host@dlthub.com",
            "username": "host",
            "timeZone": "UTC",
        },
    ],
    "attendees": [
        {
            "name": "External Person",
            "email": "external@example.com",
            "displayEmail": "external@example.com",
            "timeZone": "UTC",
            "absent": False,
        },
    ],
    "bookingFieldsResponses": {},
}


def _client_with_handler(
    handler: httpx.MockTransport,
) -> CalcomClient:
    c = CalcomClient(api_key="cal_fake_token")
    # Replace the bound httpx.Client with one that uses the mock transport so
    # we don't actually hit the network.
    c._client = httpx.Client(  # noqa: SLF001 — test surgery  # pyright: ignore[reportPrivateUsage]
        base_url=CALCOM_API_BASE,
        params={"apiKey": "cal_fake_token"},
        headers={
            "cal-api-version": CALCOM_API_VERSION,
            "Accept": "application/json",
        },
        transport=handler,
        timeout=10.0,
    )
    return c


def test_get_booking_returns_payload_when_200() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/bookings/calcom-booking-xyz"
        assert request.url.params["apiKey"] == "cal_fake_token"
        assert "Authorization" not in request.headers
        assert request.headers["cal-api-version"] == CALCOM_API_VERSION
        return httpx.Response(
            200,
            json={"status": "success", "data": _FAKE_BOOKING},
        )

    c = _client_with_handler(httpx.MockTransport(respond))
    result = c.get_booking("calcom-booking-xyz")

    assert isinstance(result, BookingCreatedPayload)
    assert result.uid == "calcom-booking-xyz"
    assert result.hosts[0].email == "host@dlthub.com"


def test_get_booking_accepts_unwrapped_data_body() -> None:
    """Some Cal.com endpoints return the booking flat; client tolerates both."""

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=_FAKE_BOOKING)

    c = _client_with_handler(httpx.MockTransport(respond))
    result = c.get_booking("x")
    assert result is not None
    assert result.uid == "calcom-booking-xyz"


def test_get_booking_returns_none_on_404() -> None:
    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(404, json={"error": "not found"})

    c = _client_with_handler(httpx.MockTransport(respond))
    assert c.get_booking("missing") is None


def test_get_booking_raises_on_5xx() -> None:
    def respond(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, json={"error": "boom"})

    c = _client_with_handler(httpx.MockTransport(respond))
    with pytest.raises(httpx.HTTPStatusError):
        c.get_booking("x")


def test_from_env_raises_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALCOM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CALCOM_API_KEY"):
        CalcomClient.from_env()
