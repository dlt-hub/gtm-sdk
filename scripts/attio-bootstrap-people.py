"""One-off bootstrap for github identity attributes on the people object.

Usage:
    infisical run -- uv run python scripts/attio-bootstrap-people.py --preview
    infisical run -- uv run python scripts/attio-bootstrap-people.py --apply
    infisical run -- uv run python scripts/attio-bootstrap-people.py --diff

Adds the ``github_handle`` and ``github_url`` attributes that octolens
github-source mentions need in order to upsert/link a Person (ai-0ex). Without
them, ``libs/attio/people.py::upsert_person(matching_attribute="github_handle")``
filters the people object on a slug Attio doesn't know, Attio returns a
``filter_error`` the SDK can't unmarshal, and the github mention's person link
is dropped. The dispatcher now degrades gracefully (the mention still lands),
but the person is only created/linked once these attributes exist.

``people`` is a SYSTEM object, so this script does NOT create the object — it
only creates the two declared attributes. It is idempotent and add-only: it
never deletes or alters existing attributes.

``--diff`` is read-only: it reports whether each declared attribute exists on
the live workspace (whichever ``ATTIO_API_KEY`` Infisical injects) and flags
type/flag mismatches. People carries MANY built-in system attributes; the diff
deliberately ignores every attribute it does not declare (so it does not drown
in system-attribute noise) and only ever acts on ``github_handle`` /
``github_url``. Run against prod (``--env=prod``) and dev (``--env=dev``).
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
    list_attributes,
)

OBJECT_API_SLUG = "people"


AttrType = Literal["text"]


@dataclass(frozen=True)
class AttrSpec:
    title: str
    api_slug: str
    attribute_type: AttrType
    is_unique: bool = False
    # text attributes are single-valued and not required; declared explicitly so
    # run_diff() can flag drift on every flag the script sets, not just a subset.
    is_multiselect: bool = False
    is_required: bool = False
    description: str = ""


ATTRIBUTES: tuple[AttrSpec, ...] = (
    # is_unique=False: github_handle is an identity / matching attribute, but
    # matching is done CLIENT-SIDE in upsert_person via _search_people_raw's
    # equality filter ({"github_handle": handle}) — NOT via Attio's native
    # assert endpoint — so schema-level uniqueness is not required. This mirrors
    # the system `linkedin` attribute, which is also text + non-unique and works
    # as a matching_attribute the same way. Attio additionally REJECTS
    # is_unique=True here ("Cannot set attribute as unique"): you cannot add a
    # unique attribute to the people object once it already has records.
    AttrSpec(
        "GitHub handle",
        "github_handle",
        "text",
        is_unique=False,
        description="GitHub username, used to match/link people from github mentions.",
    ),
    AttrSpec(
        "GitHub URL",
        "github_url",
        "text",
        description="https://github.com/<handle> profile URL.",
    ),
)


def run_diff() -> int:
    """Compare the declared github attributes against the live people schema.

    Read-only. The workspace is whichever ATTIO_API_KEY Infisical injected.
    Only the two declared slugs are considered — every other (system) attribute
    on people is intentionally ignored. Archived declared attributes are
    reported distinctly from absent ones (``--apply`` restores the former,
    creates the latter). Returns 1 on actionable drift (a declared attribute
    missing or archived, or a type/flag mismatch), else 0.
    """
    declared = {spec.api_slug: spec for spec in ATTRIBUTES}
    # show_archived=True so an archived slug is distinguishable from an absent
    # one — without it Attio hides archived attributes and they look "missing"
    # even though --apply would restore (un-archive) them.
    all_declared = {
        a.api_slug: a
        for a in list_attributes(OBJECT_API_SLUG, show_archived=True)
        if a.api_slug in declared
    }
    live = {slug: a for slug, a in all_declared.items() if not a.is_archived}
    archived = {slug: a for slug, a in all_declared.items() if a.is_archived}

    print("== people github attributes: declared (script) vs live (workspace) ==")
    actionable_drift = False

    missing = sorted(set(declared) - set(all_declared))
    print(f"\n[declared but ABSENT in workspace] ({len(missing)})  -> --apply creates")
    for slug in missing:
        print(f"  - {slug:15s} {declared[slug].attribute_type}")
    if not missing:
        print("  (none)")
    actionable_drift = actionable_drift or bool(missing)

    archived_slugs = sorted(archived)
    print(
        f"\n[declared but ARCHIVED in workspace] ({len(archived_slugs)})  "
        "-> --apply restores",
    )
    for slug in archived_slugs:
        print(f"  ~ {slug:15s} {declared[slug].attribute_type}")
    if not archived_slugs:
        print("  (none)")
    actionable_drift = actionable_drift or bool(archived_slugs)

    # Title drift is auto-fixable (--apply PATCHes the live title); type/flag
    # drift is NOT (Attio rejects those changes) and needs manual handling. Split
    # them so the diff tells the operator which bucket a slug falls in.
    title_drift: list[str] = []
    print("\n[title DRIFT]  -> --apply updates the live title")
    for slug in sorted(set(declared) & set(live)):
        spec = declared[slug]
        live_attr = live[slug]
        if spec.title != live_attr.title:
            title_drift.append(slug)
            print(f"  ~ {slug}: script={spec.title!r} live={live_attr.title!r}")
    if not title_drift:
        print("  (none)")
    actionable_drift = actionable_drift or bool(title_drift)

    print("\n[type / flag MISMATCHES]  -> manual handling in Attio")
    mismatches = 0
    for slug in sorted(set(declared) & set(live)):
        spec = declared[slug]
        live_attr = live[slug]
        diffs: list[str] = []
        if spec.attribute_type != live_attr.attribute_type:
            diffs.append(
                f"type: script={spec.attribute_type} live={live_attr.attribute_type}",
            )
        if spec.is_unique != live_attr.is_unique:
            diffs.append(
                f"is_unique: script={spec.is_unique} live={live_attr.is_unique}",
            )
        if spec.is_multiselect != live_attr.is_multiselect:
            diffs.append(
                f"is_multiselect: script={spec.is_multiselect} "
                f"live={live_attr.is_multiselect}",
            )
        if spec.is_required != live_attr.is_required:
            diffs.append(
                f"is_required: script={spec.is_required} live={live_attr.is_required}",
            )
        if diffs:
            mismatches += 1
            print(f"  ! {slug}")
            for detail in diffs:
                print(f"      {detail}")
    if mismatches == 0:
        print("  (none)")
    actionable_drift = actionable_drift or mismatches > 0

    if actionable_drift:
        print(
            "\nActionable drift found. Run --apply to create missing/archived "
            "attributes and converge title drift; a type/flag mismatch on an "
            "existing attribute needs manual handling in Attio. (exit 1)",
        )
        return 1
    print("\nNo actionable drift: workspace has the github attributes. (exit 0)")
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
        help="Create missing attributes on the people object.",
    )
    mode.add_argument(
        "--diff",
        action="store_true",
        help="Read-only: compare the live people schema against ATTRIBUTES.",
    )
    args = parser.parse_args()
    if args.diff:
        return run_diff()
    apply = bool(args.apply)

    print(f"[object]      {OBJECT_API_SLUG} (system object — attributes only)")

    pending = 0
    for spec in ATTRIBUTES:
        attr_result = create_attribute(
            target_object=OBJECT_API_SLUG,
            title=spec.title,
            api_slug=spec.api_slug,
            attribute_type=spec.attribute_type,
            description=spec.description,
            is_multiselect=spec.is_multiselect,
            is_required=spec.is_required,
            is_unique=spec.is_unique,
            apply=apply,
        )
        if apply:
            if attr_result.attribute_created:
                status = "created"
            elif attr_result.attribute_restored:
                status = (
                    "restored + retitled"
                    if attr_result.attribute_title_updated
                    else "restored (un-archived)"
                )
            elif attr_result.attribute_title_updated:
                status = "title updated"
            else:
                status = "exists (skip)"
        # Preview: report every write --apply WOULD perform (incl. a title PATCH
        # on an active attribute), so an operator never misses a live mutation.
        elif not attr_result.attribute_exists and not attr_result.attribute_archived:
            status = "would-create"
            pending += 1
        elif attr_result.attribute_archived:
            status = (
                "would-restore + retitle"
                if attr_result.attribute_title_drifts
                else "would-restore"
            )
            pending += 1
        elif attr_result.attribute_title_drifts:
            status = "would-retitle"
            pending += 1
        else:
            status = "exists (skip)"
        print(
            f"[attribute]   {spec.api_slug:15s}  {spec.attribute_type:6s}  {status}",
        )

    if not apply:
        print(f"{pending} change(s) pending. Run with --apply to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
