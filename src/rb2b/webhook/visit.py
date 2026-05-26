"""Webhook ETL contract for rb2b visit ingestion."""

from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from libs.attio.values import format_location_from_parts, normalize_linkedin_url
from libs.dlt.bucket_naming import etl_bucket_name, raw_bucket_name
from libs.rb2b import Webhook as Rb2bWebhook
from libs.webhook.filter import WebhookFilter, WebhookFilters
from src.rb2b.utils import (
    event_to_jsonl,
    generate_gcs_filename,
    split_rb2b_tags,
)


def extract_domain(website: str | None) -> str | None:
    """Best-effort domain extraction from an rb2b Website field.

    rb2b emits values like ``https://example.com``, ``example.com/path``, or
    ``www.example.com``. Strip scheme, leading ``www.``, and any path/query so
    the result matches Attio's expected ``domains[].domain`` shape.
    """
    if not website:
        return None
    candidate = website.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"//{candidate}"
    host = urlparse(candidate).hostname
    if not host:
        return None
    return host.removeprefix("www.")


class NoResolvablePersonFilter(WebhookFilter):
    """Match rb2b visits with no identifiable Person.

    Historical rationale (ai-5x9): the live ``tracking_events`` schema
    only had a Person ref slug, so a no-business-email visit landed as a
    contact-less row invisible on any timeline. Prod has since grown a
    ``company`` record-reference (verified 2026-05-26), so company-only
    visits now land on the Company timeline instead of being invisible —
    this filter is no longer in ``DEFAULT_FILTERS``. Keep the class
    available for callers that opt in (e.g. a noisy account that only
    matters when a Person resolves).

    Auto-registers in ``WebhookFilter._registry`` under the ``type`` tag.
    """

    type: Literal["no_resolvable_person"] = "no_resolvable_person"

    def should_exclude(self, webhook: Any) -> bool:
        return not bool(getattr(webhook.payload, "business_email", None))


# Empty by default: every valid rb2b visit (Person or Company resolvable)
# lands a tracking_events row. Subclasses or call sites that want the
# legacy person-only behavior can install ``NoResolvablePersonFilter``
# explicitly.
DEFAULT_FILTERS: WebhookFilters = WebhookFilters(root=[])


