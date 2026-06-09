from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, Literal

from libs.attio.client import get_client
from libs.attio.contracts import (
    ErrorEntry,
    ReliabilityEnvelope,
    SkippedField,
    WarningEntry,
)
from libs.attio.errors import (
    AttioConflictError,
    AttioNotFoundError,
    AttioValidationError,
    ConflictError,
    SchemaMismatchError,
    classify_error,
    translate_modal_signature_error,
)
from libs.attio.models import PersonInput, PersonResult, PersonSearchResult
from libs.attio.sdk_boundary import (
    build_patch_record_request,
    build_post_record_request,
    extract_exception_body_text,
    is_uniqueness_conflict,
    is_unknown_filter_attribute,
)
from libs.attio.values import (
    build_core_person_values,
    build_optional_person_values,
    normalize_email_address_list,
)

OPTIONAL_FIELD_WARNING_CODES = {
    "associated_company": "attio_associated_company_field_unavailable",
    "company": "attio_associated_company_field_unavailable",
    "notes": "attio_notes_field_unavailable",
    "primary_location": "attio_location_field_unavailable",
}

OPTIONAL_FIELD_ALIASES = {
    "associated_company": "company",
    "company": "associated_company",
}

logger = logging.getLogger(__name__)


_LINKEDIN_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?linkedin\.com/in/([^/?#]+)",
    re.IGNORECASE,
)


def _linkedin_url_variants(linkedin: str) -> list[str]:
    """Return URL variants Attio records may store for a LinkedIn profile.

    Historical writers in this repo (and user-provided URLs) differ on scheme
    (``http`` vs ``https``), host prefix (``www.`` vs bare), and trailing
    slash. To avoid creating duplicate People for the same profile, search
    across the full Cartesian product of those axes plus the original input.
    """
    match = _LINKEDIN_PROFILE_RE.match(linkedin)
    if not match:
        return [linkedin]
    handle = match.group(1).rstrip("/")
    variants: list[str] = [linkedin]
    for scheme in ("https", "http"):
        for host in ("www.linkedin.com", "linkedin.com"):
            base = f"{scheme}://{host}/in/{handle}"
            variants.append(base)
            variants.append(f"{base}/")
    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]


def _result_envelope(
    *,
    success: bool,
    partial_success: bool,
    action: Literal["searched", "created", "updated", "noop", "failed"],
    record_id: str | None,
    warnings: list[WarningEntry] | None = None,
    skipped_fields: list[SkippedField] | None = None,
    errors: list[ErrorEntry] | None = None,
    meta: dict[str, Any] | None = None,
) -> ReliabilityEnvelope:
    merged_meta = {"output_schema_version": "v1"}
    if meta:
        merged_meta.update(meta)
    return ReliabilityEnvelope(
        success=success,
        partial_success=partial_success,
        action=action,
        record_id=record_id,
        warnings=warnings or [],
        skipped_fields=skipped_fields or [],
        errors=errors or [],
        meta=merged_meta,
    )


def _extract_email_addresses_from_record_values(values: Any) -> list[str]:
    if isinstance(values, dict):
        email_values = values.get("email_addresses", [])
    else:
        email_values = getattr(values, "email_addresses", []) or []
    out: list[str] = []
    for ev in email_values:
        if hasattr(ev, "email_address"):
            out.append(ev.email_address)
    return out


def _extract_result(data: Any) -> PersonResult:
    record_id: str = data.id.record_id

    email_addresses: list[str] = []
    name: str | None = None

    email_values = data.values.get("email_addresses", [])
    for ev in email_values:
        if hasattr(ev, "email_address"):
            email_addresses.append(ev.email_address)

    name_values = data.values.get("name", [])
    for nv in name_values:
        if hasattr(nv, "full_name"):
            name = nv.full_name
            break

    return PersonResult(
        record_id=record_id,
        email_addresses=email_addresses,
        name=name,
        raw={},
    )


