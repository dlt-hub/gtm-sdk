"""Webhook ETL contract for Fathom recording ingestion.

The same transform this class implements (``attio_get_operations``) is reused
for *backfill* by ``scripts/fathom-backfill-attio-meetings.py``: that script
lists recordings from the Fathom REST API (``libs/fathom/client.py``), reshapes
each into the ``Webhook`` payload model, and runs them through this exact path.
So a recording that predates the webhook — or a meeting missing from Attio after
the pre-plan-02 Cal.com reschedule bug (ai-t58) — can be replayed without
forking the Fathom → Attio mapping logic.
"""

from typing import Any

from pydantic import BaseModel

from libs.dlt.bucket_naming import etl_bucket_name, raw_bucket_name
from libs.fathom import Webhook as FathomWebhook
from libs.meetings import canonical_meeting_uid
from src.fathom.utils import (
    build_meeting_description,
    fathom_summary_title,
    render_action_items_markdown,
    select_note_parent_email,
    generate_gcs_filename,
    recording_to_jsonl,
)


class Webhook(FathomWebhook):
    """Webhook subclass implementing ETL contract for Fathom recordings."""

    @staticmethod
    def modal_get_secret_collection_names() -> list[str]:
        return ["devx-gcp-202605260000"]

    @staticmethod
    def etl_get_bucket_name() -> str:
        return etl_bucket_name(source="fathom", entity_plural="recordings")

    @staticmethod
    def raw_get_bucket_name() -> str:
        return raw_bucket_name(source="fathom", entity_plural="recordings")

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
        return recording_to_jsonl(self.model_dump(mode="json"), self.recording_id)

    def etl_get_file_name(self) -> str:
        return generate_gcs_filename(
            self.recording_start_time,
            self.recording_id,
            self.meeting_title or self.title,
        )

    def etl_get_base_models(self, storage: Any) -> list[Any]:
        raise NotImplementedError("LanceDB integration is Phase 2+")

    # --- Attio export contract ---

    @staticmethod
    def required_api_keys() -> list[str]:
        return ["ATTIO_API_KEY"]

    @staticmethod
    def optional_api_keys() -> list[str]:
        return []

    @staticmethod
    def attio_get_app_name() -> str:
        return "export-to-attio-from-fathom-recordings"

    # --- Slack export contract (not implemented for this source) ---
    # Present so the WebhookModelProtocol conformance test passes; this source
    # is never wired into webhooks/export_to_slack.py. Returns an empty plan
    # rather than raising so an accidental deploy no-ops instead of erroring.
    @staticmethod
    def slack_get_app_name() -> str:
        return "export-to-slack-from-fathom-recordings"

    def slack_is_valid_webhook(self) -> bool:
        return False

    def slack_get_invalid_webhook_error_msg(self) -> str:
        return "Slack export is not supported for Fathom recordings"

    def slack_get_messages(self) -> list[Any]:
        return []

    def attio_is_valid_webhook(self) -> bool:
        # Ad-hoc Fathom recordings have no calendar invitees but still carry a
        # recorder we can attribute the meeting to. Only reject payloads we
        # truly can't anchor (missing recording_id or recorder email).
        return bool(self.recording_id) and bool(self.recorded_by.email)

    def attio_get_invalid_webhook_error_msg(self) -> str:
        return (
            "Fathom call payload is not exportable to Attio "
            "(missing recording_id or recorder email)"
        )

    def attio_get_operations(self) -> list[Any]:
        from src.attio.ops import (
            CompanyRef,
            UpsertNote,
            MeetingExternalRef,
            MeetingParticipant,
            MeetingRef,
            PersonRef,
            UpsertMeeting,
        )

        description: str = build_meeting_description(
            summary_markdown=(
                self.default_summary.markdown_formatted
                if self.default_summary
                else None
            ),
            fallback_title=self.meeting_title or self.title,
            # Prefer the shareable link over the internal /calls page so Attio
            # viewers without Fathom access can still open the recording.
            recording_url=self.share_url or self.url,
            recording_id=self.recording_id,
            transcript_language=self.transcript_language,
        )
        # Fall back to the recorder as the sole participant for ad-hoc Fathom
        # recordings that aren't tied to a calendar invite.
        # Fathom's calendar_invitees payload (libs/fathom/models.py:CalendarInvitee)
        # has no RSVP field, so MeetingParticipant.status falls back to its
        # "accepted" default for every Fathom-sourced participant — these statuses
        # are not trustworthy.
        participants = [
            MeetingParticipant(
                email_address=inv.email,
                is_organizer=(inv.email == self.recorded_by.email),
            )
            for inv in self.calendar_invitees
        ] or [
            MeetingParticipant(
                email_address=self.recorded_by.email,
                is_organizer=True,
            ),
        ]
        ical_uid = canonical_meeting_uid(
            host_email=self.recorded_by.email,
            start=self.scheduled_start_time,
        )
        # Attio notes cannot be parented to a meeting (ai-gez): a note hangs off
        # a standard-object record and is *associated* to the meeting via
        # ``meeting_id``. Parent the summary / action-item notes to the call's
        # primary external participant (or the recorder, as a fallback), and
        # attach the meeting via ``UpsertNote.meeting``.
        note_parent = PersonRef(
            attribute="email",
            value=select_note_parent_email(
                calendar_invitees=self.calendar_invitees,
                # Constrain the parent to emails /v2/meetings will auto-create.
                participant_emails=[p.email_address for p in participants],
                recorder_email=self.recorded_by.email,
            ),
        )
        note_meeting = MeetingRef(ical_uid=ical_uid)
        # Link the meeting to existing Attio records so it surfaces on the
        # related people/company timelines (ai-ch3). These are *Refs*, not
        # record_ids: the dispatcher resolves them by email/domain at write time
        # and silently drops any that don't already exist (link-only — the
        # /v2/meetings POST itself auto-creates participant Persons, so we never
        # create records here). Person links cover every participant; company
        # links cover external invitee domains only (our own org domain is not a
        # CRM company we attach meetings to).
        person_links = [
            PersonRef(attribute="email", value=p.email_address) for p in participants
        ]
        company_domains = {
            inv.email_domain
            for inv in self.calendar_invitees
            if inv.is_external and inv.email_domain
        }
        company_links = [
            CompanyRef(domain=domain) for domain in sorted(company_domains)
        ]
        ops: list[Any] = [
            UpsertMeeting(
                external_ref=MeetingExternalRef(
                    ical_uid=ical_uid,
                    provider="google",
                    is_recurring=False,
                ),
                title=self.meeting_title or self.title,
                description=description,
                start=self.scheduled_start_time,
                end=self.scheduled_end_time,
                is_all_day=False,
                participants=participants,
                linked_records=[*person_links, *company_links],
            ),
        ]

        if self.default_summary and self.default_summary.markdown_formatted.strip():
            ops.append(
                UpsertNote(
                    parent=note_parent,
                    meeting=note_meeting,
                    title=fathom_summary_title(self.default_summary.template_name),
                    content=self.default_summary.markdown_formatted,
                ),
            )

        if self.action_items:
            rendered = render_action_items_markdown(self.action_items)
            if rendered.strip():
                ops.append(
                    UpsertNote(
                        parent=note_parent,
                        meeting=note_meeting,
                        title="Action items",
                        content=rendered,
                    ),
                )

        return ops