class Webhook(Rb2bWebhook):
    """Webhook subclass implementing ETL contract for rb2b visits."""

    FILTERS: ClassVar[WebhookFilters] = DEFAULT_FILTERS

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-gcp-202605111323"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return etl_bucket_name(source="rb2b", entity_plural="visits")

    @staticmethod
    def raw_get_bucket_name() -> str:
        return raw_bucket_name(source="rb2b", entity_plural="visits")

    @staticmethod
    def raw_get_app_name() -> str:
        from libs.dlt.filesystem_gcp import CloudGoogle

        return CloudGoogle.clean_bucket_name(bucket_name=Webhook.raw_get_bucket_name())

    # Raw passthrough has no per-source invariants — see caldotcom/booking.py.
    def raw_is_valid_webhook(self) -> bool:
        return True

    def raw_get_invalid_webhook_error_msg(self) -> str:
        return "raw passthrough accepts any payload; should not be reachable"

    @staticmethod
    def storage_get_app_name() -> str:
        return Webhook.etl_get_bucket_name()

    @staticmethod
    def storage_get_base_model_type() -> type[BaseModel] | None:
        return None

    @staticmethod
    def lance_get_project_name() -> str:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    @staticmethod
    def lance_get_base_model_type() -> str:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    def etl_is_valid_webhook(self) -> bool:
        return True

    def etl_get_invalid_webhook_error_msg(self) -> str:
        return "This webhook family does not support ETL output"

    def etl_get_json(self, storage: Any = None) -> str:
        return event_to_jsonl(
            self.model_dump(mode="json", by_alias=False),
            self.event_id,
        )

    def etl_get_file_name(self) -> str:
        return generate_gcs_filename(
            self.timestamp,
            self.event_id,
            self.payload.company_name,
        )

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    # --- Attio export contract ---

    @staticmethod
    def attio_get_secret_collection_names() -> list[str]:
        return ["attio"]

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-rb2b-visits"

    def _attio_domain(self) -> str | None:
        return extract_domain(self.payload.website)

    def _excluded_by_filter(self) -> WebhookFilter | None:
        return self.FILTERS.should_exclude(self)

    def attio_is_valid_webhook(self) -> bool:
        # An rb2b visit is exportable to Attio if we have something to land —
        # either an identified Person (business_email) or an identified
        # Company (resolvable domain). Anonymous visits with neither are
        # skipped at this top-level gate. The Person-only filter
        # (``NoResolvablePersonFilter``) is a finer-grained gate applied
        # inside ``attio_get_operations`` to suppress just the
        # ``UpsertTrackingEvent`` op when no Person resolves.
        has_person = bool(self.payload.business_email)
        has_company = bool(self._attio_domain())
        return has_person or has_company

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return "rb2b visit has neither business_email nor a resolvable company domain"

    def attio_get_operations(self) -> list[Any]:
        if not self.attio_is_valid_webhook():
            return []

        import json
        from datetime import datetime, timezone

        from src.attio.ops import (
            CompanyRef,
            PersonRef,
            UpsertCompany,
            UpsertPerson,
            UpsertTrackingEvent,
        )

        ops: list[Any] = []
        domain = self._attio_domain()
        email = self.payload.business_email
        linkedin = normalize_linkedin_url(self.payload.linkedin_url)

        if domain:
            ops.append(
                UpsertCompany(
                    domain=domain,
                    name=self.payload.company_name,
                    industry=self.payload.industry,
                    employee_count=self.payload.employee_count,
                    estimate_revenue=self.payload.estimate_revenue,
                    merge_only_if_empty=[
                        "industry",
                        "employee_count",
                        "estimate_revenue",
                    ],
                ),
            )

        if email:
            ops.append(
                UpsertPerson(
                    matching_attribute="email",
                    email=email,
                    first_name=self.payload.first_name,
                    last_name=self.payload.last_name,
                    linkedin=linkedin,
                    company_domain=domain,
                    title=self.payload.title,
                    city=self.payload.city,
                    state=self.payload.state,
                    zipcode=self.payload.zipcode,
                    merge_only_if_empty=["title", "city", "state", "zipcode"],
                ),
            )

        # tracking_events on prod has both a ``people`` and a ``company``
        # ref attribute, so a company-only visit (no resolvable Person)
        # still lands a useful row. The Person-only filter
        # (``NoResolvablePersonFilter``) remains opt-in below — kept as a
        # short-circuit for noisy anonymous traffic that doesn't move any
        # account forward, but no longer required by the schema. The
        # audit trail for the suppressed row still lives in GCS raw + ETL.
        if self._excluded_by_filter() is not None:
            return ops

        subject_person = PersonRef(attribute="email", value=email) if email else None
        subject_company = CompanyRef(domain=domain) if domain else None
        event_subtype = (
            "repeat_visit" if self.payload.is_repeat_visit else "first_visit"
        )
        seen_at = self.payload.seen_at or self.timestamp or datetime.now(timezone.utc)
        location = format_location_from_parts(
            city=self.payload.city,
            state=self.payload.state,
            zipcode=self.payload.zipcode,
        )

        ops.append(
            UpsertTrackingEvent(
                external_id=f"rb2b:{self.event_id}",
                source="rb2b",
                name="RB2B Website visit",
                event_type="rb2b_visit",
                event_subtype=event_subtype,
                event_timestamp=seen_at,
                body_json=json.dumps(self.model_dump(mode="json")),
                captured_url=self.payload.captured_url,
                referrer=self.payload.referrer,
                is_repeat_visit=self.payload.is_repeat_visit,
                tags=split_rb2b_tags(self.payload.tags),
                location=location,
                subject_person=subject_person,
                subject_company=subject_company,
            ),
        )

        return ops
