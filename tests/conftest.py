from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attio.errors import SDKError  # noqa: E402  # pyrefly: ignore[missing-import]

from libs.attio.sdk_boundary import get_attio_sdk_client_class  # noqa: E402


@pytest.fixture(scope="session")
def attio_api_key() -> str:
    key = os.environ.get("ATTIO_API_KEY", "").strip()
    if not key:
        pytest.skip(
            "Attio integration tests gated by credentials preflight failure in tests/test_validation_type_error.py",
        )
    return key


@pytest.fixture(scope="session")
def modal_credentials_available() -> bool:
    token_id = os.environ.get("MODAL_TOKEN_ID", "").strip()
    token_secret = os.environ.get("MODAL_TOKEN_SECRET", "").strip()
    return bool(token_id and token_secret)


@pytest.fixture(scope="session")  # pyright: ignore[reportUntypedFunctionDecorator]
def attio_auth_probe(attio_api_key: str) -> None:
    # Cheap auth probe so a stale/invalid ATTIO_API_KEY skips integration tests
    # rather than 401-ing through every one. Runs once per session. Only auth
    # failures (401/403) get converted to skip — every other error propagates
    # so genuine SDK / schema / network regressions still surface.
    sdk_client_class = get_attio_sdk_client_class()
    probe_client = sdk_client_class(oauth2=attio_api_key)
    try:
        probe_client.records.post_v2_objects_object_records_query(
            object="people",
            filter_={},
            limit=1,
        )
    except SDKError as exc:
        if exc.status_code in (401, 403):
            pytest.skip(
                f"Attio credentials present but auth probe returned {exc.status_code}: {exc}",
            )
        raise


@pytest.fixture
def client(
    attio_api_key: str,
    attio_auth_probe: None,
) -> Any:
    sdk_client_class = get_attio_sdk_client_class()
    return sdk_client_class(oauth2=attio_api_key)


@pytest.fixture
def created_people_record_ids() -> list[str]:
    return []


@pytest.fixture
def cleanup_people_records(
    client: Any,
    created_people_record_ids: list[str],
) -> Iterator[None]:
    yield
    for record_id in created_people_record_ids:
        try:
            client.records.delete_v2_objects_object_records_record_id_(
                object="people",
                record_id=record_id,
            )

        except Exception as exc:
            print(
                f"Warning: failed to delete Attio test person record {record_id}: {exc}",
                file=sys.stderr,
            )
