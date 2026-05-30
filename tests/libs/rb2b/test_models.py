"""Tests for libs/rb2b/models.py — covers PR #7 review feedback:

- direct (un-enveloped) RB2B webhook bodies
- numeric Employee Count values
- RB2B's documented `12:34:56:00.00+00.00` timestamp shape
"""

from __future__ import annotations

from libs.rb2b import Webhook, compute_event_id


def test_webhook_accepts_flat_payload() -> None:
    """RB2B-direct deliveries put visit fields at the top level."""
    body = {
        "LinkedIn URL": "https://www.linkedin.com/company/example",
        "Company Name": "Example Inc",
        "Employee Count": "201-500",
        "Seen At": "2026-05-12T06:24:48.000+00:00",
        "Captured URL": "https://example.com/landing",
        "is_repeat_visit": False,
    }
    webhook = Webhook.model_validate(body)
    assert webhook.payload.company_name == "Example Inc"
    assert webhook.payload.captured_url == "https://example.com/landing"
    assert webhook.connection == "rb2b-direct"
    assert webhook.event_id.startswith("evt_")


def test_webhook_accepts_envelope_unchanged() -> None:
    """The pre-existing envelope shape must keep working."""
    body = {
        "event_id": "evt_known",
        "timestamp": "2026-05-12T06:24:48.000+00:00",
        "connection": "rb2b-visits",
        "payload": {
            "Company Name": "Example Inc",
            "Seen At": "2026-05-12T06:24:48.000+00:00",
        },
    }
    webhook = Webhook.model_validate(body)
    assert webhook.event_id == "evt_known"
    assert webhook.connection == "rb2b-visits"


def test_employee_count_accepts_integer() -> None:
    """RB2B documents Employee Count as integer | string | null."""
    body = {
        "event_id": "evt_1",
        "timestamp": "2026-05-12T06:24:48.000+00:00",
        "connection": "rb2b",
        "payload": {"Employee Count": 60},
    }
    webhook = Webhook.model_validate(body)
    assert webhook.payload.employee_count == "60"


def test_flat_payload_event_id_is_deterministic() -> None:
    """The same flat visit must always derive the same event_id.

    This is what makes live ingestion and historical replays converge on one
    Attio tracking-event external_id (rb2b:{event_id}).
    """
    body = {
        "Business Email": "alice@example.test",
        "LinkedIn URL": "https://www.linkedin.com/in/alice",
        "Captured URL": "https://example.test/pricing",
        "Seen At": "2026-05-14T09:45:00.000+00:00",
    }
    first = Webhook.model_validate(dict(body))
    second = Webhook.model_validate(dict(body))
    assert first.event_id == second.event_id
    assert first.event_id.startswith("evt_")


def test_flat_and_envelope_of_same_visit_share_event_id() -> None:
    """A flat delivery and an envelope built from the same inner payload (the
    backfill's mapped shape) must resolve to the same event_id.

    The backfill computes the dedup key with ``compute_event_id`` on the inner
    payload and injects it as the envelope event_id; this guards that the live
    flat path derives the identical value.
    """
    payload = {
        "Business Email": "bob@example.test",
        "Captured URL": "https://example.test/docs",
        "Seen At": "2026-05-14T10:00:00.000+00:00",
    }
    flat = Webhook.model_validate(dict(payload))
    derived = compute_event_id(dict(payload))
    envelope = Webhook.model_validate(
        {
            "event_id": derived,
            "timestamp": "2026-05-14T10:00:00.000+00:00",
            "connection": "rb2b-backfill",
            "payload": dict(payload),
        },
    )
    assert flat.event_id == derived == envelope.event_id


def test_event_id_converges_across_seen_at_formats() -> None:
    """The id must not depend on which timestamp shape the archive stored.

    The live webhook normalizes Seen At before hashing; the backfill hashes the
    raw archived payload. compute_event_id normalizes internally so both the
    space-separated and ISO forms of the same visit derive the same id.
    """
    iso = {
        "Business Email": "alice@example.test",
        "Captured URL": "https://example.test/pricing",
        "Seen At": "2026-05-11T21:04:43.000+00:00",
    }
    spaced = dict(iso)
    spaced["Seen At"] = "2026-05-11 21:04:43 +0000"
    assert compute_event_id(iso) == compute_event_id(spaced)


def test_event_id_naive_seen_at_treated_as_utc() -> None:
    """A naive (offset-less) Seen At must hash as UTC, not host-local time.

    Otherwise the id would differ across machines/timezones and break the
    live/replay convergence.
    """
    naive = {"Captured URL": "u", "Seen At": "2026-05-11T21:04:43"}
    utc = {"Captured URL": "u", "Seen At": "2026-05-11T21:04:43+00:00"}
    assert compute_event_id(naive) == compute_event_id(utc)


def test_event_id_no_delimiter_collision() -> None:
    """A separator inside one field must not collide with a different layout."""
    a = compute_event_id({"Business Email": "a|b", "Captured URL": "c"})
    b = compute_event_id({"Business Email": "a", "Captured URL": "b|c"})
    assert a != b


def test_anonymous_visits_do_not_collide() -> None:
    """Visits with no identity fields fall back to a full-payload hash so they
    don't all collapse onto one id."""
    a = compute_event_id({"City": "Brooklyn", "State": "NY"})
    b = compute_event_id({"City": "Austin", "State": "TX"})
    assert a != b
    assert a.startswith("evt_")


def test_explicit_event_id_is_preserved_over_derived() -> None:
    """An explicitly-provided event_id always wins over the derived hash."""
    body = {
        "event_id": "evt_caller_supplied",
        "Business Email": "carol@example.test",
        "Seen At": "2026-05-14T10:00:00.000+00:00",
    }
    webhook = Webhook.model_validate(body)
    assert webhook.event_id == "evt_caller_supplied"


def test_seen_at_normalizes_rb2b_documented_format() -> None:
    """RB2B docs sometimes use `12:34:56:00.00+00.00` separators."""
    body = {
        "event_id": "evt_1",
        "timestamp": "2026-05-12T06:24:48.000+00:00",
        "connection": "rb2b",
        "payload": {"Seen At": "2026-05-12T12:34:56:00.00+00.00"},
    }
    webhook = Webhook.model_validate(body)
    assert webhook.payload.seen_at is not None
    assert webhook.payload.seen_at.year == 2026
    assert webhook.payload.seen_at.hour == 12
    assert webhook.payload.seen_at.minute == 34
    assert webhook.payload.seen_at.second == 56


def test_seen_at_normalizes_space_separated_format() -> None:
    """Raw archive payloads carry `2026-05-11 21:04:43 +0000` (space + +HHMM).

    Both the envelope timestamp and the payload Seen At must parse — the flat
    path normalizes the synthesized envelope timestamp from this value too.
    """
    body = {
        "LinkedIn URL": "https://www.linkedin.com/company/example",
        "Seen At": "2026-05-11 21:04:43 +0000",
        "Captured URL": "https://dlthub.com/",
    }
    webhook = Webhook.model_validate(body)
    assert webhook.payload.seen_at is not None
    assert webhook.payload.seen_at.year == 2026
    assert webhook.payload.seen_at.month == 5
    assert webhook.payload.seen_at.day == 11
    assert webhook.payload.seen_at.hour == 21
    assert webhook.payload.seen_at.minute == 4
    assert webhook.payload.seen_at.second == 43
