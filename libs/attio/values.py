from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any, Literal

from libs.attio.models import (
    MeetingInput,
    MentionInput,
    PersonInput,
    TrackingEventInput,
)

_LINKEDIN_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?linkedin\.com/in/([^/?#]+)",
    re.IGNORECASE,
)

_LINKEDIN_COMPANY_RE = re.compile(
    r"^https?://(?:www\.)?linkedin\.com/company/([^/?#]+)",
    re.IGNORECASE,
)


def normalize_linkedin_url(url: str | None) -> str | None:
    """Canonicalize a LinkedIn profile URL to ``https://www.linkedin.com/in/<handle>``.

    Returns ``None`` for falsy input or URLs that are not profile (``/in/<handle>``)
    shape — e.g. company, feed, or posts URLs — so callers can decide whether to
    pass the original through or reject it.
    """
    if not url:
        return None
    match = _LINKEDIN_PROFILE_RE.match(url.strip())
    if not match:
        return None
    handle = match.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{handle}"


def normalize_linkedin_company_url(url: str | None) -> str | None:
    """Canonicalize a LinkedIn **company** URL to ``https://www.linkedin.com/company/<slug>``.

    Counterpart to ``normalize_linkedin_url`` which handles ``/in/<handle>``
    profile URLs. rb2b's ``linkedin_url`` field is sometimes the visitor's
    profile (no business_email, but identifiable individual) and sometimes the
    company page (anonymous traffic for a known company); callers discriminate
    by which normalizer returns non-None.

    Returns ``None`` for falsy input or URLs that are not company-page shape so
    profile URLs and other shapes fall through to the profile normalizer.
    """
    if not url:
        return None
    match = _LINKEDIN_COMPANY_RE.match(url.strip())
    if not match:
        return None
    slug = match.group(1).rstrip("/")
    return f"https://www.linkedin.com/company/{slug}"


def normalize_email_address_list(candidates: Iterable[str | None]) -> list[str]:
    """Strip, drop empties, dedupe case-insensitively; keep first-seen spelling."""
    out: list[str] = []
    seen_set: set[str] = set()
    for raw in candidates:
        if raw is None:
            continue
        e = str(raw).strip()
        if not e:
            continue
        key = e.casefold()
        if key in seen_set:
            continue
        seen_set.add(key)
        out.append(e)
    return out


def format_email(email: str) -> list[str] | None:
    if not email:
        return None
    return [email]


def format_email_addresses_for_write(
    emails: list[str],
) -> list[dict[str, str]] | None:
    """Format email addresses for Attio person record write.

    Wraps each email in {"email_address": "..."} structure.

    Gotcha: Attio's `email_addresses` attribute applies email validation and
    rejects RFC-2606 reserved TLDs (`.test`, `.invalid`, `.example`, `.localhost`)
    with the *misleading* error
    ``An invalid value was passed to attribute with slug "email_addresses"``.
    The error names the attribute, not the value, so it reads like a schema or
    shape problem. It isn't — the writer below is shape-correct; the offending
    input is a value with a reserved TLD. Commit ``5ac70af`` misdiagnosed this
    and disabled the writer entirely; AI-291 restored it. Use ``example.com``
    (also RFC-reserved but accepted by Attio) for any probe/fixture emails.
    """
    if not emails:
        return None
    return [{"email_address": email} for email in emails]


def format_name(
    first_name: str | None,
    last_name: str | None,
) -> list[dict[str, str]] | None:
    if not first_name and not last_name:
        return None

    full_name_parts: list[str] = []
    if first_name:
        full_name_parts.append(first_name)
    if last_name:
        full_name_parts.append(last_name)

    name_data: dict[str, str] = {
        "first_name": first_name or "",
        "last_name": last_name or "",
        "full_name": " ".join(full_name_parts),
    }

    return [name_data]


def format_phone(phone: str | None) -> list[dict[str, str]] | None:
    if not phone:
        return None

    phone_str: str = str(phone)
    phone_data: dict[str, str] = {"original_phone_number": phone_str}

    match True:
        case _ if phone_str.startswith(("+1", "1")):
            phone_data["country_code"] = "US"
        case _ if phone_str.startswith("+44"):
            phone_data["country_code"] = "GB"
        case _ if phone_str.startswith("+33"):
            phone_data["country_code"] = "FR"
        case _:
            phone_data["country_code"] = "US"

    return [phone_data]


def format_linkedin(url: str | None) -> list[str] | None:
    if not url:
        return None
    if not url.startswith("http"):
        return [f"https://www.linkedin.com/in/{url.rstrip('/')}"]
    canonical = normalize_linkedin_url(url)
    return [canonical or url]


def format_location(
    location: str | None,
    *,
    country_code: str | None,
    mode: Literal["raw", "city"] = "city",
) -> list[dict[str, Any]] | None:
    """Build an Attio ``primary_location`` value from a free-form string.

    ``country_code`` is required by contract (no default), keyword-only. Pass
    an ISO-3166-1 alpha-2 code (e.g. ``"US"``, ``"IN"``) or ``None``. A prior
    version of this helper defaulted to ``"US"``, which silently misattributed
    non-US data parsed from LinkedIn/CRM strings; see ai-sfp and the sibling
    fix in ai-ds6.

    Returns ``None`` when:
    - ``location`` is empty, or
    - ``country_code`` is ``None`` — an Attio location without a country is
      incomplete, and emitting one would still register as a write and
      overwrite human-curated data on repeat visits.

    ``mode="city"`` treats the first comma-separated token as locality and
    the second as region (anything after is ignored — Attio's ``location``
    has no free country slot, only ``country_code``). ``mode="raw"`` keeps
    the first token as ``line_1``.
    """
    if not location:
        return None
    if country_code is None:
        return None

    parts: list[str] = str(location).split(",")

    if mode == "raw":
        line_1 = parts[0].strip() if len(parts) > 0 else None
        locality = parts[1].strip() if len(parts) > 1 else None
        region = parts[2].strip() if len(parts) > 2 else None
    else:
        line_1 = None
        locality = parts[0].strip() if len(parts) > 0 else None
        region = parts[1].strip() if len(parts) > 1 else None

    location_data: dict[str, Any] = {
        "line_1": line_1,
        "line_2": None,
        "line_3": None,
        "line_4": None,
        "locality": locality,
        "region": region,
        "postcode": None,
        "country_code": country_code,
        "latitude": None,
        "longitude": None,
    }
    return [location_data]


def format_location_from_parts(
    city: str | None,
    state: str | None,
    zipcode: str | None,
    country_code: str | None,
) -> dict[str, Any] | None:
    """Build an Attio ``location`` attribute value from structured parts.

    ``country_code`` is required by contract (no default). Callers must pass
    an ISO-3166-1 alpha-2 code (e.g. ``"US"``, ``"IN"``) or ``None``. A prior
    version of this helper defaulted to ``"US"``, which silently misattributed
    non-US data when the country lookup failed or the caller forgot the arg;
    see ai-ds6.

    Returns ``None`` when:
    - every locality field (city/state/zipcode) is empty, or
    - ``country_code`` is ``None`` — an Attio location without a country is
      incomplete, and emitting one would still register as a write and
      overwrite human-curated data on repeat visits.

    Returns the inner dict (not the ``[{...}]`` list shape) because Attio's
    ``location`` attribute is single-valued — the caller wraps it before
    sending. This keeps ``location`` symmetric with the structured
    ``MeetingExternalRef`` rather than the multi-valued select/text wrappers.
    """
    city_clean = (city or "").strip() or None
    state_clean = (state or "").strip() or None
    zip_clean = (zipcode or "").strip() or None
    if not (city_clean or state_clean or zip_clean):
        return None
    if country_code is None:
        return None
    return {
        "line_1": None,
        "line_2": None,
        "line_3": None,
        "line_4": None,
        "locality": city_clean,
        "region": state_clean,
        "postcode": zip_clean,
        "country_code": country_code,
        "latitude": None,
        "longitude": None,
    }


def format_company_ref(domain: str | None) -> list[dict[str, Any]] | None:
    if not domain:
        return None
    return [{"target_object": "companies", "domains": [{"domain": domain}]}]


def format_person_record_ref(record_id: str | None) -> list[dict[str, Any]] | None:
    if not record_id:
        return None
    return [{"target_object": "people", "target_record_id": record_id}]


def format_company_record_ref(record_id: str | None) -> list[dict[str, Any]] | None:
    if not record_id:
        return None
    return [{"target_object": "companies", "target_record_id": record_id}]


def format_notes(notes: str | None) -> list[str] | None:
    if not notes:
        return None
    return [notes]


def format_company_name(name: str) -> list[dict[str, str]]:
    return [{"value": name}]


_DOMAIN_LABEL_RE = re.compile(
    r"^(?=.{1,63}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z",
)


def looks_like_domain(value: object) -> bool:
    """Cheap shape check: does ``value`` plausibly look like a bare hostname?

    Validates per-label (RFC 1035-ish): each dot-separated label must be
    non-empty, contain only ASCII letters/digits/hyphens, and must not start
    or end with a hyphen. Rejects URL fragments (``acme.com?q=…``,
    ``acme.com#x``), comma-separated lists, schemes, paths, leading/trailing
    dots, and labels like ``acme-`` / ``-acme`` / ``acme.-foo``.

    Rejects IPv4-literal values (``0.0.0.0``, ``123.45.67.89``) explicitly
    via ``ipaddress.IPv4Address`` rather than a coarse "must have alpha"
    check — fully-numeric hostnames like ``123.com`` are legitimate and
    must be accepted (roborev finding).

    The goal is to keep the orchestrator's row outcome classification
    accurate — anything that gets through here is what Attio actually has
    to validate.

    Non-string inputs (e.g. ``None``, integers, lists) are rejected without
    raising, so callers can pass raw values from external responses safely
    (roborev finding).
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    if "." not in value:
        return False
    # RFC 1035: total hostname length cannot exceed 253 octets. Enforcing this
    # here keeps preview/apply parity — without it, an overlong-but-well-formed
    # hostname slips past ``looks_like_domain`` in preview and then gets
    # rejected by Attio on apply (roborev finding).
    if len(value) > 253:
        return False
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        pass
    else:
        return False  # IPv4 literal, not a hostname
    labels = value.split(".")
    for label in labels:
        if not _DOMAIN_LABEL_RE.match(label):
            return False  # empty label, invalid chars, or leading/trailing hyphen
    return True


def format_company_domains(domain: str | None) -> list[dict[str, str]] | None:
    if not domain:
        return None
    # Normalize before validating/returning — Attio stores the domain
    # verbatim, so trailing whitespace from upstream payloads would
    # otherwise leak through (roborev finding).
    normalized = domain.strip().lower()
    if not looks_like_domain(normalized):
        return None
    return [{"domain": normalized}]


def format_company_linkedin(url: str | None) -> list[str] | None:
    """Format a LinkedIn company URL for Attio Company ``linkedin`` slug.

    The slug is a single-value ``text`` attribute on the standard ``companies``
    object (confirmed prod 2026-05-26 via ``tmp/probe_company_linkedin_write.py``),
    so the write shape is the same as ``format_company_description``:
    ``[<url_string>]``. Pre-normalizes through ``normalize_linkedin_company_url``
    so we never write a profile (``/in/...``) or otherwise malformed URL into
    the company linkedin slug.
    """
    canonical = normalize_linkedin_company_url(url)
    if not canonical:
        return None
    return [canonical]


def format_company_description(description: str | None) -> list[str] | None:
    if not description:
        return None
    return [description]


def build_core_person_values(
    input: PersonInput,
    *,
    partial: bool = False,
    email_addresses: list[str] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {}

    if email_addresses is not None:
        formatted = format_email_addresses_for_write(email_addresses)
        if formatted:
            values["email_addresses"] = formatted
    elif not partial:
        combined = normalize_email_address_list([input.email, *input.additional_emails])
        formatted = format_email_addresses_for_write(combined)
        if formatted:
            values["email_addresses"] = formatted

    name = format_name(input.first_name, input.last_name)
    if name:
        values["name"] = name

    phone = format_phone(input.phone)
    if phone:
        values["phone_numbers"] = phone

    linkedin = format_linkedin(input.linkedin)
    if linkedin:
        values["linkedin"] = linkedin

    if input.github_handle:
        # Attio text attributes require a list value (mirrors `linkedin` above,
        # which `format_linkedin` returns as list[str]). A bare string fails the
        # SDK's PostV2ObjectsObjectRecordsDataRequest validation. See ai-bw6.
        values["github_handle"] = [input.github_handle]
    if input.github_url:
        values["github_url"] = [input.github_url]

    return values


def build_optional_person_values(
    *,
    company_domain: str | None,
    notes: str | None,
    location: str | None,
    country_code: str | None,
    location_mode: Literal["raw", "city"] = "city",
) -> dict[str, Any]:
    """Build the optional-field subset of a person values dict.

    ``country_code`` is required by contract (no default) — pairs with
    ``location`` and is forwarded to ``format_location``. Pass an
    ISO-3166-1 alpha-2 code or ``None``; when ``None``, the
    ``primary_location`` write is skipped (see ai-sfp / ai-ds6).
    """
    values: dict[str, Any] = {}

    company = format_company_ref(company_domain)
    if company:
        values["associated_company"] = company

    location_value = format_location(
        location,
        country_code=country_code,
        mode=location_mode,
    )
    if location_value:
        values["primary_location"] = location_value

    notes_value = format_notes(notes)
    if notes_value:
        values["notes"] = notes_value

    return values


def build_meeting_payload(input: MeetingInput) -> dict[str, Any]:
    """Build the JSON body for POST /v2/meetings.

    `external_ref` is a structured object per Attio's SDK: `{ical_uid, provider,
    is_recurring, original_start_time}`. Idempotency is keyed off `ical_uid`.
    Datetimes serialize as `{"datetime": <iso8601>}`; the iso8601 offset carries
    timezone info so no separate `timezone` key is emitted.
    """
    ref = input.external_ref
    data: dict[str, Any] = {
        "external_ref": {
            "ical_uid": ref.ical_uid,
            "provider": ref.provider,
            "is_recurring": ref.is_recurring,
            "original_start_time": ref.original_start_time,
        },
        "title": input.title,
        "description": input.description,
        "is_all_day": input.is_all_day,
        "start": {"datetime": input.start.isoformat()},
        "end": {"datetime": input.end.isoformat()},
        "participants": [
            {
                "email_address": p.email_address,
                "is_organizer": p.is_organizer,
                "status": p.status,
            }
            for p in input.participants
        ],
        "linked_records": [
            {"object": lr.object, "record_id": lr.record_id}
            for lr in input.linked_records
        ],
    }
    return {"data": data}


# ---------- Octolens mentions ----------

# Fields the webhook MUST NEVER write. Enforced both here (builder-level) and
# at the type level by MentionInput not declaring them. Belt-and-suspenders.
# related_person is excluded from this set because it's written programmatically
# by the dispatcher when linking LinkedIn mentions to Person records.
_HUMAN_OWNED_MENTION_FIELDS: frozenset[str] = frozenset(
    {"triage_status", "related_company"},
)


def _scalar_value(v: Any) -> list[dict[str, Any]]:
    """Wrap a scalar into Attio's standard ``[{"value": ...}]`` list shape."""
    return [{"value": v}]


def _select_value(option: str) -> list[dict[str, str]]:
    """Single-select attribute shape: ``[{"option": "<title>"}]``."""
    return [{"option": option}]


def _multiselect_values(options: list[str]) -> list[dict[str, str]]:
    return [{"option": opt} for opt in options]


def build_mention_values(input: MentionInput) -> dict[str, Any]:
    """Build the Attio values payload for a social_mention assert call.

    Always includes `source_platform` and `source_id`. Attio's assert endpoint
    may create the record on the first delivery the system processes for a
    given `mention_url` (e.g. a `mention_updated` that arrives before its
    `mention_created`), and the new record must carry its source identity.
    Both fields are derived from the webhook payload itself and are invariant
    per mention URL, so re-sending them on a true update is a no-op rewrite
    of identical values.
    """
    values: dict[str, Any] = {}

    values["mention_url"] = _scalar_value(input.mention_url)
    values["last_action"] = _select_value(input.last_action)
    values["source_platform"] = _select_value(input.source_platform)
    values["source_id"] = _scalar_value(input.source_id)
    values["mention_body"] = _scalar_value(input.mention_body)
    values["mention_timestamp"] = _scalar_value(input.mention_timestamp.isoformat())
    values["author_handle"] = _scalar_value(input.author_handle)
    values["primary_keyword"] = _scalar_value(input.primary_keyword)
    values["bookmarked"] = _scalar_value(input.bookmarked)

    if input.mention_title is not None:
        values["mention_title"] = _scalar_value(input.mention_title)
    if input.author_profile_url is not None:
        values["author_profile_url"] = _scalar_value(input.author_profile_url)
    if input.author_avatar_url is not None:
        values["author_avatar_url"] = _scalar_value(input.author_avatar_url)
    if input.relevance_score is not None:
        values["relevance_score"] = _select_value(input.relevance_score)
    if input.relevance_comment is not None:
        values["relevance_comment"] = _scalar_value(input.relevance_comment)
    if input.sentiment is not None:
        values["sentiment"] = _select_value(input.sentiment)
    if input.language is not None:
        values["language"] = _scalar_value(input.language)
    if input.subreddit is not None:
        values["subreddit"] = _scalar_value(input.subreddit)
    if input.view_id is not None:
        values["view_id"] = _scalar_value(input.view_id)
    if input.view_name is not None:
        values["view_name"] = _scalar_value(input.view_name)
    if input.image_url is not None:
        values["image_url"] = _scalar_value(input.image_url)

    if input.keywords:
        values["keywords"] = _multiselect_values(input.keywords)
    if input.octolens_tags:
        values["octolens_tags"] = _multiselect_values(input.octolens_tags)

    if input.related_person_record_id is not None:
        person_ref = format_person_record_ref(input.related_person_record_id)
        if person_ref:
            values["related_person"] = person_ref

    # Guard the invariant. Should be unreachable since MentionInput doesn't
    # declare these fields, but kept here in case the model grows.
    for forbidden in _HUMAN_OWNED_MENTION_FIELDS:
        assert forbidden not in values, f"Webhook tried to write {forbidden!r}"

    return values


def build_tracking_event_values(
    input: TrackingEventInput,
) -> dict[str, list[dict[str, Any]]]:
    """Build the Attio values dict for a tracking_events record write.

    Emits the full writable surface of the live prod ``tracking_events``
    schema. Confirmed on prod 2026-05-26 via
    ``tmp/inspect_tracking_events_schema.py``:

    - ``external_id`` (text) — idempotency key
    - ``source`` (select) — emitter identifier (``rb2b``, ``caldotcom``, ...);
      JIT-seeded in ``find_or_create_tracking_event``. See ai-ztm.
    - ``name`` (text)
    - ``event_type`` (select) — JIT-seeded
    - ``event_subtype`` (select, optional) — JIT-seeded when present
    - ``timestamp`` (date) — truncated to day precision; sub-day ordering
      survives inside ``body_json`` and in the GCS raw landing
    - ``body`` (text) — JSON-stringified raw payload
    - ``people`` (record-reference, optional) — Person link.

      Plan-02 wrote to a ``contact`` slug instead, which only exists on
      dev. PR #111 fixed the cal.com lifecycle path to use ``people``; PR
      ai-0lv (this change) extends the fix to the rb2b path so prod writes
      stop silently dropping the person link.
    - ``company`` (record-reference, optional) — Company link, resolved
      via the dispatcher's ``LookupTable`` from the rb2b ``UpsertCompany``
      that runs earlier in the same plan
    - ``captured_url`` (text, optional)
    - ``referrer`` (text, optional)
    - ``is_repeat_visit`` (checkbox, optional)
    - ``tags`` (multi-select, optional) — JIT-seeded
    - ``location`` (location, optional) — structured locality/region/postcode

    ``owner`` is left for human curation (the cal.com lifecycle writer
    hardcodes it for meetings, but the rb2b path leaves it unset so
    Attio's default assignment rules apply).
    """
    values: dict[str, list[dict[str, Any]]] = {
        "external_id": _scalar_value(input.external_id),
        "source": _select_value(input.source),
        "name": _scalar_value(input.name),
        "event_type": _select_value(input.event_type),
        "timestamp": _scalar_value(input.event_timestamp.date().isoformat()),
        "body": _scalar_value(input.body_json),
    }
    if input.event_subtype is not None:
        values["event_subtype"] = _select_value(input.event_subtype)
    if input.related_person_record_id is not None:
        person_ref = format_person_record_ref(input.related_person_record_id)
        if person_ref:
            values["people"] = person_ref
    if input.related_company_record_id is not None:
        company_ref = format_company_record_ref(input.related_company_record_id)
        if company_ref:
            values["company"] = company_ref
    if input.captured_url:
        values["captured_url"] = _scalar_value(input.captured_url)
    if input.referrer:
        values["referrer"] = _scalar_value(input.referrer)
    if input.is_repeat_visit is not None:
        values["is_repeat_visit"] = _scalar_value(input.is_repeat_visit)
    if input.tags:
        values["tags"] = _multiselect_values(input.tags)
    if input.location is not None:
        values["location"] = [input.location]
    return values


_LEGAL_SUFFIX_RE = re.compile(
    r",?\s+(inc\.?|llc|gmbh|ltd\.?|limited)\.?$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_company_name(name: str) -> str:
    """Lowercase, strip trailing legal suffix, collapse punctuation/whitespace.

    Used by both ``libs.attio.companies.find_company_by_name`` and the
    Snowflake loader's per-row dedup to match Attio Companies whose stored
    name differs only in legal suffix or punctuation from the CSV value.
    """
    stripped = name.strip()
    suffix_stripped = _LEGAL_SUFFIX_RE.sub("", stripped)
    no_punct = _PUNCT_RE.sub(" ", suffix_stripped)
    return " ".join(no_punct.lower().split())