def _extract_search_result(data: Any) -> PersonSearchResult:
    record_id: str = data.id.record_id

    name: str | None = None
    for nv in data.values.get("name", []):
        if hasattr(nv, "full_name"):
            name = nv.full_name
            break

    email_addresses: list[str] = []
    for ev in data.values.get("email_addresses", []):
        if hasattr(ev, "email_address"):
            email_addresses.append(ev.email_address)

    phone_numbers: list[str] = []
    for pv in data.values.get("phone_numbers", []):
        if hasattr(pv, "original_phone_number"):
            phone_numbers.append(pv.original_phone_number)
        elif hasattr(pv, "phone_number"):
            phone_numbers.append(pv.phone_number)

    linkedin: str | None = None
    for lv in data.values.get("linkedin", []):
        if hasattr(lv, "value"):
            linkedin = lv.value
            break

    location: str | None = None
    for loc in data.values.get("primary_location", []):
        if hasattr(loc, "locality"):
            parts = [
                getattr(loc, "locality", None),
                getattr(loc, "region", None),
                getattr(loc, "country_code", None),
            ]
            location = ", ".join(p for p in parts if p)
            break

    company_record_id: str | None = None
    for cv in data.values.get("company", []):
        if hasattr(cv, "target_record_id"):
            company_record_id = cv.target_record_id
            break

    return PersonSearchResult(
        record_id=record_id,
        name=name,
        email_addresses=email_addresses,
        phone_numbers=phone_numbers,
        linkedin=linkedin,
        location=location,
        company=company_record_id,
    )


