from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TranscriptSegment(BaseModel):
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None
    text: str


class NormalizedMeeting(BaseModel):
    id: str
    title: str
    notes_markdown: str
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)
    transcript_source: Literal["local", "api", "preserved"]
    transcript_status: Literal["present", "missing", "deleted_in_source"]
    created_at: str | None = None


class ExportCliJsonPayload(BaseModel):
    """Subset of export options accepted via ``elvis granola export --json``."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["local", "api", "hybrid"] | None = None
    output: str | None = None
    since: str | None = None
    debug: bool | None = None


class ExportRunOptions(BaseModel):
    source: Literal["local", "api", "hybrid"] = "hybrid"
    output_root: Path = Path("/Users/elvis/Documents/elviskahoro/zotero/zotero-granola")
    granola_dir: Path = Path.home() / "Library" / "Application Support" / "Granola"
    since: dt.datetime | None = None
    debug: bool = False
    api_key: str | None = None
    api_notes: dict[str, dict[str, Any]] | None = None
    now: dt.datetime | None = None


class ExportRunResult(BaseModel):
    source: Literal["local", "api", "hybrid"]
    processed: int
    written: int
    skipped: int
    errors: int
    manifest_path: str
    state_path: str


class ManifestEntry(BaseModel):
    id: str
    status: Literal["written", "skipped", "error"]
    markdown_path: str | None = None
    json_path: str | None = None
    error: str | None = None


class WrittenPaths(BaseModel):
    markdown_path: Path
    json_path: Path
