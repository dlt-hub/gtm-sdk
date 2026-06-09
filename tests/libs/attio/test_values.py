from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from libs.attio.models import MentionInput, PersonInput
from libs.attio.sdk_boundary import build_post_record_request
from libs.attio.values import (
    build_core_person_values,
    build_mention_values,
    build_optional_person_values,
    format_company_linkedin,
    format_linkedin,
    format_location,
    format_location_from_parts,
    normalize_linkedin_company_url,
    normalize_linkedin_url,
)


def test_format_location_city_mode_drops_street_granularity() -> None:
    value = format_location(
        "123 Main St, San Francisco, CA",
        country_code="US",
        mode="city",
    )
    assert value is not None
    assert value[0]["line_1"] is None
    assert value[0]["locality"] == "123 Main St"
    assert value[0]["region"] == "San Francisco"
    assert value[0]["country_code"] == "US"


def test_format_location_returns_none_without_country() -> None:
    # ai-sfp: stop silently tagging non-US strings as US. When the caller
    # can't supply a country code, the helper must skip — same contract as
    # format_location_from_parts (ai-ds6).
    assert format_location("Bengaluru, Karnataka, India", country_code=None) is None


def test_format_location_passes_through_non_us_country() -> None:
    value = format_location("Mumbai, MH", country_code="IN", mode="city")
    assert value is not None
    assert value[0]["locality"] == "Mumbai"
    assert value[0]["country_code"] == "IN"


def test_build_optional_person_values_serializes_notes_and_company() -> None:
    values = build_optional_person_values(
        company_domain="acme.com",
        notes="met at conference",
        location="San Francisco, CA",
        country_code="US",
        location_mode="city",
    )
    assert "associated_company" in values
    assert "notes" in values
    assert "primary_location" in values
    assert values["primary_location"][0]["country_code"] == "US"


def test_build_optional_person_values_skips_location_without_country() -> None:
    # ai-sfp contract: when the caller can't supply a country_code, the
    # primary_location write is skipped rather than written with a wrong
    # default. The other optional fields still flow through.
    values = build_optional_person_values(
        company_domain="acme.com",
        notes="met at conference",
        location="Bengaluru, Karnataka, India",
        country_code=None,
        location_mode="city",
    )
    assert "associated_company" in values
    assert "notes" in values
    assert "primary_location" not in values


def test_build_core_person_values_combines_primary_and_additional_emails() -> None:
    inp = PersonInput(
        email="a@example.com",
        additional_emails=["b@example.com", "a@example.com"],
    )
    values = build_core_person_values(inp)
    emails = [ev["email_address"] for ev in values["email_addresses"]]
    assert sorted(emails) == ["a@example.com", "b@example.com"]


def test_build_core_person_values_partial_omits_emails_when_not_explicit() -> None:
    inp = PersonInput(email="lookup@example.com", first_name="X")
    values = build_core_person_values(inp, partial=True)
    assert "email_addresses" not in values


def test_location_mode_raw_retains_line_1() -> None:
    # Direct unit on format_location now that build_optional_person_values
    # no longer writes primary_location without a country (see ai-sfp).
    value = format_location(
        "123 Main, SF, CA",
        country_code="US",
        mode="raw",
    )
    assert value is not None
    assert value[0]["line_1"] == "123 Main"
    assert value[0]["locality"] == "SF"
    assert value[0]["region"] == "CA"


# ---------- Octolens mentions ----------


def _sample_mention() -> MentionInput:
    return MentionInput(
        mention_url="https://reddit.com/r/x/comments/abc",
        last_action="mention_created",
        source_platform="reddit",
        source_id="abc",
        mention_title=None,
        mention_body="hello",
        mention_timestamp=datetime(2026, 5, 10, 11, 55, 53),
        author_handle="someuser",
        author_profile_url="https://reddit.com/u/someuser",
        author_avatar_url=None,
        relevance_score="0.85",
        relevance_comment="strong relevance",
        primary_keyword="deepline",
        keywords=["gtm", "outbound"],
        octolens_tags=["competitor"],
        sentiment="Positive",
        language="en",
        subreddit="r/example",
        view_id=42,
        view_name="GTM watch",
        bookmarked=False,
        image_url=None,
    )


HUMAN_OWNED = {"triage_status", "related_person", "related_company"}


def test_mention_builder_includes_all_writable_fields() -> None:
    values = build_mention_values(_sample_mention())
    assert "mention_url" in values
    assert "source_platform" in values
    assert "source_id" in values
    assert "relevance_score" in values
    assert "keywords" in values
    assert "octolens_tags" in values


def test_mention_builder_never_includes_human_owned_fields() -> None:
    values = build_mention_values(_sample_mention())
    assert HUMAN_OWNED.isdisjoint(values.keys())


