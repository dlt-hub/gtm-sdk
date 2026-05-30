"""One-off bootstrap for the social_mention custom object.

Usage:
    infisical run -- uv run python scripts/attio-bootstrap-social_mentions.py --preview
    infisical run -- uv run python scripts/attio-bootstrap-social_mentions.py --apply
    infisical run -- uv run python scripts/attio-bootstrap-social_mentions.py --diff

Idempotent. Safe to re-run. Adding a new attribute = edit ATTRIBUTES below
and re-run.

``--diff`` is read-only: it dumps the live ``social_mention`` schema of the
workspace selected by the injected ``ATTIO_API_KEY`` and compares it to the
``ATTRIBUTES`` declared below — reporting attributes declared-but-missing,
present-but-undeclared (prod-only drift), type/flag mismatches, and select /
status option-vocabulary gaps. The workspace is whichever key Infisical injects,
so run it against prod (``--env=prod``) and dev (``--env=dev``) to compare them.
Prod is the source of truth: reconcile ``ATTRIBUTES`` to match live prod, then
``--apply`` against dev to bring dev into parity.

Mirroring is **add-only**: ``--apply`` creates missing attributes/options but
never deletes or alters existing ones. Attributes that exist on dev but not prod
(or type mismatches) are reported by ``--diff`` for manual handling — Attio
attribute deletion via API has edge cases not worth automating here.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Anchor on the script's own directory so the script runs correctly regardless
# of CWD (per repo CLAUDE.md path-anchoring rule). `uv run path/to/script.py`
# does NOT chdir into the script's folder.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from libs.attio.attributes import (  # noqa: E402
    create_attribute,
    ensure_select_options,
    list_attributes,
    list_select_options,
    list_status_options,
)
from libs.attio.objects import create_object  # noqa: E402

# Open-vocabulary selects: every slug here is JIT-seeded at write time by
# libs/attio/mentions.py::_SINGLE_SELECT_FIELDS / _MULTISELECT_FIELDS, so their
# live option set legitimately grows beyond whatever seed_options the script
# declares. --diff treats *live-only* options on these as expected growth (not
# actionable drift) and annotates them; a *declared-only* option (a seeded floor
# value the workspace lacks) is still real drift. keywords/octolens_tags seed
# nothing (pure open vocab); source_platform seeds a floor that still grows.
_OPEN_VOCAB_SELECTS: frozenset[str] = frozenset(
    {"keywords", "octolens_tags", "source_platform"},
)

OBJECT_API_SLUG = "social_mention"
OBJECT_SINGULAR = "Social mention"
OBJECT_PLURAL = "Social mentions"


AttrType = Literal[
    "text",
    "select",
    "checkbox",
    "number",
    "timestamp",
    "status",
    "record-reference",
]


@dataclass(frozen=True)
class AttrSpec:
    title: str
    api_slug: str
    attribute_type: AttrType
    is_multiselect: bool = False
    is_unique: bool = False
    is_required: bool = False
    description: str = ""
    allowed_objects: tuple[str, ...] = ()
    # Closed-vocabulary select options to seed at bootstrap. Open-vocab selects
    # (keywords, octolens_tags) leave this empty and rely on runtime ensure in
    # libs/attio/mentions.py.
    seed_options: tuple[str, ...] = ()


ATTRIBUTES: tuple[AttrSpec, ...] = (
    AttrSpec("Mention URL", "mention_url", "text", is_unique=True),
    AttrSpec(
        # Seeded to mirror prod's live vocabulary (2026-05-29 schema audit) so a
        # freshly-bootstrapped dev workspace reaches parity immediately. New
        # platforms still self-register JIT at write time (source_platform is in
        # libs/attio/mentions.py::_SINGLE_SELECT_FIELDS), so this list is a
        # floor, not a closed set.
        "Source platform",
        "source_platform",
        "select",
        seed_options=(
            "bluesky",
            "dev",
            "github",
            "hackernews",
            "linkedin",
            "newsletter",
            "podcasts",
            "reddit",
            "stackoverflow",
            "tiktok",
            "twitter",
            "youtube",
        ),
    ),
    AttrSpec("Source ID", "source_id", "text"),
    AttrSpec("Mention title", "mention_title", "text"),
    AttrSpec("Mention body", "mention_body", "text"),
    AttrSpec("Mention timestamp", "mention_timestamp", "timestamp"),
    AttrSpec("Author handle", "author_handle", "text"),
    AttrSpec("Author profile URL", "author_profile_url", "text"),
    AttrSpec("Author avatar URL", "author_avatar_url", "text"),
    AttrSpec(
        "Relevance score",
        "relevance_score",
        "select",
        # "unknown" supports the Octolens CSV backfill (relevance not scored at
        # export time). Seeded here for --diff parity; the writer JIT-creates it.
        seed_options=("high", "medium", "low", "unknown"),
    ),
    AttrSpec("Relevance comment", "relevance_comment", "text"),
    AttrSpec("Primary keyword", "primary_keyword", "text"),
    AttrSpec("Keywords", "keywords", "select", is_multiselect=True),
    AttrSpec("Octolens tags", "octolens_tags", "select", is_multiselect=True),
    AttrSpec(
        "Sentiment",
        "sentiment",
        "select",
        seed_options=("Positive", "Neutral", "Negative"),
    ),
    AttrSpec("Language", "language", "text"),
    AttrSpec("Subreddit", "subreddit", "text"),
    AttrSpec("View ID", "view_id", "number"),
    AttrSpec("View name", "view_name", "text"),
    AttrSpec("Bookmarked", "bookmarked", "checkbox"),
    AttrSpec("Image URL", "image_url", "text"),
    AttrSpec(
        "Last action",
        "last_action",
        "select",
        seed_options=("mention_created", "mention_updated"),
    ),
    AttrSpec("Triage status", "triage_status", "status"),
    AttrSpec(
        "Related person",
        "related_person",
        "record-reference",
        allowed_objects=("people",),
    ),
    AttrSpec(
        "Related company",
        "related_company",
        "record-reference",
        allowed_objects=("companies",),
    ),
)


def run_diff() -> int:
    """Compare the live workspace schema against the declared ATTRIBUTES.

    Read-only. The workspace is whichever ATTIO_API_KEY Infisical injected.

    Returns 1 when *actionable* drift is found, else 0 — so --diff can gate a
    preflight, not just print a report. Actionable drift = a declared attribute
    missing from the workspace, an undeclared non-system attribute present in
    the workspace, a type/flag/allowed_objects mismatch, or an option gap that
    is not merely expected open-vocab growth. Live-only options on
    _OPEN_VOCAB_SELECTS slugs (which grow JIT at write time) are reported but do
    NOT flip the exit code; a declared-only (missing seed floor) option always
    does.
    """
    actionable_drift = False
    declared = {spec.api_slug: spec for spec in ATTRIBUTES}
    live = {
        a.api_slug: a for a in list_attributes(OBJECT_API_SLUG) if not a.is_archived
    }

    print("== social_mention schema diff: declared (script) vs live (workspace) ==")
    if not live:
        print(
            f"  live workspace reports NO attributes on '{OBJECT_API_SLUG}' "
            "(object missing in this workspace, or only system attributes). "
            "All declared attributes below would be created by --apply.",
        )

    declared_slugs = set(declared)
    live_slugs = set(live)

    missing = sorted(declared_slugs - live_slugs)
    undeclared = sorted(s for s in live_slugs - declared_slugs if not live[s].is_system)
    undeclared_system = sorted(
        s for s in live_slugs - declared_slugs if live[s].is_system
    )

    actionable_drift = actionable_drift or bool(missing) or bool(undeclared)

    print(f"\n[declared but MISSING in workspace] ({len(missing)})  -> --apply creates")
    for slug in missing:
        print(f"  - {slug:25s} {declared[slug].attribute_type}")
    if not missing:
        print("  (none)")

    print(
        f"\n[present in workspace but NOT declared] ({len(undeclared)})  "
        "-> prod-only / drift; reconcile into ATTRIBUTES if prod has it",
    )
    for slug in undeclared:
        live_attr = live[slug]
        multi = " multiselect" if live_attr.is_multiselect else ""
        print(f"  + {slug:25s} {live_attr.attribute_type}{multi}")
    if not undeclared:
        print("  (none)")
    if undeclared_system:
        print(f"  ({len(undeclared_system)} system attribute(s) ignored)")

    print("\n[type / flag / allowed_objects MISMATCHES]")
    mismatches = 0
    for slug in sorted(declared_slugs & live_slugs):
        spec = declared[slug]
        live_attr = live[slug]
        diffs: list[str] = []
        # `description` is deliberately NOT compared: the script seeds "" for most
        # attributes while prod legitimately carries richer human-authored copy,
        # so a description check would false-positive on nearly every attribute.
        # Title and is_required ARE structural and compared below.
        if spec.title != live_attr.title:
            diffs.append(f"title: script={spec.title!r} live={live_attr.title!r}")
        if spec.attribute_type != live_attr.attribute_type:
            diffs.append(
                f"type: script={spec.attribute_type} live={live_attr.attribute_type}",
            )
        if spec.is_multiselect != live_attr.is_multiselect:
            diffs.append(
                f"is_multiselect: script={spec.is_multiselect} live={live_attr.is_multiselect}",
            )
        if spec.is_unique != live_attr.is_unique:
            diffs.append(
                f"is_unique: script={spec.is_unique} live={live_attr.is_unique}",
            )
        if spec.is_required != live_attr.is_required:
            diffs.append(
                f"is_required: script={spec.is_required} live={live_attr.is_required}",
            )
        if spec.attribute_type == "record-reference" and set(
            spec.allowed_objects,
        ) != set(
            live_attr.allowed_objects,
        ):
            diffs.append(
                f"allowed_objects: script={sorted(spec.allowed_objects)} "
                f"live={sorted(live_attr.allowed_objects)}",
            )
        if diffs:
            mismatches += 1
            print(f"  ! {slug}")
            for detail in diffs:
                print(f"      {detail}")
    if mismatches == 0:
        print("  (none)")
    actionable_drift = actionable_drift or mismatches > 0

    print("\n[select / status OPTION vocab diffs]  (live vs declared seed_options)")
    option_diffs = 0
    for slug in sorted(declared_slugs & live_slugs):
        spec = declared[slug]
        live_attr = live[slug]
        if live_attr.attribute_type not in ("select", "status"):
            continue
        if live_attr.attribute_type == "select":
            live_options = set(
                list_select_options(target_object=OBJECT_API_SLUG, attribute_slug=slug),
            )
        else:
            live_options = set(
                list_status_options(target_object=OBJECT_API_SLUG, attribute_slug=slug),
            )
        declared_options = set(spec.seed_options)
        live_only = sorted(live_options - declared_options)
        declared_only = sorted(declared_options - live_options)
        if not live_only and not declared_only:
            continue
        option_diffs += 1
        is_open_vocab = slug in _OPEN_VOCAB_SELECTS
        is_status = live_attr.attribute_type == "status"
        # status vocabularies (e.g. triage_status) are human-managed in the Attio
        # UI and cannot be seeded via this bootstrap, so their diffs are reported
        # but never gate the exit code. For non-status selects: a missing seed
        # floor (declared_only) is always real drift; live-only options are drift
        # only for closed vocabularies — on open-vocab slugs they are expected
        # JIT growth and must not flip the exit code.
        if not is_status and (declared_only or (live_only and not is_open_vocab)):
            actionable_drift = True
        if is_status:
            note = " (status: human-managed, reported not gated)"
        elif is_open_vocab:
            note = " (open-vocab: grows JIT, live-only expected)"
        else:
            note = ""
        print(f"  ~ {slug} [{live_attr.attribute_type}]{note}")
        if live_only:
            print(f"      live-only (workspace has, script omits): {live_only}")
        if declared_only:
            print(f"      declared-only (script seeds, live lacks): {declared_only}")
    if option_diffs == 0:
        print("  (none)")

    if actionable_drift:
        print(
            "\nActionable drift found. Prod is source of truth: reconcile "
            "ATTRIBUTES to match live prod, then --apply against dev. Mirror is "
            "add-only — undeclared/mismatched live attributes need manual "
            "handling in Attio. (exit 1)",
        )
        return 1
    print("\nNo actionable drift: workspace matches declared schema. (exit 0)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Print what would happen; no writes.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Create missing object + attributes.",
    )
    mode.add_argument(
        "--diff",
        action="store_true",
        help="Read-only: compare the live workspace schema against ATTRIBUTES.",
    )
    args = parser.parse_args()
    if args.diff:
        return run_diff()
    apply = bool(args.apply)

    print(f"[object]      {OBJECT_API_SLUG}")
    obj_result = create_object(
        api_slug=OBJECT_API_SLUG,
        singular_noun=OBJECT_SINGULAR,
        plural_noun=OBJECT_PLURAL,
        apply=apply,
    )
    if obj_result.object_created:
        obj_action = "created"
    elif obj_result.object_exists:
        obj_action = "exists"
    else:
        obj_action = "would-create"
    print(f"              {obj_action}")

    pending = 0
    for spec in ATTRIBUTES:
        attr_result = create_attribute(
            target_object=OBJECT_API_SLUG,
            title=spec.title,
            api_slug=spec.api_slug,
            attribute_type=spec.attribute_type,
            description=spec.description,
            is_multiselect=spec.is_multiselect,
            is_unique=spec.is_unique,
            allowed_objects=list(spec.allowed_objects) or None,
            apply=apply,
        )
        if attr_result.attribute_created:
            status = "created"
        elif attr_result.attribute_exists:
            status = "exists (skip)"
        else:
            status = "would-create"
            pending += 1
        print(
            f"[attribute]   {spec.api_slug:25s}  {spec.attribute_type:14s}  {status}",
        )

        if (
            spec.seed_options
            and apply
            and (attr_result.attribute_exists or attr_result.attribute_created)
        ):
            created_options = ensure_select_options(
                target_object=OBJECT_API_SLUG,
                attribute_slug=spec.api_slug,
                options=list(spec.seed_options),
            )
            for opt in spec.seed_options:
                opt_status = "created" if opt in created_options else "exists (skip)"
                print(f"  [option]    {spec.api_slug:25s}  {opt:20s}  {opt_status}")
        elif spec.seed_options and not apply:
            for opt in spec.seed_options:
                print(f"  [option]    {spec.api_slug:25s}  {opt:20s}  would-ensure")

    if not apply:
        print(f"{pending} creates pending. Run with --apply to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
