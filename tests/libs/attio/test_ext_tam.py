# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from libs.attio.models import ExtTamInput


def _make_input(**overrides: object) -> ExtTamInput:
    defaults: dict[str, object] = dict(
        name="Acme (Jane Doe @ Snowflake)",
        person_self_id="11111111-1111-1111-1111-111111111111",
        employer_id="22222222-2222-2222-2222-222222222222",
        account_ids=["33333333-3333-3333-3333-333333333333"],
        source="snowflake_scored_accounts_csv",
        source_snapshot_date=date(2026, 5, 25),
    )
    defaults.update(overrides)
    return ExtTamInput(**defaults)  # type: ignore[arg-type]


# `datetime`/`UTC` imports are no longer needed in this test file because
# `ExtTamInput.created_at` was removed when we discovered the Attio API does
# not honor it on records.


def test_build_values_includes_required_record_refs() -> None:
    from libs.attio.ext_tam import _build_ext_tam_values

    values = _build_ext_tam_values(_make_input(coverage_type="Capacity"))
    assert values["person_self"] == [
        {
            "target_object": "people",
            "target_record_id": "11111111-1111-1111-1111-111111111111",
        },
    ]
    assert values["employer"] == [
        {
            "target_object": "companies",
            "target_record_id": "22222222-2222-2222-2222-222222222222",
        },
    ]
    assert values["accounts"] == [
        {
            "target_object": "companies",
            "target_record_id": "33333333-3333-3333-3333-333333333333",
        },
    ]
    assert values["coverage_type"] == ["Capacity"]
    assert values["source"] == ["snowflake_scored_accounts_csv"]
    assert values["source_snapshot_date"] == [{"value": "2026-05-25"}]


def test_build_values_includes_connection_created_date_when_set() -> None:
    from libs.attio.ext_tam import _build_ext_tam_values

    values = _build_ext_tam_values(
        _make_input(connection_created_date=date(2025, 1, 5)),
    )
    assert values["connection_created_date"] == [{"value": "2025-01-05"}]


def test_build_values_omits_unset_optional_fields() -> None:
    from libs.attio.ext_tam import _build_ext_tam_values

    values = _build_ext_tam_values(_make_input())
    assert "customer_region" not in values
    assert "partner_score" not in values
    assert "last_connection_date" not in values
    assert "connection_created_date" not in values


def test_find_by_person_and_account_returns_record_id() -> None:
    fake_client = MagicMock()
    matched = MagicMock()
    matched.id.record_id = "rec-1"
    fake_client.records.post_v2_objects_object_records_query.return_value.data = [
        matched,
    ]

    with patch("libs.attio.ext_tam.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.ext_tam import find_by_person_and_account

        out = find_by_person_and_account(
            person_id="p1",
            account_id="c1",
        )
    assert out == "rec-1"


def test_find_by_person_and_account_returns_none_on_miss() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = []

    with patch("libs.attio.ext_tam.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.ext_tam import find_by_person_and_account

        out = find_by_person_and_account(person_id="p1", account_id="c1")
    assert out is None


def test_upsert_preview_no_writes() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = []

    with patch("libs.attio.ext_tam.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.ext_tam import upsert_ext_tam

        envelope = upsert_ext_tam(input=_make_input(), apply=False)
    assert envelope.action == "noop"
    fake_client.records.post_v2_objects_object_records.assert_not_called()
    fake_client.records.patch_v2_objects_object_records_record_id_.assert_not_called()


def test_upsert_apply_creates_when_missing() -> None:
    fake_client = MagicMock()
    fake_client.records.post_v2_objects_object_records_query.return_value.data = []
    created = MagicMock()
    created.id.record_id = "new-rec"
    fake_client.records.post_v2_objects_object_records.return_value.data = created

    with patch("libs.attio.ext_tam.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.ext_tam import upsert_ext_tam

        envelope = upsert_ext_tam(
            input=_make_input(connection_created_date=date(2025, 1, 5)),
            apply=True,
        )

    assert envelope.action == "created"
    assert envelope.record_id == "new-rec"


def test_upsert_apply_patches_when_record_exists() -> None:
    fake_client = MagicMock()
    found = MagicMock()
    found.id.record_id = "existing-rec"
    fake_client.records.post_v2_objects_object_records_query.return_value.data = [found]
    updated = MagicMock()
    updated.id.record_id = "existing-rec"
    fake_client.records.patch_v2_objects_object_records_record_id_.return_value.data = (
        updated
    )

    with patch("libs.attio.ext_tam.get_client") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = fake_client
        from libs.attio.ext_tam import upsert_ext_tam

        envelope = upsert_ext_tam(
            input=_make_input(connection_created_date=date(2025, 1, 5)),
            apply=True,
        )
    assert envelope.action == "updated"
    assert envelope.record_id == "existing-rec"
    fake_client.records.post_v2_objects_object_records.assert_not_called()