def test_mention_builder_keeps_source_identity_on_update_action() -> None:
    """Regression for AI-290: source identity must survive `mention_updated`
    so that an assert-create from a missed `mention_created` still lands a
    complete record.
    """
    sample = _sample_mention()
    sample.last_action = "mention_updated"
    values = build_mention_values(sample)
    assert "source_platform" in values
    assert "source_id" in values


def test_mention_builder_handles_null_optionals() -> None:
    sample = _sample_mention()
    sample.mention_title = None
    sample.subreddit = None
    values = build_mention_values(sample)
    assert "mention_body" in values


def test_build_core_person_values_emits_github_handle_and_url() -> None:
    pi = PersonInput(
        github_handle="elviskahoro",
        github_url="https://github.com/elviskahoro",
    )
    values = build_core_person_values(pi)
    # The handle writes to the `github` slug (active in dev+prod); the
    # `github_handle` slug is archived in prod and deprecated. See ai-0jg.
    assert values["github"] == ["elviskahoro"]
    assert "github_handle" not in values
    assert values["github_url"] == ["https://github.com/elviskahoro"]


def test_build_core_person_values_passes_real_attio_request_model() -> None:
    """Regression for ai-bw6: github_handle/github_url must satisfy the real
    Attio SDK request model (text attrs require list values), not just our
    dict shape. A bare-string assignment fails here, not silently in prod —
    the "validate against the real model, not hand-authored fixtures" gotcha.
    """
    pi = PersonInput(
        github_handle="elviskahoro",
        github_url="https://github.com/elviskahoro",
    )
    values = build_core_person_values(pi)
    # Raises pydantic ValidationError if any text value is a bare string.
    build_post_record_request(values)


def test_attio_request_model_rejects_bare_string_text_value() -> None:
    """Locks in the SDK contract that drove ai-bw6: a text attribute assigned a
    bare string (the original bug shape) must fail the request model, proving
    the regression test above actually exercises the list requirement.
    """
    with pytest.raises(ValidationError):
        build_post_record_request({"github": "elviskahoro"})


def test_build_core_person_values_skips_github_when_absent() -> None:
    pi = PersonInput(email="a@example.com")
    values = build_core_person_values(pi)
    assert "github" not in values
    assert "github_handle" not in values
    assert "github_url" not in values


CANONICAL_LINKEDIN = "https://www.linkedin.com/in/foo-bar"


def test_normalize_linkedin_url_returns_canonical_form() -> None:
    cases = [
        "https://www.linkedin.com/in/foo-bar",
        "https://linkedin.com/in/foo-bar",
        "http://www.linkedin.com/in/foo-bar",
        "http://linkedin.com/in/foo-bar",
        "https://www.linkedin.com/in/foo-bar/",
        "https://www.linkedin.com/in/foo-bar?utm=x",
        "  https://www.LinkedIn.com/in/foo-bar  ",
    ]
    for url in cases:
        assert normalize_linkedin_url(url) == CANONICAL_LINKEDIN, url


def test_normalize_linkedin_url_rejects_non_profile() -> None:
    assert normalize_linkedin_url(None) is None
    assert normalize_linkedin_url("") is None
    assert normalize_linkedin_url("not a url") is None
    assert normalize_linkedin_url("https://www.linkedin.com/company/acme") is None
    assert normalize_linkedin_url("https://www.linkedin.com/feed/update/123") is None


def test_format_linkedin_emits_canonical_for_bare_handle() -> None:
    assert format_linkedin("foo-bar") == [CANONICAL_LINKEDIN]
    assert format_linkedin("foo-bar/") == [CANONICAL_LINKEDIN]


def test_format_linkedin_canonicalizes_profile_urls() -> None:
    assert format_linkedin("https://linkedin.com/in/foo-bar/") == [CANONICAL_LINKEDIN]
    assert format_linkedin("http://www.linkedin.com/in/foo-bar") == [CANONICAL_LINKEDIN]


def test_format_linkedin_passes_through_non_profile_urls() -> None:
    company_url = "https://www.linkedin.com/company/acme"
    assert format_linkedin(company_url) == [company_url]


def test_format_linkedin_returns_none_for_empty() -> None:
    assert format_linkedin(None) is None
    assert format_linkedin("") is None


CANONICAL_LINKEDIN_COMPANY = "https://www.linkedin.com/company/acme-corp"


def test_normalize_linkedin_company_url_returns_canonical_form() -> None:
    for url in (
        "https://www.linkedin.com/company/acme-corp",
        "https://linkedin.com/company/acme-corp",
        "http://www.linkedin.com/company/acme-corp",
        "http://linkedin.com/company/acme-corp",
        "https://www.linkedin.com/company/acme-corp/",
        "https://www.linkedin.com/company/acme-corp?trk=foo",
        "  https://www.linkedin.com/company/acme-corp  ",
    ):
        assert normalize_linkedin_company_url(url) == CANONICAL_LINKEDIN_COMPANY, url


