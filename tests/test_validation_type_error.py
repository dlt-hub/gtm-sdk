import os
import secrets
from typing import Any

import pytest

from attio.errors import ResponseValidationError
from libs.attio.sdk_boundary import build_post_record_request


def _new_test_email() -> str:
    return f"attio-validation-test-{secrets.token_hex(4)}@example.com"


def test_attio_integration_credentials_preflight() -> None:
    key = os.environ.get("ATTIO_API_KEY", "").strip()
    if not key:
        pytest.skip("ATTIO_API_KEY not set — skipping integration tests")


def test_add_person_with_valid_data_or_typed_api_error(
    client: Any,
    created_people_record_ids: list[str],
    cleanup_people_records: None,
) -> None:
    try:
        response = client.records.post_v2_objects_object_records(
            object="people",
            data=build_post_record_request(
                values={
                    "email_addresses": [
                        {
                            "email_address": _new_test_email(),
                            "is_primary": True,
                        },
                    ],
                    "name": [{"first_name": "SDK", "last_name": "Validation"}],
                },
            ),
        )
    except Exception as exc:
        assert not isinstance(exc, ResponseValidationError)
        return

    record_id = response.data.id.record_id
    created_people_record_ids.append(record_id)
    assert record_id


def test_add_person_with_validation_error_is_not_response_validation_error(
    client: Any,
) -> None:
    try:
        client.records.post_v2_objects_object_records(
            object="people",
            data=build_post_record_request(
                values={
                    "email_addresses": [
                        {
                            "email_address": "invalid-email-format",
                            "is_primary": True,
                        },
                    ],
                    "name": [{"first_name": "Bad", "last_name": "Email"}],
                },
            ),
        )

    except Exception as exc:
        assert not isinstance(exc, ResponseValidationError)
        assert "literal_error" not in str(exc)
        assert "validation" in str(exc).lower() or "invalid" in str(exc).lower()
        return

    raise AssertionError("Expected request to fail for invalid email")


def test_add_person_with_multiple_validation_errors_is_not_response_validation_error(
    client: Any,
) -> None:
    try:
        client.records.post_v2_objects_object_records(
            object="people",
            data=build_post_record_request(
                values={
                    "email_addresses": [
                        {
                            "email_address": "invalid",
                            "is_primary": True,
                        },
                    ],
                    "name": [{"first_name": "", "last_name": ""}],
                },
            ),
        )

    except Exception as exc:
        assert not isinstance(exc, ResponseValidationError)
        assert "literal_error" not in str(exc)
        return

    raise AssertionError("Expected request to fail for invalid payload")


def test_search_people_still_works(
    client: Any,
) -> None:
    response = client.records.post_v2_objects_object_records_query(
        object="people",
        filter_={},
        limit=1,
    )
    assert isinstance(response.data, list)


def test_not_found_error_still_works(
    client: Any,
) -> None:
    try:
        client.records.get_v2_objects_object_records_record_id_(
            object="people",
            record_id="rec_does_not_exist_123456789",
        )

    except Exception as exc:
        assert not isinstance(exc, ResponseValidationError)
        return

    raise AssertionError("Expected a not-found error")
