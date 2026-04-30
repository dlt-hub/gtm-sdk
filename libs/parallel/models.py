from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from libs.parallel.types import FindAllGenerator, SearchMode

# --- Input models (map to SDK call signatures) ---


class SearchInput(BaseModel):
    objective: str
    mode: SearchMode = "one-shot"
    max_results: int = 10


class ExtractExcerptsInput(BaseModel):
    url: str
    objective: str


class ExtractFullContentInput(BaseModel):
    url: str


class FindAllCreateInput(BaseModel):
    objective: str
    entity_type: str
    match_conditions: list[MatchCondition] = []
    match_limit: int = 10
    generator: FindAllGenerator = "base"


class FindAllLookupInput(BaseModel):
    findall_id: str


# --- Response / result models ---


class ExtractErrorData(BaseModel):
    url: str
    error_type: str
    http_status_code: int | None = None
    content: str | None = None


class ExtractResponse(BaseModel):
    extract_id: str
    result: ExtractResultData | None = None
    errors: list[ExtractErrorData] = []


class ExtractResultData(BaseModel):
    url: str
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str] = []
    full_content: str | None = None


class FindAllCandidate(BaseModel):
    candidate_id: str
    name: str
    url: str
    match_status: str
    description: str | None = None
    output: dict[str, Any] | None = None


class FindAllResultData(BaseModel):
    findall_id: str
    candidates: list[FindAllCandidate] = []


class FindAllRunData(BaseModel):
    findall_id: str
    status: str
    is_active: bool
    generated_count: int = 0
    matched_count: int = 0


class MatchCondition(BaseModel):
    name: str
    description: str


class SearchResponse(BaseModel):
    search_id: str
    results: list[SearchResultItem] = []


class SearchResultItem(BaseModel):
    url: str
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str] = []