def test_normalize_linkedin_company_url_rejects_non_company() -> None:
    assert normalize_linkedin_company_url(None) is None
    assert normalize_linkedin_company_url("") is None
    assert normalize_linkedin_company_url("not a url") is None
    assert (
        normalize_linkedin_company_url("https://www.linkedin.com/in/bob-jones") is None
    )
    assert (
        normalize_linkedin_company_url("https://www.linkedin.com/feed/update/123")
        is None
    )


def test_format_company_linkedin_canonicalizes_company_urls() -> None:
    assert format_company_linkedin(
        "https://linkedin.com/company/acme-corp/",
    ) == [CANONICAL_LINKEDIN_COMPANY]
    assert format_company_linkedin(
        "http://www.linkedin.com/company/acme-corp?utm=x",
    ) == [CANONICAL_LINKEDIN_COMPANY]


def test_format_company_linkedin_rejects_profile_urls() -> None:
    # Profile URLs must not pollute the Company linkedin slug. The rb2b
    # discriminator routes /in/ URLs to UpsertPerson.linkedin instead.
    assert format_company_linkedin("https://www.linkedin.com/in/bob-jones") is None


def test_format_company_linkedin_returns_none_for_empty() -> None:
    assert format_company_linkedin(None) is None
    assert format_company_linkedin("") is None


def test_build_tracking_event_values_minimum() -> None:
    from libs.attio.models import TrackingEventInput
    from libs.attio.values import build_tracking_event_values

    i = TrackingEventInput(
        external_id="rb2b:abc",
        source="rb2b",
        name="https://example.test/p",
        event_type="rb2b_visit",
        event_timestamp=datetime(2026, 5, 14, 9, 0),
        body_json='{"x":1}',
    )
    vs = build_tracking_event_values(i)
    # Required slugs always present, matching prod's writable surface
    assert vs["external_id"] == [{"value": "rb2b:abc"}]
    assert vs["source"] == [{"option": "rb2b"}]
    assert vs["name"] == [{"value": "https://example.test/p"}]
    assert vs["event_type"] == [{"option": "rb2b_visit"}]
    assert vs["timestamp"] == [{"value": "2026-05-14"}]  # date, not datetime
    assert vs["body"] == [{"value": '{"x":1}'}]
    # Optional slugs omitted when their input field is None / empty
    assert "event_subtype" not in vs
    assert "people" not in vs
    assert "company" not in vs
    assert "captured_url" not in vs
    assert "referrer" not in vs
    assert "is_repeat_visit" not in vs
    assert "tags" not in vs
    assert "location" not in vs
    # ``contact`` is a dev-only legacy slug — see build_tracking_event_values
    assert "contact" not in vs


def test_build_tracking_event_values_with_subtype_and_people_ref() -> None:
    """Prod uses the ``people`` slug, not ``contact`` (dev-only legacy).
    See the docstring on ``build_tracking_event_values`` for history.
    """
    from libs.attio.models import TrackingEventInput
    from libs.attio.values import build_tracking_event_values

    i = TrackingEventInput(
        external_id="rb2b:abc",
        source="rb2b",
        name="https://example.test/p",
        event_type="rb2b_visit",
        event_subtype="repeat_visit",
        event_timestamp=datetime(2026, 5, 14, 9, 0),
        body_json='{"x":1}',
        related_person_record_id="pe_1",
    )
    vs = build_tracking_event_values(i)
    assert vs["event_subtype"] == [{"option": "repeat_visit"}]
    assert vs["people"] == [
        {"target_object": "people", "target_record_id": "pe_1"},
    ]
    assert "contact" not in vs


def test_build_tracking_event_values_truncates_timestamp_to_day() -> None:
    """Live schema's ``timestamp`` attribute is a date, not a datetime."""
    from libs.attio.models import TrackingEventInput
    from libs.attio.values import build_tracking_event_values

    i = TrackingEventInput(
        external_id="rb2b:abc",
        source="rb2b",
        name="x",
        event_type="rb2b_visit",
        event_timestamp=datetime(2026, 5, 14, 23, 59, 59),
        body_json="{}",
    )
    vs = build_tracking_event_values(i)
    assert vs["timestamp"] == [{"value": "2026-05-14"}]


def test_build_tracking_event_values_source_is_select_shape() -> None:
    """``source`` must use the Attio select shape ``[{"option": ...}]`` so
    Attio-side filters and views can group by emitter. ai-ztm."""
    from libs.attio.models import TrackingEventInput
    from libs.attio.values import build_tracking_event_values

    i = TrackingEventInput(
        external_id="caldotcom:meeting_ended:uid:a@b.test",
        source="caldotcom",
        name="meeting ended",
        event_type="meeting_ended",
        event_timestamp=datetime(2026, 5, 14, 9, 0),
        body_json="{}",
    )
    vs = build_tracking_event_values(i)
    assert vs["source"] == [{"option": "caldotcom"}]