def _search_people_raw(
    name: str | None = None,
    email: str | None = None,
    email_domain: str | None = None,
    phone: str | None = None,
    company: str | None = None,
    linkedin: str | None = None,
    github_handle: str | None = None,
    sample: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> list[PersonSearchResult]:
    conditions: list[dict[str, Any]] = []
    if name:
        conditions.append({"name": {"$contains": name}})
    if email:
        conditions.append({"email_addresses": email})
    if email_domain:
        domain = email_domain.lstrip("@")
        conditions.append(
            {"email_addresses": {"email_address": {"$ends_with": f"@{domain}"}}},
        )
    if phone:
        conditions.append({"phone_numbers": {"$contains": phone}})
    if linkedin:
        variants = _linkedin_url_variants(linkedin)
        if len(variants) == 1:
            conditions.append({"linkedin": variants[0]})
        else:
            conditions.append({"$or": [{"linkedin": v} for v in variants]})
    if github_handle:
        # Filter on the `github` slug — the attribute active on people in both
        # dev and prod. The kwarg/field name stays `github_handle` (conceptual
        # identity); only the Attio slug is `github`. `github_handle` is archived
        # in prod, so a filter on it returns a filter_error. See ai-0jg.
        conditions.append({"github": github_handle})

    if not sample and not conditions and not company:
        raise AttioValidationError(
            "Provide at least one search criterion or use --sample to browse recent records.",
        )

    with get_client() as client:
        if company:
            company_filter: dict[str, Any]
            if "." in company:
                company_filter = {
                    "$or": [
                        {"name": {"$contains": company}},
                        {"domains": {"domain": {"$eq": company}}},
                    ],
                }
            else:
                company_filter = {"name": {"$contains": company}}
            company_response = client.records.post_v2_objects_object_records_query(
                object="companies",
                filter_=company_filter,
                limit=10,
            )
            if not company_response.data:
                return []
            company_ids = [r.id.record_id for r in company_response.data]
            company_or = [{"company": {"target_record_id": cid}} for cid in company_ids]
            if len(company_or) == 1:
                conditions.append(company_or[0])
            else:
                conditions.append({"$or": company_or})

        if len(conditions) == 0:
            filter_: dict[str, Any] = {}
        elif len(conditions) == 1:
            filter_ = conditions[0]
        else:
            filter_ = {"$and": conditions}

        try:
            response = client.records.post_v2_objects_object_records_query(
                object="people",
                filter_=filter_,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            # A filter slug the people object doesn't define (e.g. `github`
            # if it were archived/absent) makes Attio return a `filter_error`
            # the SDK can't unmarshal, surfacing as an opaque
            # ResponseValidationError. Translate it into a typed, classifiable
            # SchemaMismatchError so callers see `schema_mismatch` rather than a
            # raw handler_exception, and so an optional UpsertPerson can degrade
            # cleanly (ai-0ex). `from None` drops the SDK pydantic chain.
            if is_unknown_filter_attribute(exc):
                # `github` is the only filter slug among the supported search
                # criteria that isn't a built-in people attribute, so it is the
                # offender when this fires today. Name it explicitly for an
                # actionable envelope; keep generic if it wasn't the input.
                offending = "github" if github_handle else None
                raise SchemaMismatchError(
                    "people object has no filter attribute"
                    + (f" '{offending}'" if offending else " in the query"),
                    field=offending,
                ) from None
            raise
        results = [_extract_search_result(record) for record in response.data]

        company_ids = {r.company for r in results if r.company}
        if company_ids:
            name_map: dict[str, str] = {}
            for cid in company_ids:
                try:
                    cr = client.records.get_v2_objects_object_records_record_id_(
                        object="companies",
                        record_id=cid,
                    )
                    for nv in cr.data.values.get("name", []):
                        if hasattr(nv, "value"):
                            name_map[cid] = nv.value
                            break
                except Exception as exc:
                    logger.debug(
                        "Failed to resolve company name for company id %s: %s",
                        cid,
                        exc,
                    )
            for r in results:
                if r.company and r.company in name_map:
                    r.company = name_map[r.company]

        return results


def _detect_optional_field_from_exception(error: Exception) -> str | None:
    text = extract_exception_body_text(error)
    for field in OPTIONAL_FIELD_WARNING_CODES:
        if field in text:
            return field
    return None


def _attempt_person_write_with_optional_fallback(
    *,
    write_func: Callable[[dict[str, Any]], Any],
    core_values: dict[str, Any],
    optional_values: dict[str, Any],
    strict: bool,
) -> tuple[Any, list[WarningEntry], list[SkippedField]]:
    active_optional = dict(optional_values)
    alias_pairs_attempted: set[tuple[str, str]] = set()
    warnings: list[WarningEntry] = []
    skipped_fields: list[SkippedField] = []

    while True:
        values = {**core_values, **active_optional}
        try:
            response = write_func(values)
            return response, warnings, skipped_fields
        except Exception as exc:
            optional_field = _detect_optional_field_from_exception(exc)
            if not optional_field:
                raise

            alias_field = OPTIONAL_FIELD_ALIASES.get(optional_field)
            if optional_field not in active_optional and alias_field in active_optional:
                optional_field = alias_field
                alias_field = OPTIONAL_FIELD_ALIASES.get(optional_field)

            if strict:
                raise SchemaMismatchError(
                    f"Optional field unavailable: {optional_field}",
                    field=optional_field,
                ) from exc

            if (
                alias_field
                and optional_field in active_optional
                and alias_field not in active_optional
            ):
                alias_pair = (
                    (optional_field, alias_field)
                    if optional_field <= alias_field
                    else (alias_field, optional_field)
                )
                if alias_pair not in alias_pairs_attempted:
                    alias_pairs_attempted.add(alias_pair)
                    active_optional[alias_field] = active_optional.pop(optional_field)
                    continue

            if optional_field not in active_optional:
                raise

            del active_optional[optional_field]
            code = OPTIONAL_FIELD_WARNING_CODES[optional_field]
            warnings.append(
                WarningEntry(
                    code=code,
                    message=f"Skipped optional field '{optional_field}' due to schema mismatch.",
                    field=optional_field,
                    retryable=False,
                ),
            )
            skipped_fields.append(
                SkippedField(
                    field=optional_field,
                    reason="schema_mismatch",
                ),
            )


# Public alias for test and integration reliability checks.
attempt_person_write_with_optional_fallback = (
    _attempt_person_write_with_optional_fallback
)


def search_people(
    name: str | None = None,
    email: str | None = None,
    email_domain: str | None = None,
    phone: str | None = None,
    company: str | None = None,
    sample: bool = False,
    limit: int = 25,
) -> ReliabilityEnvelope:
    results = _search_people_raw(
        name=name,
        email=email,
        email_domain=email_domain,
        phone=phone,
        company=company,
        sample=sample,
        limit=limit,
    )
    return _result_envelope(
        success=True,
        partial_success=False,
        action="searched",
        record_id=None,
        meta={"results": [r.model_dump() for r in results], "count": len(results)},
    )


def add_person(input: PersonInput) -> ReliabilityEnvelope:
    core_values = build_core_person_values(input)
    optional_values = build_optional_person_values(
        company_domain=input.company_domain,
        notes=input.notes,
        location=input.location,
        country_code=input.country_code,
        location_mode=input.location_mode,
    )

    with get_client() as client:

        def _create_person(values: dict[str, Any]) -> Any:
            return client.records.post_v2_objects_object_records(
                object="people",
                data=build_post_record_request(values),
            )

        try:
            response, warnings, skipped_fields = (
                _attempt_person_write_with_optional_fallback(
                    write_func=_create_person,
                    core_values=core_values,
                    optional_values=optional_values,
                    strict=input.strict,
                )
            )
        except Exception as e:
            if is_uniqueness_conflict(e):
                # `from None` suppresses the SDK's ResponseValidationError chain — see
                # libs/attio/companies.py::add_company for the full explanation.
                raise AttioConflictError(
                    "Person already exists. Use 'update' instead.",
                ) from None
            raise

        person = _extract_result(response.data)
        return _result_envelope(
            success=True,
            partial_success=bool(skipped_fields),
            action="created",
            record_id=person.record_id,
            warnings=warnings,
            skipped_fields=skipped_fields,
            meta={"person": person.model_dump()},
        )


def update_person(
    record_id: str | None,
    email: str | None,
    input: PersonInput,
) -> ReliabilityEnvelope:
    with get_client() as client:
        if not record_id:
            if not email:
                # Missing selector is a client-input error (→400), not a lookup
                # miss (→404). Keep AttioNotFoundError for the genuine
                # "no record matched" case below. See ai-h5y.
                raise AttioValidationError(
                    "Provide --id or --email to identify the person.",
                )
            query_response = client.records.post_v2_objects_object_records_query(
                object="people",
                filter_={"email_addresses": email},
                limit=2,
            )
            if not query_response.data:
                raise AttioNotFoundError(f"No person found with email: {email}")
            record_id = query_response.data[0].id.record_id

        get_resp = client.records.get_v2_objects_object_records_record_id_(
            object="people",
            record_id=record_id,
        )
        existing_emails = _extract_email_addresses_from_record_values(
            get_resp.data.values,
        )

        email_write: list[str] | None = None
        merge_warnings: list[WarningEntry] = []

        if input.replace_emails:
            seeds = [
                e
                for e in [input.email, *input.additional_emails]
                if e and str(e).strip()
            ]
            email_write = normalize_email_address_list(seeds)
            if not email_write:
                raise AttioValidationError(
                    "--replace-emails requires at least one email "
                    "(identity or --add-email).",
                )
            if {e.casefold() for e in email_write} != {
                e.casefold() for e in existing_emails
            }:
                merge_warnings.append(
                    WarningEntry(
                        code="multiple_emails_added",
                        message="Email addresses updated (--replace-emails).",
                        field="email_addresses",
                        retryable=False,
                    ),
                )
        elif input.additional_emails:
            email_write = normalize_email_address_list(
                [*existing_emails, input.email, *input.additional_emails],
            )
            new_keys = {e.casefold() for e in email_write} - {
                e.casefold() for e in existing_emails
            }
            if new_keys:
                merge_warnings.append(
                    WarningEntry(
                        code="multiple_emails_added",
                        message="New email address(es) merged onto existing person.",
                        field="email_addresses",
                        retryable=False,
                    ),
                )

        core_values = build_core_person_values(
            input,
            partial=True,
            email_addresses=email_write,
        )
        optional_values = build_optional_person_values(
            company_domain=input.company_domain,
            notes=input.notes,
            location=input.location,
            country_code=input.country_code,
            location_mode=input.location_mode,
        )

        def _update_person(values: dict[str, Any]) -> Any:
            return client.records.patch_v2_objects_object_records_record_id_(
                object="people",
                record_id=record_id,
                data=build_patch_record_request(values),
            )

        try:
            response, warnings, skipped_fields = (
                _attempt_person_write_with_optional_fallback(
                    write_func=_update_person,
                    core_values=core_values,
                    optional_values=optional_values,
                    strict=input.strict,
                )
            )
        except Exception as e:
            # PATCH can hit a uniqueness conflict when a unique attribute
            # (e.g. email_addresses) is updated to a value already held by
            # another record. Suppress the SDK pydantic chain — see add_person.
            if is_uniqueness_conflict(e):
                raise AttioConflictError(
                    "Person update conflicts with an existing record.",
                ) from None
            raise
        warnings = [*merge_warnings, *warnings]

        person = _extract_result(response.data)
        return _result_envelope(
            success=True,
            partial_success=bool(skipped_fields),
            action="updated",
            record_id=person.record_id,
            warnings=warnings,
            skipped_fields=skipped_fields,
            meta={"person": person.model_dump()},
        )


def upsert_person(
    input: PersonInput,
    *,
    matching_attribute: Literal["email", "linkedin", "github_handle"] = "email",
    strict: bool = False,
) -> ReliabilityEnvelope:
    # Read-then-create on the single matching identity. Attio cannot enforce
    # native uniqueness on `linkedin`/`github_handle` (text attributes on the
    # populated people object — see scripts/attio-bootstrap-people.py), so a
    # concurrent create can still produce a duplicate; the >1-match branch below
    # converges future writes deterministically and out-of-band cleanup handles
    # the rare straggler (ai-icn). The PersonInput field name matches
    # matching_attribute for all three identities.
    matches = _search_people_raw(
        email=input.email if matching_attribute == "email" else None,
        linkedin=input.linkedin if matching_attribute == "linkedin" else None,
        github_handle=(
            input.github_handle if matching_attribute == "github_handle" else None
        ),
        limit=50,
    )
    identity_value: str | None = getattr(input, matching_attribute, None)
    identity_label = matching_attribute

    if len(matches) == 0:
        return add_person(input)

    if len(matches) == 1:
        return update_person(record_id=matches[0].record_id, email=None, input=input)

    if strict:
        raise ConflictError(
            f"Multiple people matched {identity_label} {identity_value}; strict mode rejects ambiguity.",
        )

    selected = sorted(m.record_id for m in matches)[0]
    envelope = update_person(record_id=selected, email=None, input=input)
    envelope.warnings.append(
        WarningEntry(
            code="upsert_multi_match_selected_record",
            message=(
                "Multiple records matched; wrote to the lexicographically smallest "
                "record_id. The other duplicate(s) are left in place (not "
                "auto-removed) and may need manual merge in Attio."
            ),
            field="record_id",
            retryable=False,
        ),
    )
    envelope.partial_success = True
    return envelope


def get_person_values(
    *,
    matching_attribute: Literal["email", "linkedin", "github_handle"],
    email: str | None = None,
    linkedin: str | None = None,
    github_handle: str | None = None,
) -> dict[str, Any] | None:
    """Look up a person by the identifier matching ``matching_attribute``.

    The lookup uses exactly one identifier — the one named by
    ``matching_attribute`` — so the read targets the same record that a
    write keyed on the same ``matching_attribute`` will touch. Callers must
    not assume email-then-linkedin OR-ing; supply ``matching_attribute``
    explicitly to keep read and write aligned (see ``merge_only_if_empty``
    in ``src.attio.export._handle_upsert_person``).

    Raises:
        ValueError: if ``matching_attribute`` is unknown, or if the
            corresponding identifier is None/empty. Surfaces caller bugs
            loudly instead of silently degrading into an unprotected
            overwrite.

    Returns:
        The Attio record's ``values`` dict, or None if no record matches.
        Transient Attio query errors are swallowed and degrade to None
        (existing safety net — keeps the upsert from cascading-failing
        on a flaky read).
    """
    identifier_map: dict[str, str | None] = {
        "email": email,
        "linkedin": linkedin,
        "github_handle": github_handle,
    }
    if matching_attribute not in identifier_map:
        raise ValueError(
            f"get_person_values: unknown matching_attribute={matching_attribute!r}",
        )
    if not identifier_map[matching_attribute]:
        raise ValueError(
            f"get_person_values: matching_attribute={matching_attribute!r} "
            f"requires non-empty {matching_attribute!r}",
        )

    try:
        # Search on the SAME single identity the write path uses, so a non-unique
        # identity with duplicates resolves to the SAME canonical record (min
        # record_id) on read and write — otherwise merge_only_if_empty could
        # inspect one duplicate while the write lands on another. _search_people_raw
        # also handles linkedin URL variants. Then read that record's values by id.
        matches = _search_people_raw(
            email=email if matching_attribute == "email" else None,
            linkedin=linkedin if matching_attribute == "linkedin" else None,
            github_handle=(
                github_handle if matching_attribute == "github_handle" else None
            ),
            limit=50,
        )
        if not matches:
            return None
        canonical_id = min(m.record_id for m in matches)
        with get_client() as client:
            response = client.records.get_v2_objects_object_records_record_id_(
                object="people",
                record_id=canonical_id,
            )
            return dict(response.data.values or {})
    except Exception:
        # Any lookup failure (transient query/GET error) degrades to None
        # so the upsert can still proceed.
        return None


def error_envelope(error: Exception, *, strict: bool = False) -> ReliabilityEnvelope:
    classified = classify_error(
        translate_modal_signature_error(error),
        strict=strict,
    )
    return _result_envelope(
        success=False,
        partial_success=False,
        action="failed",
        record_id=None,
        errors=[
            ErrorEntry(
                code=classified.code,
                message=classified.message,
                error_type=classified.error_type,
                fatal=classified.fatal,
                field=classified.field,
            ),
        ],
    )


def _split_name(name: str) -> tuple[str, str | None]:
    parts = name.strip().split(" ", 1)
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def find_person_by_name_at_company(
    name: str,
    company_record_id: str,
) -> str | None:
    """Find a Person by name; on multi-match, prefer the one linked to ``company_record_id``.

    Splits ``name`` on the first space — single-word names match first_name
    only. Returns the lex-smallest matching record_id on tied multi-match
    (mirrors the policy in ``upsert_person``).
    """
    first, last = _split_name(name)
    filter_conditions: list[dict[str, Any]] = [{"name": {"first_name": first}}]
    if last:
        filter_conditions.append({"name": {"last_name": last}})
    if len(filter_conditions) == 1:
        filter_: dict[str, Any] = filter_conditions[0]
    else:
        filter_ = {"$and": filter_conditions}

    with get_client() as client:
        response = client.records.post_v2_objects_object_records_query(
            object="people",
            filter_=filter_,
            limit=50,
        )
        if not response.data:
            return None
        # Prefer a match already linked to the company.
        for rec in response.data:
            for cv in rec.values.get("company", []):
                if getattr(cv, "target_record_id", None) == company_record_id:
                    return rec.id.record_id
        # Fall back to the lex-smallest record_id.
        return sorted(rec.id.record_id for rec in response.data)[0]


def stub_create_person(
    name: str,
    company_record_id: str,
    *,
    apply: bool,
) -> str:
    """Create a Person record with only ``name`` (split on first space) and a company link.

    Preview mode returns ``preview-<name>``. Apply mode does not require an
    email — it goes around ``PersonInput`` (which mandates one of
    email/linkedin/github) by hitting the SDK directly with a minimal
    payload. The created Person can be enriched later by other workflows.
    """
    if not apply:
        return f"preview-{name}"

    first, last = _split_name(name)
    values: dict[str, Any] = {
        "name": [{"first_name": first, "last_name": last or "", "full_name": name}],
        "company": [
            {"target_object": "companies", "target_record_id": company_record_id},
        ],
    }
    with get_client() as client:
        try:
            response = client.records.post_v2_objects_object_records(
                object="people",
                data=build_post_record_request(values),
            )
            return response.data.id.record_id
        except Exception as exc:
            if is_uniqueness_conflict(exc):
                from libs.attio.sdk_boundary import extract_existing_record_id

                existing = extract_existing_record_id(exc)
                if existing:
                    return existing
            raise
