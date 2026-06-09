"""classify_error buckets scope/permission failures as `insufficient_scope` (ai-ica).

The opaque "...does not exist or you do not have permission to access it." message
(returned when a missing-scope POST seeds a select option) must classify as
`insufficient_scope` with remediation text, not the catch-all `unknown_error`.
"""

from __future__ import annotations

from libs.attio.errors import (
    AttioScopeError,
    SchemaMismatchError,
    classify_error,
)


class _FakeSDKError(Exception):
    """Mimics attio SDKError shape: carries .raw_response.status_code."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.raw_response = type("R", (), {"status_code": status_code})()


def test_schema_mismatch_classifies_as_schema_mismatch() -> None:
    # A missing filter attribute (e.g. `github` if archived/absent) is translated
    # to SchemaMismatchError at the lib boundary; classify_error must bucket it as
    # `schema_mismatch` (not the catch-all `unknown_error`) and preserve the
    # field for an actionable envelope (ai-0ex).
    err = SchemaMismatchError(
        "people object has no filter attribute 'github'",
        field="github",
    )
    classified = classify_error(err)
    assert classified.code == "schema_mismatch"
    assert classified.field == "github"


def test_permission_message_classifies_as_insufficient_scope() -> None:
    err = _FakeSDKError(
        "Either the List/Object does not exist or you do not have permission "
        "to access it.",
    )
    classified = classify_error(err)
    assert classified.code == "insufficient_scope"
    assert classified.fatal is True
    assert "/v2/self" in classified.message


def test_403_status_classifies_as_insufficient_scope() -> None:
    err = _FakeSDKError("forbidden", status_code=403)
    classified = classify_error(err)
    assert classified.code == "insufficient_scope"


def test_attio_scope_error_classifies_as_insufficient_scope() -> None:
    err = AttioScopeError("missing scope", missing=["object_configuration:read-write"])
    classified = classify_error(err)
    assert classified.code == "insufficient_scope"
    assert "object_configuration:read-write" in classified.message


def test_unrelated_error_stays_unknown() -> None:
    classified = classify_error(ValueError("something else entirely"))
    assert classified.code == "unknown_error"
