from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from libs.attio.contracts import ReliabilityEnvelope
from libs.attio.errors import SchemaMismatchError
from src.attio.export import execute
from src.attio.ops import PersonRef, UpsertMention, UpsertPerson


def _op() -> UpsertMention:
    return UpsertMention(
        mention_url="https://reddit.com/r/x/comments/abc",
        last_action="mention_created",
        source_platform="reddit",
        source_id="abc",
        mention_body="hello",
        mention_timestamp=datetime(2026, 5, 10, 11, 55, 53),
        author_handle="u",
        primary_keyword="kw",
    )


def _success(record_id: str) -> ReliabilityEnvelope:
    return ReliabilityEnvelope(
        success=True,
        partial_success=False,
        action="created",
        record_id=record_id,
        warnings=[],
        skipped_fields=[],
        errors=[],
        meta={"output_schema_version": "v1"},
    )


def test_execute_dispatches_upsert_mention() -> None:
    with patch(
        "src.attio.export.libs_upsert_mention",
        return_value=_success("rec-9"),
    ) as handler:
        result = execute([_op()])
    handler.assert_called_once()
    assert result.success is True
    assert result.outcomes[0].op_type == "UpsertMention"
    assert result.outcomes[0].record_id == "rec-9"


def test_github_plan_mention_lands_when_person_upsert_fails() -> None:
    """The literal ai-0ex acceptance criterion: a github octolens plan whose
    optional UpsertPerson fails (no github_handle attribute) still writes the
    social_mention, with the endpoint reporting overall success=True."""
    person_op = UpsertPerson(
        matching_attribute="github_handle",
        github_handle="elviskahoro",
        github_url="https://github.com/elviskahoro",
        optional=True,
    )
    mention_op = UpsertMention(
        mention_url="https://github.com/dlt-hub/dlt/issues/4002",
        last_action="mention_created",
        source_platform="github",
        source_id="4002",
        mention_body="dlt issue",
        mention_timestamp=datetime(2026, 5, 10, 11, 55, 53),
        author_handle="elviskahoro",
        primary_keyword="dlt",
        related_person=PersonRef(attribute="github_handle", value="elviskahoro"),
        related_person_optional=True,
    )

    def _person_blows_up(*_args, **_kwargs):
        raise SchemaMismatchError(
            "people object has no filter attribute 'github'",
            field="github",
        )

    with (
        patch("src.attio.export.libs_upsert_person", side_effect=_person_blows_up),
        patch(
            "src.attio.export.libs_upsert_mention",
            return_value=_success("mention-rec-1"),
        ) as mention_handler,
    ):
        result = execute([person_op, mention_op])

    # Overall success reflects the MENTION, not the optional person.
    assert result.success is True
    # The person op failed but did not abort, and is classified, not opaque.
    person_outcome = result.outcomes[0]
    assert person_outcome.success is False
    assert person_outcome.optional is True
    assert person_outcome.envelope.errors[0].code == "schema_mismatch"
    # The mention landed, unlinked.
    mention_handler.assert_called_once()
    assert mention_handler.call_args.args[0].related_person_record_id is None
    assert result.outcomes[1].success is True
    assert result.outcomes[1].record_id == "mention-rec-1"
