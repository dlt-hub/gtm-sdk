import sys
import time
from typing import Any

from libs.attio.client import get_client
from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import AttioNotFoundError, AttioValidationError
from libs.attio.models import NoteInput, NoteResult
from libs.attio.sdk_boundary import build_post_note_request, model_dump_or_empty


def _resolve_parent_record_id(
    client: Any,
    parent_object: str,
    parent_record_id: str | None,
    parent_email: str | None,
    parent_domain: str | None,
) -> str:
    if parent_record_id:
        return parent_record_id

    if parent_object == "people":
        if not parent_email:
            raise AttioValidationError(
                "Provide --record-id or --email to identify the parent person.",
            )
        response = client.records.post_v2_objects_object_records_query(
            object="people",
            filter_={"email_addresses": parent_email},
            limit=1,
        )
        if not response.data:
            raise AttioNotFoundError(
                f"No person found with email: {parent_email}",
            )
        return response.data[0].id.record_id

    if parent_object == "companies":
        if not parent_domain:
            raise AttioValidationError(
                "Provide --record-id or --domain to identify the parent company.",
            )
        response = client.records.post_v2_objects_object_records_query(
            object="companies",
            filter_={"domains": parent_domain},
            limit=1,
        )
        if not response.data:
            raise AttioNotFoundError(
                f"No company found with domain: {parent_domain}",
            )
        return response.data[0].id.record_id

    raise AttioValidationError(
        f"Invalid parent object: {parent_object}. Must be 'people' or 'companies'.",
    )


