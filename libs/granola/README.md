# Granola Export Workflow

`granola export` writes local-first meeting exports to markdown and JSON sidecars.

Architecture:

- `libs/granola/*` contains reusable composable building blocks (models, parsing, normalization, state, writers, API adapter).
- `src/granola/export.py` contains multi-step workflow orchestration.
- `cli/granola/export.py` remains a thin command entrypoint.

## Default Output Root

By default exports are written to:

`/Users/elvis/Documents/elviskahoro/zotero/zotero-granola`

Override with `--output` when needed.

## Commands

```bash
uv run python -m cli.main granola export --source hybrid --output /Users/elvis/Documents/elviskahoro/zotero/zotero-granola
uv run python -m cli.main granola export --source local --since 2026-03-01T00:00:00+00:00
uv run python -m cli.main granola export --debug
```

## Source Modes

- `local`: local cache only
- `api`: API only (requires `GRANOLA_API_KEY`)
- `hybrid`: local + optional API enrichment (default)

## Output Contract

- Deterministic files per meeting:
  - `notes/YYYY/YYYY-MM-DD_slug_id.md`
  - `notes/YYYY/YYYY-MM-DD_slug_id.json`
- Run metadata files under export root:
  - `manifest.jsonl`
  - `state.json`

## JSON Sidecars

The `.json` sidecar stores normalized meeting data for machine use and incremental sync:

- `id`, `title`, `notes_markdown`
- `transcript_segments`
- `transcript_source` (`local` | `api` | `preserved`)
- `transcript_status` (`present` | `missing` | `deleted_in_source`)
- `created_at` (when available)

Transcript preservation uses previous sidecars when source transcript content becomes unavailable.

## Local Cache Path

Expected local cache directory:

`~/Library/Application Support/Granola`

The exporter reads the highest available `cache-v*.json` file and supports both:

- top-level `state` payloads
- nested `cache.state` payloads

## Privacy

Exports may contain sensitive meeting notes and transcripts. Keep the output directory encrypted and access-controlled.
