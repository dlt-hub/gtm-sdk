"""Tests for the rb2b composable webhook filter framework."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.rb2b.webhook.visit import (
    DEFAULT_FILTERS,
    NoResolvablePersonFilter,
    Webhook,
    WebhookFilters,
)

SAMPLES = Path(__file__).resolve().parents[3] / "api" / "samples"


def _load(name: str) -> Webhook:
    return Webhook.model_validate(json.loads((SAMPLES / name).read_text()))


def _load_raw(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text())


def test_default_filters_allow_company_only_tracking_event() -> None:
    # Anonymous, company-only visit (Tomorrow Happens shape): no business_email,
    # but the prod ``tracking_events`` schema has a Company ref so the row
    # still lands and the visit is visible on the Company timeline.
    # DEFAULT_FILTERS is empty as of ai-0lv — see NoResolvablePersonFilter.
    webhook = _load("rb2b.visit.company_only.redacted.json")
    assert webhook.payload.business_email is None
    assert webhook._excluded_by_filter() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    assert webhook.attio_is_valid_webhook() is True
    ops = webhook.attio_get_operations()
    assert [type(o).__name__ for o in ops] == ["UpsertCompany", "UpsertTrackingEvent"]
    te = ops[1]
    assert te.subject_company is not None
    assert te.subject_person is None
    # Filters do NOT apply to the ETL/raw paths — every webhook still lands.
    assert webhook.etl_is_valid_webhook() is True
    assert webhook.raw_is_valid_webhook() is True


def test_no_resolvable_person_filter_still_opts_in_for_company_only() -> None:
    # The class itself still works as an opt-in suppression for callers
    # that want the legacy person-only behavior — only DEFAULT_FILTERS
    # changed.
    webhook = _load("rb2b.visit.company_only.redacted.json")
    filter_ = NoResolvablePersonFilter(name="drop-anon")
    assert filter_.should_exclude(webhook) is True


def test_anonymous_visit_rejected_at_validity_gate() -> None:
    # No business_email and no domain → rejected before the filter even runs.
    webhook = _load("rb2b.visit.anonymous.redacted.json")
    assert webhook.payload.business_email is None
    assert webhook.attio_is_valid_webhook() is False
    assert webhook.attio_get_operations() == []
    # ETL/raw paths still accept it.
    assert webhook.etl_is_valid_webhook() is True
    assert webhook.raw_is_valid_webhook() is True


def test_filter_keeps_tracking_event_when_business_email_present() -> None:
    # Benjamin Myers shape: business_email present → filter does not exclude,
    # UpsertTrackingEvent is emitted.
    webhook = _load("rb2b.visit.person_only.redacted.json")
    assert webhook.payload.business_email
    assert webhook._excluded_by_filter() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    types = [type(o).__name__ for o in webhook.attio_get_operations()]
    assert "UpsertTrackingEvent" in types

    webhook = _load("rb2b.visit.person_and_company.redacted.json")
    assert webhook.payload.business_email
    assert webhook._excluded_by_filter() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    types = [type(o).__name__ for o in webhook.attio_get_operations()]
    assert "UpsertTrackingEvent" in types


def test_filter_unblocks_tracking_event_when_business_email_added_to_company_only() -> (
    None
):
    raw = _load_raw("rb2b.visit.company_only.redacted.json")
    raw["payload"]["Business Email"] = "ops@acme.test"
    w = Webhook.model_validate(raw)
    assert w._excluded_by_filter() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    types = [type(o).__name__ for o in w.attio_get_operations()]
    assert "UpsertTrackingEvent" in types


def test_filters_serialize_to_json_array() -> None:
    # Default has no filters; round-trip a non-empty list to exercise the
    # discriminated union.
    dumped = DEFAULT_FILTERS.model_dump()
    assert isinstance(dumped, list)
    assert dumped == []

    populated = WebhookFilters(
        root=[NoResolvablePersonFilter(name="drop-no-resolvable-person")],
    )
    roundtrip = WebhookFilters.model_validate_json(populated.model_dump_json())
    assert isinstance(roundtrip.root[0], NoResolvablePersonFilter)
    assert roundtrip.root[0].name == "drop-no-resolvable-person"
