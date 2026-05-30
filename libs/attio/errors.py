from __future__ import annotations

from dataclasses import dataclass

from src.modal_app import MODAL_APP


class AttioError(Exception):
    pass


class ConfigurationError(AttioError):
    pass


class ConnectivityError(AttioError):
    pass


class SchemaMismatchError(AttioError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class ValidationError(AttioError):
    pass


class ConflictError(AttioError):
    def __init__(self, message: str, *, existing_record_id: str | None = None) -> None:
        super().__init__(message)
        self.existing_record_id = existing_record_id


class AttioAuthError(AttioError):
    pass


class AttioScopeError(AttioAuthError):
    """The token authenticated but lacks an OAuth scope a path requires.

    Distinct from :class:`AttioAuthError` (no/invalid token): here the token is
    valid and active, but its ``/v2/self`` scope set is missing a grant the
    operation needs (e.g. ``object_configuration:read-write`` to seed a new
    select option). Raised by the ``libs.attio.preflight`` scope check so the
    failure is legible at the orchestration entrypoint instead of surfacing as
    an opaque "...does not exist or you do not have permission..." error deep
    inside a write. See ai-ica.
    """

    def __init__(self, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


class AttioNotFoundError(AttioError):
    pass


class DeploymentMismatchError(AttioError):
    pass


class AttioConflictError(ConflictError):
    pass


class AttioValidationError(ValidationError):
    pass


@dataclass
class ClassifiedError:
    code: str
    message: str
    error_type: str
    fatal: bool
    field: str | None = None


def translate_modal_signature_error(error: Exception) -> Exception:
    if isinstance(error, TypeError) and "unexpected keyword argument" in str(error):
        return DeploymentMismatchError(
            "Modal function signature mismatch. Deployed function is stale for current CLI payload. "
            f"Redeploy with: modal app stop {MODAL_APP} && modal deploy deploy.py --name {MODAL_APP}",
        )
    return error


# Substrings Attio returns when a token's scope (or per-object grant) is
# insufficient for the attempted write. The "...does not exist or you do not
# have permission..." wording is the exact opaque message that made ai-ica hard
# to diagnose: a missing-scope POST (e.g. seeding a select option without
# `object_configuration:read-write`) looks identical to a genuine 404. We bucket
# it as `insufficient_scope` so the envelope carries an actionable remediation
# instead of `unknown_error`.
_SCOPE_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "do not have permission to access it",
    "do not have permission",
    "insufficient scope",
    "missing the required scope",
    "is not authorized",
)

_SCOPE_REMEDIATION = (
    "The Attio token authenticated but lacks the scope/permission for this "
    "write. If this is a select-option or attribute seed, the token needs "
    "`object_configuration:read-write` (or pre-bootstrap the schema with "
    "scripts/attio-bootstrap-tracking_events.py). Verify the token's scopes "
    "with GET /v2/self."
)


def _looks_like_scope_error(error: Exception) -> bool:
    status = getattr(
        getattr(error, "raw_response", None),
        "status_code",
        None,
    )
    message = str(error).lower()
    if status == 403:
        return True
    return any(sub in message for sub in _SCOPE_ERROR_SUBSTRINGS)


def classify_error(error: Exception, *, strict: bool = False) -> ClassifiedError:
    if isinstance(error, AttioScopeError):
        return ClassifiedError(
            code="insufficient_scope",
            message=f"{error} {_SCOPE_REMEDIATION}",
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, DeploymentMismatchError):
        return ClassifiedError(
            code="modal_signature_mismatch",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, ConfigurationError):
        return ClassifiedError(
            code="configuration_error",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, ConnectivityError):
        return ClassifiedError(
            code="connectivity_error",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, SchemaMismatchError):
        return ClassifiedError(
            code="schema_mismatch",
            message=str(error),
            error_type=type(error).__name__,
            fatal=strict,
            field=error.field,
        )
    if isinstance(error, ConflictError):
        return ClassifiedError(
            code="conflict",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )
    if isinstance(error, (ValidationError, AttioValidationError)):
        return ClassifiedError(
            code="validation_error",
            message=str(error),
            error_type=type(error).__name__,
            fatal=True,
        )

    if _looks_like_scope_error(error):
        return ClassifiedError(
            code="insufficient_scope",
            message=f"{error} {_SCOPE_REMEDIATION}",
            error_type=type(error).__name__,
            fatal=True,
        )

    return ClassifiedError(
        code="unknown_error",
        message=str(error),
        error_type=type(error).__name__,
        fatal=True,
    )
