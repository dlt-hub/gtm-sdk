from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.rb2b.webhook.visit import Webhook, extract_domain

SAMPLES = Path(__file__).resolve().parents[3] / "api" / "samples"


def _load(name: str) -> Webhook:
    import orjson

    return Webhook.model_validate(orjson.loads((SAMPLES / name).read_text()))


def _load_raw(name: str) -> dict[str, Any]:
    import orjson

    return orjson.loads((SAMPLES / name).read_text())


def _person_op_linkedin(w: Webhook) -> str | None:
    for op in w.attio_get_operations():
        if type(op).__name__ == "UpsertPerson":
            return op.linkedin
    raise AssertionError("expected an UpsertPerson op")


def _person_webhook_with_linkedin(value: str | None) -> Webhook:
    raw = _load_raw("rb2b.visit.person_only.redacted.json")
    raw["payload"]["LinkedIn URL"] = value
    return Webhook.model_validate(raw)


def test_attio_get_secret_collection_names() -> None:
    assert Webhook.attio_get_secret_collection_names() == ["attio"]


def test_extract_domain_strips_scheme_path_and_www() -> None:
    assert extract_domain("https://example.com") == "example.com"
    assert extract_domain("https://www.example.com/path?x=1") == "example.com"
    assert extract_domain("example.com") == "example.com"
    assert extract_domain("www.example.com") == "example.com"
    assert extract_domain(None) is None
    assert extract_domain("") is None


def test_attio_get_operations_anonymous_visit_is_rejected() -> None:
    w = _load("rb2b.visit.anonymous.redacted.json")
    assert w.attio_is_valid_webhook() is False


def test_attio_get_operations_company_only_emits_company_and_tracking_event() -> None:
    """Company-only visit (no resolvable Person) lands both an
    ``UpsertCompany`` and an ``UpsertTrackingEvent`` carrying a
    ``CompanyRef`` — the prod ``tracking_events`` schema has a
    ``company`` record-reference attribute, so the row is visible on the
    Company timeline even without a Person link. See ai-0lv;
    superseded ai-5x9's Person-only-schema rationale.
    """
    w = _load("rb2b.visit.company_only.redacted.json")
    assert w.attio_is_valid_webhook() is True
    ops = w.attio_get_operations()
    assert [type(o).__name__ for o in ops] == ["UpsertCompany", "UpsertTrackingEvent"]
    te = ops[1]
    assert te.subject_company is not None
    assert te.subject_company.domain == ops[0].domain
    assert te.subject_person is None


def test_attio_get_operations_person_only_emits_two_ops() -> None:
    w = _load("rb2b.visit.person_only.redacted.json")
    ops = w.attio_get_operations()
    assert [type(o).__name__ for o in ops] == ["UpsertPerson", "UpsertTrackingEvent"]
    te = ops[1]
    assert te.subject_person is not None


def test_attio_get_operations_person_and_company_emits_three_ops_in_order() -> None:
    w = _load("rb2b.visit.person_and_company.redacted.json")
    ops = w.attio_get_operations()
    assert [type(o).__name__ for o in ops] == [
        "UpsertCompany",
        "UpsertPerson",
        "UpsertTrackingEvent",
    ]
    assert ops[0].merge_only_if_empty == [
        "industry",
        "employee_count",
        "estimate_revenue",
    ]
    assert ops[1].merge_only_if_empty == ["title", "city", "state", "zipcode"]
    te = ops[2]
    assert te.external_id.startswith("rb2b:")
    assert te.source == "rb2b"
    assert te.event_type == "rb2b_visit"
    assert te.event_subtype in {"first_visit", "repeat_visit"}
    assert te.name == "RB2B Website visit"


def test_attio_get_operations_tracking_event_preserves_payload_in_body_json() -> None:
    """body_json keeps the entire raw payload for warehouse-side filtering
    even though every prod-schema field is also written to its own slug.
    Gives downstream consumers a single source of truth.
    """
    w = _load("rb2b.visit.person_and_company.redacted.json")
    te = w.attio_get_operations()[-1]
    body = json.loads(te.body_json)
    payload = body["payload"]
    assert payload["captured_url"] == w.payload.captured_url
    assert payload["referrer"] == w.payload.referrer
    assert payload["is_repeat_visit"] == w.payload.is_repeat_visit
    assert payload["city"] == w.payload.city
    assert payload["state"] == w.payload.state
    assert payload["zipcode"] == w.payload.zipcode
    assert payload["tags"] == w.payload.tags


