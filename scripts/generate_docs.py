#!/usr/bin/env python3
"""Fill the generated regions of the docs from the code.

Usage: generate_docs.py [--check]

A region sits between `<!-- generated: <name> -->` and `<!-- end generated -->`
in README.md, DEVELOPING.md, tests/README.md or docs/*.md; GENERATORS names what
fills it. The Kubernetes facts come from the rendered chart (chart.py). --check
exits 1 when a region differs from what the code gives; tests/test_docs.py runs
it in the unit tests. Edit the code, not the region.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(REPO / "files"))
sys.path.insert(0, str(SCRIPTS))

import chart  # noqa: E402
from generate_agents_md import table  # noqa: E402

DOCS = [REPO / "README.md", REPO / "DEVELOPING.md", REPO / "tests" / "README.md", *sorted((REPO / "docs").glob("*.md"))]
REGION = re.compile(r"(<!-- generated: (?P<name>[\w-]+) -->\n)(?P<body>.*?)(<!-- end generated -->)", re.S)


def task_cronjobs() -> dict[str, list[tuple[str, str]]]:
    """{task: [(schedule, component)]} of the chart's CronJobs that run the task runner, every component on."""
    found: dict[str, list[tuple[str, str]]] = {}
    for template, doc in chart.render(every_component=True):
        if doc["kind"] != "CronJob":
            continue
        command = chart.pod_spec(doc)["containers"][0]["command"]
        if "misp_container.task" in command:
            name = command[command.index("misp_container.task") + 1]
            found.setdefault(name, []).append((doc["spec"]["schedule"], chart.template_component(template)))
    return found


def periodic_tasks() -> str:
    from misp_container import task
    cronjobs = task_cronjobs()
    rows = []
    for name in task.TASKS:
        kind = "console" if name in task.CAKE_TASKS else "API"
        runs = ", ".join(f"`{schedule}` (`{component}`)" for schedule, component in cronjobs.get(name, [])) or "none"
        rows.append((f"`{name}`", task.DESCRIPTIONS[name], kind, runs))
    return table(("Task", "Does", "Kind", "CronJob schedule (component)"), rows)


def secret_users() -> dict[str, dict[str, set[str]]]:
    """{Secret: {user: keys}}: which workloads read a Secret, all of it (empty set) or some keys.

    A workload is named for its component, or for itself outside one; an init
    container for itself.
    """
    users: dict[str, dict[str, set[str]]] = {}
    for template, doc in chart.render(every_component=True):
        spec = chart.pod_spec(doc)
        if not spec:
            continue
        owner = chart.template_component(template) or re.sub(r"-\d+$", "", doc["metadata"]["name"])
        containers = [(c["name"], c) for c in spec.get("initContainers", [])] + [(owner, c) for c in spec["containers"]]
        for user, c in containers:
            for source in c.get("envFrom", []):
                if "secretRef" in source:
                    users.setdefault(source["secretRef"]["name"], {})[user] = set()
            for var in c.get("env", []):
                ref = var.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    keys = users.setdefault(ref["name"], {}).setdefault(user, {ref["key"]})
                    if keys:
                        keys.add(ref["key"])
    return users


def secrets() -> str:
    users = secret_users()
    rows = []
    def order(path: Path):
        # the known ones in their order, then any new one by name
        return (path.name not in SECRET_ORDER, SECRET_ORDER.index(path.name) if path.name in SECRET_ORDER else 0, path.name)

    for path in sorted((chart.CHART / "files").glob("secrets-*.env"), key=order):
        name = "misp-" + path.stem.removeprefix("secrets-")
        keys = [line.split("=", 1)[0] for line in path.read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#")]
        readers = []
        for user, subset in sorted(users.get(name, {}).items()):
            readers.append(f"{user} ({', '.join(f'`{k}`' for k in sorted(subset))})" if subset else user)
        rows.append((f"`{name}`", f"`{path.relative_to(REPO)}`", ", ".join(f"`{k}`" for k in keys), ", ".join(readers)))
    return table(("Secret", "File", "Keys", "Read by"), rows)


# The order of the Secrets table: the three every release has, then the migration's
SECRET_ORDER = ["secrets-db.env", "secrets-app.env", "secrets-admin.env", "secrets-migrate.env"]


def components() -> str:
    return table(("Component", "Adds"), chart.value_rows()[1])


def values() -> str:
    return table(("Value", "Is"), chart.value_rows()[0])


GENERATORS = {
    "components": components,
    "periodic-tasks": periodic_tasks,
    "secrets": secrets,
    "values": values,
}


def render(text: str) -> str:
    """The text with every generated region filled; an unknown region name raises KeyError."""
    return REGION.sub(lambda m: m.group(1) + GENERATORS[m.group("name")]() + "\n" + m.group(4), text)


def main(argv: list[str]) -> int:
    stale = []
    for doc in DOCS:
        text = doc.read_text()
        rendered = render(text)
        if rendered != text:
            stale.append(str(doc.relative_to(REPO)))
            if "--check" not in argv:
                doc.write_text(rendered)
    if "--check" in argv and stale:
        print(f"generated regions differ from the code in {', '.join(stale)}: run `mise run docs`", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
