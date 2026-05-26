from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from src.accounts import tasks
from src.accounts.models import BatchMutationResult
from src.api_keys import inject_api_keys
from src.app import app, image
from src.secrets_bootstrap import bootstrap_secret, with_secrets


class BatchAddPeopleQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[dict[str, Any]]
    apply: bool = False


class BatchAddCompaniesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[dict[str, Any]]
    apply: bool = False


@app.function(image=image, secrets=[bootstrap_secret()])
@with_secrets("ATTIO_API_KEY")
def gtm_batch_add_people(
    payload: dict[str, Any],
    api_keys: dict[str, str] | None = None,
) -> BatchMutationResult:
    with inject_api_keys(api_keys or {}):
        query = BatchAddPeopleQuery.model_validate(payload)
        return BatchMutationResult.model_validate(
            tasks.batch_add_people(query.records, apply=query.apply),
        )


@app.function(image=image, secrets=[bootstrap_secret()])
@with_secrets("ATTIO_API_KEY")
def gtm_batch_add_companies(
    payload: dict[str, Any],
    api_keys: dict[str, str] | None = None,
) -> BatchMutationResult:
    with inject_api_keys(api_keys or {}):
        query = BatchAddCompaniesQuery.model_validate(payload)
        return BatchMutationResult.model_validate(
            tasks.batch_add_companies(query.records, apply=query.apply),
        )