def test_attio_get_operations_tracking_event_populates_prod_schema_fields() -> None:
    """The UpsertTrackingEvent op now carries every prod-schema field
    rather than burying them in body_json. See ai-0lv.
    """
    from src.rb2b.utils import split_rb2b_tags

    w = _load("rb2b.visit.person_and_company.redacted.json")
    te = w.attio_get_operations()[-1]
    assert te.captured_url == w.payload.captured_url
    assert te.referrer == w.payload.referrer
    assert te.is_repeat_visit == w.payload.is_repeat_visit
    assert te.tags == split_rb2b_tags(w.payload.tags)
    # Location reconstructed from structured parts; only assert the
    # locality fields rb2b actually delivers.
    assert te.location is not None
    assert te.location["locality"] == w.payload.city
    assert te.location["region"] == w.payload.state
    assert te.location["postcode"] == w.payload.zipcode
    # Both refs land when both resolve
    assert te.subject_person is not None
    assert te.subject_person.value == w.payload.business_email
    assert te.subject_company is not None
    assert te.subject_company.domain  # whatever extract_domain returned


def test_attio_get_operations_tracking_event_drops_location_when_payload_empty() -> (
    None
):
    """No city/state/zipcode → no location attribute. Avoids overwriting
    human-curated CRM data on subsequent visits with empty location."""
    raw = _load_raw("rb2b.visit.person_and_company.redacted.json")
    raw["payload"]["City"] = None
    raw["payload"]["State"] = None
    raw["payload"]["Zipcode"] = None
    w = Webhook.model_validate(raw)
    te = [
        o for o in w.attio_get_operations() if type(o).__name__ == "UpsertTrackingEvent"
    ][0]
    assert te.location is None


def test_attio_get_operations_repeat_visit_sets_subtype() -> None:
    w = _load("rb2b.visit.repeat_visit.redacted.json")
    te = [
        o for o in w.attio_get_operations() if type(o).__name__ == "UpsertTrackingEvent"
    ][0]
    assert te.event_subtype == "repeat_visit"


def test_attio_get_operations_first_visit_sets_subtype() -> None:
    """Falsy/missing is_repeat_visit maps to ``first_visit`` — safe default
    for the "is this a hot-prospect first touch?" query.
    """
    w = _load("rb2b.visit.person_only.redacted.json")
    te = [
        o for o in w.attio_get_operations() if type(o).__name__ == "UpsertTrackingEvent"
    ][0]
    assert te.event_subtype == "first_visit"


def test_linkedin_canonical_url_passes_through() -> None:
    w = _person_webhook_with_linkedin("https://www.linkedin.com/in/bob-jones")
    assert _person_op_linkedin(w) == "https://www.linkedin.com/in/bob-jones"


def test_linkedin_trailing_slash_is_collapsed() -> None:
    w = _person_webhook_with_linkedin("https://www.linkedin.com/in/bob-jones/")
    assert _person_op_linkedin(w) == "https://www.linkedin.com/in/bob-jones"


def test_linkedin_http_is_upgraded_to_https() -> None:
    w = _person_webhook_with_linkedin("http://linkedin.com/in/bob-jones")
    assert _person_op_linkedin(w) == "https://www.linkedin.com/in/bob-jones"


def test_linkedin_query_string_is_stripped() -> None:
    w = _person_webhook_with_linkedin("https://www.linkedin.com/in/bob-jones?trk=foo")
    assert _person_op_linkedin(w) == "https://www.linkedin.com/in/bob-jones"


def test_linkedin_company_url_is_dropped_but_person_still_created() -> None:
    w = _person_webhook_with_linkedin("https://www.linkedin.com/company/acme")
    ops = w.attio_get_operations()
    assert any(type(op).__name__ == "UpsertPerson" for op in ops)
    assert _person_op_linkedin(w) is None


def test_linkedin_none_passes_through_as_none() -> None:
    w = _person_webhook_with_linkedin(None)
    assert _person_op_linkedin(w) is None


def test_linkedin_empty_string_normalizes_to_none() -> None:
    w = _person_webhook_with_linkedin("")
    assert _person_op_linkedin(w) is None


def test_attio_get_operations_flat_envelope_normalizes() -> None:
    w = _load("rb2b.visit.flat_envelope.redacted.json")
    # _wrap_flat_payload should have normalized this into the envelope shape.
    assert w.event_id  # generated if missing
    ops = w.attio_get_operations()
    # Exact op makeup depends on the fixture's fields — assert ordering invariant.
    types = [type(o).__name__ for o in ops]
    assert types[-1] == "UpsertTrackingEvent"
    assert types[0] in ("UpsertCompany", "UpsertPerson")
