from __future__ import annotations

import json
from typing import cast

import modal

from libs.attio.errors import ConflictError
from src.attio import companies as modal_companies
from src.attio import notes as modal_notes
from src.attio import people as modal_people


def test_company_add_http_returns_structured_409_on_conflict(monkeypatch) -> None:
    def _raise_conflict(**_kwargs):
        raise ConflictError("already exists")

    monkeypatch.setattr(modal_companies.attio_add_company, "remote", _raise_conflict)

    fn = cast(modal.Function, modal_companies.http_attio_company_add)  # type: ignore
    response = fn.local(
        modal_companies.CompanyAddQuery(
            name="Acme",
            domain="acme.com",
            description=None,
        ),
    )

    assert hasattr(response, "status_code")
    assert hasattr(response, "body")
    assert response.status_code == 409
    body = json.loads(response.body.decode("utf-8"))
    assert body["errors"][0]["code"] == "conflict"


def test_note_update_http_infers_401_from_error_message(monkeypatch) -> None:
    def _raise_auth(**_kwargs):
        raise RuntimeError(
            "API error occurred: Status 401 Content-Type application/json",
        )

    monkeypatch.setattr(modal_notes.attio_update_note, "remote", _raise_auth)

    fn = cast(modal.Function, modal_notes.http_attio_note_update)  # type: ignore
    response = fn.local(
        modal_notes.NoteUpdateQuery(
            note_id="note_123",
            title="x",
            content="y",
        ),
    )

    assert hasattr(response, "status_code")
    assert hasattr(response, "body")
    assert response.status_code == 401
    body = json.loads(response.body.decode("utf-8"))
    assert body["errors"][0]["code"] == "unknown_error"


def test_note_add_forwards_meeting_id_to_note_input(monkeypatch) -> None:
    # ai-gez: the HTTP/Modal note-add surface must forward meeting_id so callers
    # can create meeting-associated notes, not silently-plain ones.
    captured: dict[str, object] = {}

    def _capture(note_input):  # noqa: ANN001, ANN202
        captured["meeting_id"] = note_input.meeting_id
        captured["parent_object"] = note_input.parent_object
        return modal_notes.NoteResult(
            note_id="note-1",
            title=note_input.title,
            parent_object=note_input.parent_object,
            parent_record_id=note_input.parent_record_id or "rec-1",
            content_plaintext=note_input.content,
            created_at="2026-05-29T00:00:00Z",
            meeting_id=note_input.meeting_id,
        )

    monkeypatch.setattr(modal_notes, "add_note", _capture)

    fn = cast(modal.Function, modal_notes.attio_add_note)  # type: ignore
    result = fn.local(
        payload=modal_notes.NoteAddQuery(
            title="Action items",
            content="body",
            parent_object="people",
            record_id="rec-1",
            format="markdown",
            meeting_id="meet-123",
        ).model_dump(),
        api_keys={"attio_api_key": "ak"},
    )

    assert captured["meeting_id"] == "meet-123"
    assert captured["parent_object"] == "people"
    assert cast(modal_notes.NoteResult, result).meeting_id == "meet-123"


def test_person_add_http_returns_non_2xx_when_envelope_failed(monkeypatch) -> None:
    def _failed_envelope(**_kwargs):
        return {
            "success": False,
            "partial_success": False,
            "action": "failed",
            "record_id": None,
            "warnings": [],
            "skipped_fields": [],
            "errors": [
                {
                    "code": "validation_error",
                    "message": "bad input",
                    "error_type": "ValidationError",
                    "fatal": True,
                    "field": None,
                    "details": {},
                },
            ],
            "meta": {"output_schema_version": "v1"},
        }

    monkeypatch.setattr(modal_people.attio_add_person, "remote", _failed_envelope)

    fn = cast(modal.Function, modal_people.http_attio_person_add)  # type: ignore
    response = fn.local(
        modal_people.PersonAddQuery(
            email="a@example.com",
        ),
    )

    assert hasattr(response, "status_code")
    assert hasattr(response, "body")
    assert response.status_code == 400
    body = json.loads(response.body.decode("utf-8"))
    assert body["success"] is False


def test_not_found_code_maps_to_404() -> None:
    # AttioNotFoundError -> classify_error -> code="not_found" (ai-h5y). The
    # message here deliberately lacks the "Status NNN" pattern, so the status is
    # resolved from ERROR_CODE_TO_STATUS rather than the message, proving the
    # code-to-status entry — not just message inference — yields a 404.
    from src.attio.http_responses import status_code_from_error_payload

    payload = {
        "errors": [
            {
                "code": "not_found",
                "message": "Attio POST /v2/meetings returned 404.",
            },
        ],
    }
    assert status_code_from_error_payload(payload) == 404
