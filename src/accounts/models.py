from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Input models ---


class ResearchInput(BaseModel):
    objective: str


class EnrichInput(BaseModel):
    url: str
    objective: str


class FindPeopleInput(BaseModel):
    query: str


class MapAccountHierarchyInput(BaseModel):
    account: str


class BatchAddPeopleInput(BaseModel):
    records: list[dict[str, Any]]
    apply: bool = False


class BatchAddCompaniesInput(BaseModel):
    records: list[dict[str, Any]]
    apply: bool = False


# --- Result models ---


class ResearchResult(BaseModel):
    objective: str
    results: list[dict[str, Any]]


class FindPeopleResult(BaseModel):
    query: str
    people: list[dict[str, Any]]


class EnrichResult(BaseModel):
    url: str
    objective: str
    data: dict[str, Any]


class MapAccountHierarchyResult(BaseModel):
    account: str
    hierarchy: list[dict[str, Any]]


class BatchMutationResult(BaseModel):
    mode: Literal["preview", "apply"]
    requested: int
    created: int
    skipped: int
    conflicts: int = 0
    errors: int = 0
    results: list[dict[str, Any]] = Field(default_factory=list)
    source: str | None = None
    applied_at: str | None = None
