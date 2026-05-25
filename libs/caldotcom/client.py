"""Cal.com REST API v2 client.

Only ``GET /v2/bookings/{uid}`` is needed today — ``BOOKING_NO_SHOW_UPDATED``
webhooks are too slim (just ``bookingUid`` + per-attendee ``noShow`` flags) to
compute ``canonical_meeting_uid`` directly, so the handler must fetch the full
booking to learn host email + start time. All other Cal.com triggers carry
enough information in-payload.

Errors propagate (unlike ``libs/harvest/client.py`` which swallows them) so the
NO_SHOW op surfaces a failed ``ReliabilityEnvelope`` if Cal.com is unreachable —
silent failure here would hide the gap and corrupt audit logs.
"""

from __future__ import annotations

import os
from types import TracebackType
from typing import Any

import httpx

from libs.caldotcom.models import BookingCreatedPayload

CALCOM_API_BASE = "https://api.cal.com"
# Pinned per Cal.com docs: "BookingOutput_2024_08_13" shape matches our
# BookingCreatedPayload model. Bump deliberately when migrating.
CALCOM_API_VERSION = "2024-08-13"


class CalcomClient:
    """Thin synchronous client for the Cal.com v2 API."""

    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        self._client = httpx.Client(
            base_url=CALCOM_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "cal-api-version": CALCOM_API_VERSION,
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def get_booking(self, booking_uid: str) -> BookingCreatedPayload | None:
        """Fetch the full booking by uid.

        Returns ``None`` only on 404 (booking deleted or not found). All other
        non-success codes raise ``httpx.HTTPStatusError`` — the NO_SHOW handler
        turns those into a failed ReliabilityEnvelope so the webhook caller
        sees the gap.

        Cal.com wraps the success body as ``{"status": "success", "data": {...}}``
        with ``data`` matching ``BookingOutput_2024_08_13`` (= our
        ``BookingCreatedPayload``). The wrapper key is also accepted as a flat
        body for forward compatibility.
        """
        response = self._client.get(f"/v2/bookings/{booking_uid}")
        if response.status_code == 404:
            return None
        response.raise_for_status()

        body: Any = response.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(data, dict):
            raise httpx.HTTPError(
                f"Cal.com /v2/bookings/{booking_uid} returned non-dict body",
            )
        # Inject ``triggerEvent`` so the discriminated union resolves.
        return BookingCreatedPayload.model_validate(
            {"triggerEvent": "BOOKING_CREATED", **data},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CalcomClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @classmethod
    def from_env(cls) -> CalcomClient:
        """Build a client from ``CALCOM_API_KEY`` in the process environment.

        The webhook runtime injects this via a Modal Secret bound on the app.
        Local invocation must `infisical run ... -- <cmd>` to populate it.
        """
        try:
            api_key = os.environ["CALCOM_API_KEY"]
        except KeyError as exc:
            raise RuntimeError(
                "CALCOM_API_KEY not set — required for BOOKING_NO_SHOW_UPDATED "
                "handling. Add it to Infisical (dev) and the 'caldotcom' Modal "
                "secret. See plan-02 deploy notes.",
            ) from exc
        return cls(api_key=api_key)
