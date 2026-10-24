#!/usr/bin/env python3
"""Apply this repository's patches to a MISP tree, and fail when one no longer fits.

Usage: apply_patches.py <MISP directory> [patch directory]

Each file in patches/ is a git diff against MISP's root after a header: a title,
then Why: and Upstream: lines. The patches apply in name order. A patch that
MISP already carries, or one that no longer applies, stops the run with a line
that names it, so the image build fails until the patch is deleted or adapted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PATCHES = Path(__file__).resolve().parents[1] / "patches"


def header(path: Path) -> dict[str, str]:
    """The title and the Key: value lines before a patch's diff."""
    fields = {}
    for line in path.read_text().splitlines():
        if line.startswith("diff --git"):
            break
        if not fields and line.strip():
            fields["title"] = line.strip()
        elif ": " in line:
            key, _, value = line.partition(": ")
            fields[key.strip().lower()] = value.strip()
    return fields


def touched(path: Path) -> list[str]:
    """The MISP files a patch changes."""
    return [line[len("+++ b/"):] for line in path.read_text().splitlines() if line.startswith("+++ b/")]


def applies(misp: Path, patch: Path, *args: str) -> bool:
    return subprocess.run(["git", "apply", "--check", *args, str(patch)], cwd=misp,
                          capture_output=True).returncode == 0


def apply_all(misp: Path, patches: list[Path]) -> list[str]:
    """Apply each patch that fits; one line per patch that does not."""
    problems = []
    for patch in patches:
        if applies(misp, patch):
            subprocess.run(["git", "apply", str(patch)], cwd=misp, check=True)
            print(f"applied {patch.name}: {header(patch).get('title', '')}")
        elif applies(misp, patch, "--reverse"):
            problems.append(f"{patch.name}: MISP already carries this change; delete the patch")
        else:
            problems.append(f"{patch.name}: no longer applies to {', '.join(touched(patch))}; check whether "
                            f"MISP fixed it (upstream: {header(patch).get('upstream', 'unknown')}), "
                            "then adapt or delete the patch")
    return problems


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    misp = Path(argv[0])
    directory = Path(argv[1]) if len(argv) == 2 else PATCHES
    problems = apply_all(misp, sorted(directory.glob("*.patch")))
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
