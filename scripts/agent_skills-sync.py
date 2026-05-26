"""Sync agent skills into `<gtm-sdk>/.agents/skills/` as symlinks.

Composes two sources, with the local layer overriding the parent on
name collisions:

1. Parent repo: `/Users/elvis/Documents/ai/.agents/skills/*`
2. Local repo: `<gtm-sdk>/skills/*`

Existing symlinks in the destination are wiped first so renames and
deletes in either source propagate cleanly. Real directories (which
should not exist) are left alone.

Usage:
    uv run python scripts/sync-agent-skills.py
"""

from __future__ import annotations

from pathlib import Path

import typer

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_PARENT_SRC = Path("/Users/elvis/Documents/ai/.agents/skills")
DEFAULT_LOCAL_SRC = REPO_ROOT / "skills"
DEFAULT_DST = REPO_ROOT / ".agents" / "skills"


def wipe_symlinks(dst: Path) -> None:
    """Remove every symlink directly under `dst`, leaving real dirs in place."""
    for child in dst.iterdir():
        if child.is_symlink():
            child.unlink()


def link_subdirs(src: Path, dst: Path) -> int:
    """Symlink each immediate subdirectory of `src` into `dst`.

    Replaces any existing symlink at the destination name (last writer
    wins, which is how the local layer overrides the parent).
    """
    if not src.is_dir():
        return 0
    count = 0
    for child in sorted(src.iterdir()):
        if not child.is_dir():
            continue
        target = dst / child.name
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(child)
        count += 1
    return count


def main(
    parent_src: Path = typer.Option(
        DEFAULT_PARENT_SRC,
        help="Skills dir from the parent `ai/` repo. Linked first.",
    ),
    local_src: Path = typer.Option(
        DEFAULT_LOCAL_SRC,
        help="gtm-sdk local skills dir. Linked second, overrides parent.",
    ),
    dst: Path = typer.Option(
        DEFAULT_DST,
        help="Destination `.agents/skills/` directory.",
    ),
) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    wipe_symlinks(dst)
    parent_count = link_subdirs(parent_src, dst)
    local_count = link_subdirs(local_src, dst)
    typer.echo(
        f"linked {parent_count} parent + {local_count} local skills into {dst}",
    )


if __name__ == "__main__":
    typer.run(main)
