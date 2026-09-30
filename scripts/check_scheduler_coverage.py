#!/usr/bin/env python3
"""Fail when MISP's scheduler offers work that no task runner task covers.

Usage: check_scheduler_coverage.py <app/Console/Command/SchedulerWorkerShell.php>

Reads the task types, the task actions and ADMIN_ACTIONS from MISP's scheduler
shell and compares them with SCHEDULER_COVERAGE in misp_container/task.py.
Exits 1 and names each uncovered item.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files"))

from misp_container.task import SCHEDULER_COVERAGE  # noqa: E402

TYPE_RE = re.compile(r"\$task\['type'\]\s*===?\s*'([^']+)'")
ACTION_RE = re.compile(r"\$task\['action'\]\s*[!=]==?\s*'(\w+)'")
IN_ARRAY_RE = re.compile(r"in_array\(\s*\$task\['action'\]\s*,\s*\[([^\]]*)\]")
ADMIN_RE = re.compile(r"const\s+ADMIN_ACTIONS\s*=\s*\[(.*?)\];", re.S)
QUOTED_RE = re.compile(r"'(\w+)'")


def offered(source: str) -> tuple[set[str], set[str], set[str]]:
    """(task types, non-admin actions, admin actions) named in the scheduler shell."""
    code = re.sub(r"//[^\n]*", "", source)
    types = set(TYPE_RE.findall(code))
    actions = set(ACTION_RE.findall(code))
    for listed in IN_ARRAY_RE.findall(code):
        actions.update(QUOTED_RE.findall(listed))
    admin = ADMIN_RE.search(code)
    admin_actions = set(QUOTED_RE.findall(admin.group(1))) if admin else set()
    # runAdminTask compares some admin actions to shape their job arguments
    return types, actions - admin_actions, admin_actions


def uncovered(source: str, coverage: dict = SCHEDULER_COVERAGE) -> list[str]:
    types, actions, admin_actions = offered(source)
    if not types:
        return ["no task types found: the scheduler shell changed shape, update this check"]
    covered_types = {t for t, _ in coverage}
    covered_actions = {a for t, a in coverage if t != "Admin" and a}
    covered_admin = {a for t, a in coverage if t == "Admin"}
    missing = [f"task type {t!r}" for t in sorted(types - covered_types)]
    missing += [f"task action {a!r}" for a in sorted(actions - covered_actions)]
    missing += [f"admin action {a!r}" for a in sorted(admin_actions - covered_admin)]
    return missing


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        source = f.read()
    missing = uncovered(source)
    for item in missing:
        print(f"not covered by the task runner: {item}")
    if not missing:
        types, actions, admin_actions = offered(source)
        print(f"covered: {len(types)} task types, {len(actions)} actions, {len(admin_actions)} admin actions")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
