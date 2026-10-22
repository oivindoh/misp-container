#!/usr/bin/env python3
"""Merge master into the misp-3.0 branch, as the sync workflow does.

Usage: merge_into_misp3.py <ref>   (in a checkout of misp-3.0)

The branch pins MISP 3.0 where master pins 2.5: the ARG CORE_TAG and
CORE_COMMIT lines of the Dockerfile, files/composer.lock and the settings
catalogue. The branch's .gitattributes keeps its own lock, catalogue and
generated AGENTS.md whole (merge=ours; this script sets merge.ours.driver for
the merge); the sync workflow then regenerates the generated files. A Dockerfile
conflict that touches only the pin lines takes the branch's side. Any other
conflict aborts the merge and names the files.

Exit codes: 0 merged; 1 a conflict for a person to resolve; 2 usage; 3 the
branch has every commit of <ref> already.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

EXIT_CONFLICT = 1
EXIT_USAGE = 2
EXIT_NOTHING = 3

# The lines of the Dockerfile that pin the MISP version
PIN = re.compile(r"^(ARG CORE_TAG(=.*)?|ARG CORE_COMMIT(=.*)?|# MISP 3\.0 .*)$")
CONFLICT = re.compile(r"^<<<<<<<[^\n]*\n(.*?)^=======\n(.*?)^>>>>>>>[^\n]*\n", re.M | re.S)


def resolve_pin_conflicts(text: str) -> str | None:
    """The text with every conflict resolved to the branch's side, or None when a conflict is not only pin lines."""
    def pins_only(block: str) -> bool:
        return all(PIN.match(line) for line in block.splitlines() if line.strip())

    for ours, theirs in CONFLICT.findall(text):
        if not (pins_only(ours) and pins_only(theirs)):
            return None
    return CONFLICT.sub(lambda m: m.group(1), text)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def merge(ref: str) -> int:
    if git("merge-base", "--is-ancestor", ref, "HEAD", check=False).returncode == 0:
        print(f"misp-3.0 has every commit of {ref}")
        return EXIT_NOTHING
    result = git("-c", "merge.ours.driver=true", "-c", "merge.conflictStyle=merge",
                 "merge", "--no-ff", "--no-commit", ref, check=False)
    conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
    if result.returncode != 0 and not conflicted:
        print(result.stdout + result.stderr, file=sys.stderr)
        git("merge", "--abort", check=False)
        return EXIT_CONFLICT
    unresolved = []
    for path in conflicted:
        resolved = resolve_pin_conflicts(Path(path).read_text()) if path == "Dockerfile" else None
        if resolved is None:
            unresolved.append(path)
        else:
            Path(path).write_text(resolved)
            git("add", path)
    if unresolved:
        git("merge", "--abort")
        print("conflicts for a person to resolve:")
        for path in unresolved:
            print(f"  {path}")
        return EXIT_CONFLICT
    git("commit", "--quiet", "-m", f"merge master into misp-3.0 ({ref[:12]})")
    print(f"merged {ref} into misp-3.0" + (", the Dockerfile pin lines kept" if conflicted else ""))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return EXIT_USAGE
    return merge(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