def resolve_record_id_for_ref(
    *,
    parent_object: str,
    email: str | None = None,
    domain: str | None = None,
    attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> str | None:
    """Resolve a people/companies record_id by email or domain.

    Returns ``None`` (rather than raising) when no record matches after all
    attempts, so callers can branch on a miss. Used by the dispatcher when a
    note's parent ref is not in the plan's LookupTable — e.g. the Fathom path,
    where the ``/v2/meetings`` upsert auto-creates the participant Person
    instead of emitting an explicit ``UpsertPerson`` op (ai-gez).

    The lookup is a read-after-write: the record was typically just created by a
    preceding ``/v2/meetings`` upsert, and Attio's record search can lag a beat
    behind that write. Retry a bounded number of times with linear backoff
    before treating a miss as final, so a brief propagation delay does not abort
    the whole export. A permanent error (invalid parent_object, missing
    email/domain) raises ``AttioValidationError`` and is not retried.
    """
    for attempt in range(attempts):
        with get_client() as client:
            try:
                return _resolve_parent_record_id(
                    client,
                    parent_object,
                    None,
                    email,
                    domain,
                )
            except AttioNotFoundError:
                pass
        if attempt + 1 < attempts:
            time.sleep(backoff_seconds * (attempt + 1))
    return None


def _extract_result(note: Any) -> NoteResult:
    raw: dict[str, Any] = model_dump_or_empty(note)
    return NoteResult(
        note_id=note.id.note_id,
        title=note.title,
        parent_object=note.parent_object,
        parent_record_id=note.parent_record_id,
        content_plaintext=note.content_plaintext,
        created_at=note.created_at,
        meeting_id=getattr(note, "meeting_id", None),
        raw=raw,
    )


def list_notes_for_parent(
    parent_object: str,
    parent_record_id: str,
) -> list[NoteResult]:
    with get_client() as client:
        response = client.notes.get_v2_notes(
            parent_object=parent_object,
            parent_record_id=parent_record_id,
        )
        return [_extract_result(n) for n in response.data]


def add_note(input: NoteInput) -> NoteResult:
    with get_client() as client:
        record_id = _resolve_parent_record_id(
            client,
            input.parent_object,
            input.parent_record_id,
            input.parent_email,
            input.parent_domain,
        )

        response = client.notes.post_v2_notes(
            data=build_post_note_request(
                parent_object=input.parent_object,
                parent_record_id=record_id,
                title=input.title,
                format_=input.format,
                content=input.content,
                created_at=input.created_at,
                meeting_id=input.meeting_id,
            ),
        )

        return _extract_result(response.data)


def find_note_by_title(
    *,
    parent_object: str,
    parent_record_id: str,
    title: str,
    meeting_id: str | None = None,
) -> str | None:
    """Return the note_id of an existing Note with this exact title on this parent.

    Linear scan of the parent's notes list; Attio does not expose a
    server-side title filter for Notes. Used by ``create_note`` for
    idempotency.

    When ``meeting_id`` is given, the match is scoped to notes associated with
    that meeting: a person/company parent accumulates notes across many
    meetings, so title alone is not a unique key for a meeting-associated note
    (ai-gez). When ``meeting_id`` is None, matches on title only (back-compat
    for non-meeting notes).
    """
    with get_client() as client:
        response = client.notes.get_v2_notes(
            parent_object=parent_object,
            parent_record_id=parent_record_id,
        )
        for note in response.data:
            if getattr(note, "title", None) != title:
                continue
            if (
                meeting_id is not None
                and getattr(note, "meeting_id", None) != meeting_id
            ):
                continue
            return note.id.note_id
    return None


def create_note(
    *,
    input: NoteInput,
    apply: bool,
) -> ReliabilityEnvelope:
    """Idempotently create a Note attached to a parent record.

    Differs from the existing ``add_note`` in two ways: (1) it requires
    ``input.parent_record_id`` to be set (no email/domain resolution), and
    (2) it short-circuits via ``find_note_by_title`` so re-runs against the
    same parent + title return ``action="noop"`` with the existing
    ``record_id``.

    ``input.format`` is passed through verbatim (defaults to ``"plaintext"``
    on ``NoteInput``; callers from the loader explicitly pass
    ``format="markdown"``).

    Preview mode (``apply=False``) never reads or writes.
    """
    if not apply:
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="noop",
            record_id=None,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1", "preview": True},
        )

    if not input.parent_record_id:
        raise AttioValidationError(
            "create_note requires input.parent_record_id; "
            "use add_note for email/domain resolution.",
        )

    existing = find_note_by_title(
        parent_object=input.parent_object,
        parent_record_id=input.parent_record_id,
        title=input.title,
        meeting_id=input.meeting_id,
    )
    if existing is not None:
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="noop",
            record_id=existing,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1", "idempotent": True},
        )

    with get_client() as client:
        response = client.notes.post_v2_notes(
            data=build_post_note_request(
                parent_object=input.parent_object,
                parent_record_id=input.parent_record_id,
                title=input.title,
                format_=input.format,
                content=input.content,
                created_at=input.created_at,
                meeting_id=input.meeting_id,
            ),
        )
        return ReliabilityEnvelope(
            success=True,
            partial_success=False,
            action="created",
            record_id=response.data.id.note_id,
            warnings=[],
            skipped_fields=[],
            errors=[],
            meta={"output_schema_version": "v1"},
        )


def update_note(note_id: str, input: NoteInput) -> NoteResult:
    with get_client() as client:
        existing = client.notes.get_v2_notes_note_id_(note_id=note_id)
        note = existing.data

        title = input.title if input.title else note.title
        content = input.content if input.content else note.content_plaintext
        format_ = input.format
        # Attio has no in-place note update, so we delete + recreate. Preserve
        # the existing meeting association unless the caller overrides it,
        # otherwise the recreate would silently drop it (ai-gez).
        meeting_id = (
            input.meeting_id
            if input.meeting_id is not None
            else getattr(note, "meeting_id", None)
        )

        client.notes.delete_v2_notes_note_id_(note_id=note_id)

        response = client.notes.post_v2_notes(
            data=build_post_note_request(
                parent_object=note.parent_object,
                parent_record_id=note.parent_record_id,
                title=title,
                format_=format_,
                content=content,
                meeting_id=meeting_id,
            ),
        )

        new_note_id = response.data.id.note_id
        print(
            f"Warning: note_id changed from {note_id} to {new_note_id} "
            f"(Attio does not support note updates in place).",
            file=sys.stderr,
        )

        return _extract_result(response.data)