def test_format_location_from_parts_full_address() -> None:
    loc = format_location_from_parts(
        city="Cape Elizabeth",
        state="ME",
        zipcode="04107",
        country_code="US",
    )
    assert loc == {
        "line_1": None,
        "line_2": None,
        "line_3": None,
        "line_4": None,
        "locality": "Cape Elizabeth",
        "region": "ME",
        "postcode": "04107",
        "country_code": "US",
        "latitude": None,
        "longitude": None,
    }


def test_format_location_from_parts_all_empty_returns_none() -> None:
    # Empty inputs must not produce a sentinel object — that would still
    # be a write against Attio and overwrite human-curated data on repeat
    # visits.
    assert format_location_from_parts(None, None, None, country_code="US") is None
    assert format_location_from_parts("", "", "", country_code="US") is None
    assert format_location_from_parts("  ", "  ", "  ", country_code="US") is None


def test_format_location_from_parts_partial_zip_only() -> None:
    loc = format_location_from_parts(None, None, "04107", country_code="US")
    assert loc is not None
    assert loc["postcode"] == "04107"
    assert loc["locality"] is None
    assert loc["region"] is None


def test_format_location_from_parts_strips_whitespace() -> None:
    loc = format_location_from_parts(
        "  Brooklyn  ",
        "  NY  ",
        "  11201  ",
        country_code="US",
    )
    assert loc is not None
    assert loc["locality"] == "Brooklyn"
    assert loc["region"] == "NY"
    assert loc["postcode"] == "11201"


def test_format_location_from_parts_custom_country() -> None:
    loc = format_location_from_parts("Toronto", "ON", "M5V 2T6", country_code="CA")
    assert loc is not None
    assert loc["country_code"] == "CA"


def test_format_location_from_parts_returns_none_without_country() -> None:
    # Acceptance from ai-ds6: a fully-populated locality with no country
    # must skip rather than misattribute (the historical bug silently
    # tagged these as US).
    assert (
        format_location_from_parts(
            city="Bengaluru",
            state="Karnataka",
            zipcode=None,
            country_code=None,
        )
        is None
    )


def test_format_location_from_parts_india_country_code() -> None:
    loc = format_location_from_parts(
        city="Mumbai",
        state=None,
        zipcode=None,
        country_code="IN",
    )
    assert loc is not None
    assert loc["country_code"] == "IN"
    assert loc["locality"] == "Mumbai"


def test_build_tracking_event_values_full_surface() -> None:
    """Every prod-schema slug populated end-to-end. Lock in the wire shape
    so a future schema-drift can't silently break it. See ai-0lv.
    """
    from libs.attio.models import TrackingEventInput
    from libs.attio.values import build_tracking_event_values

    location = format_location_from_parts(
        "Cape Elizabeth",
        "ME",
        "04107",
        country_code="US",
    )
    i = TrackingEventInput(
        external_id="rb2b:abc",
        source="rb2b",
        name="RB2B Website visit",
        event_type="rb2b_visit",
        event_subtype="repeat_visit",
        event_timestamp=datetime(2026, 5, 26, 3, 30),
        body_json='{"x":1}',
        captured_url="https://dlthub.com/blog/openflow",
        referrer="https://google.com",
        is_repeat_visit=True,
        tags=["b2b", "enterprise"],
        location=location,
        related_person_record_id="pe_1",
        related_company_record_id="co_1",
    )
    vs = build_tracking_event_values(i)
    assert vs["captured_url"] == [{"value": "https://dlthub.com/blog/openflow"}]
    assert vs["referrer"] == [{"value": "https://google.com"}]
    assert vs["is_repeat_visit"] == [{"value": True}]
    assert vs["tags"] == [{"option": "b2b"}, {"option": "enterprise"}]
    assert vs["location"] == [location]
    assert vs["people"] == [
        {"target_object": "people", "target_record_id": "pe_1"},
    ]
    assert vs["company"] == [
        {"target_object": "companies", "target_record_id": "co_1"},
    ]


def test_build_tracking_event_values_is_repeat_visit_false_emits() -> None:
    """Explicit False is meaningful (this visit is NOT a repeat) and must
    land on the row; only None suppresses the write."""
    from libs.attio.models import TrackingEventInput
    from libs.attio.values import build_tracking_event_values

    i = TrackingEventInput(
        external_id="rb2b:abc",
        source="rb2b",
        name="x",
        event_type="rb2b_visit",
        event_timestamp=datetime(2026, 5, 14, 9, 0),
        body_json="{}",
        is_repeat_visit=False,
    )
    vs = build_tracking_event_values(i)
    assert vs["is_repeat_visit"] == [{"value": False}]
