#!/usr/bin/env python3
"""Fill the generated regions of the docs and AGENTS.md from the code.

Usage: generate_docs.py [--check]

A region sits between `<!-- generated: <name> -->` and `<!-- end generated -->`
in AGENTS.md, README.md, DEVELOPING.md, tests/README.md or docs/*.md; GENERATORS
names what fills it. Every line of a region comes from the tree: module
docstrings, the first comment block of a chart template, test manifest or
workflow, the objects the rendered chart holds (chart.py), the comments of the
chart's values, the Dockerfile's stage comments, the task runner, the upstream
guard, the patches, the mise tasks and the CI jobs. --check exits 1 when a
region differs from what the code gives; tests/test_docs.py runs it in the unit
tests. Edit the code, not the region.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(REPO / "files"))
sys.path.insert(0, str(SCRIPTS))

import chart  # noqa: E402

DOCS = [REPO / "AGENTS.md", REPO / "README.md", REPO / "DEVELOPING.md", REPO / "tests" / "README.md",
        *sorted((REPO / "docs").glob("*.md"))]
REGION = re.compile(r"(<!-- generated: (?P<name>[\w-]+) -->\n)(?P<body>.*?)(<!-- end generated -->)", re.S)
# Lines a header comment may follow: a shebang, shell options, a manifest's kind
PREAMBLE = re.compile(r"^(#!|set -|apiVersion:|kind:|---$)")


# -- reading the tree --------------------------------------------------------------------

def rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def one_line(text: str) -> str:
    """The first paragraph of a text, on one line."""
    paragraph = text.strip().split("\n\n")[0]
    return " ".join(line.strip() for line in paragraph.splitlines())


def docstring(path: Path) -> str:
    return one_line(ast.get_docstring(ast.parse(path.read_text())) or "")


def comment_block(path: Path) -> str:
    """The header comment of a file, on one line: the comment lines before its first line of content."""
    lines: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or PREAMBLE.match(stripped):
            if lines and not stripped:
                break
            continue
        if not stripped.startswith("#"):
            break
        text = stripped.lstrip("#").strip()
        if text and not set(text) <= set("=-"):
            lines.append(text.strip("- ").strip())
        elif lines:
            break
    return one_line("\n".join(lines))


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    return "\n".join(lines)


# -- the regions of the docs -------------------------------------------------------------

def task_cronjobs() -> dict[str, list[tuple[str, str]]]:
    """{task: [(schedule, component)]} of the chart's CronJobs that run the task runner, every component on."""
    found: dict[str, list[tuple[str, str]]] = {}
    for template, doc in chart.render(every_component=True):
        if doc["kind"] != "CronJob":
            continue
        command = chart.pod_spec(doc)["containers"][0]["args"]
        if "misp_container.task" in command:
            name = command[command.index("misp_container.task") + 1]
            found.setdefault(name, []).append((doc["spec"]["schedule"], chart.template_component(template)))
    return found


def periodic_tasks() -> str:
    from misp_container import task
    cronjobs = task_cronjobs()
    rows = []
    for name, line in task.TASKS.items():
        runs = ", ".join(f"`{schedule}` (`{component}`)" for schedule, component in cronjobs.get(name, [])) or "none"
        rows.append((f"`{name}`", task.DESCRIPTIONS[name], f"`cake {' '.join(line)}`", runs))
    return table(("Task", "Does", "Runs", "CronJob schedule (component)"), rows)


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


# The order of the Secrets table: the three every release has, then the migration's
SECRET_ORDER = ["secrets-db.env", "secrets-app.env", "secrets-admin.env", "secrets-migrate.env"]


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
        rows.append((f"`{name}`", f"`{rel(path)}`", ", ".join(f"`{k}`" for k in keys), ", ".join(readers)))
    return table(("Secret", "File", "Keys", "Read by"), rows)


def components() -> str:
    return table(("Component", "Adds"), chart.value_rows()[1])


def values() -> str:
    return table(("Value", "Is"), chart.value_rows()[0])


def unit_tests() -> str:
    return table(("Module", "Tests"), [(f"`{p.name}`", docstring(p)) for p in sorted((REPO / "tests").glob("test_*.py"))])


def upstream_checks() -> str:
    import check_upstream
    checks = [*check_upstream.CHECKS, check_upstream.PHP_CHECK]
    return table(("Check", "MISP file", "Ours", "Fails when MISP"),
                 [(f"`{c.name}`", c.upstream, c.ours, c.fails_when) for c in checks])


# -- the regions of AGENTS.md ------------------------------------------------------------

def read_first() -> str:
    rows = []
    for path in DOCS[1:]:
        title = next((line[2:].strip() for line in path.read_text().splitlines() if line.startswith("# ")), "")
        rows.append((f"`{rel(path)}`", title))
    return table(("File", "Title"), rows)


def image_stages() -> str:
    stages = [(m.group(2), m.group(3)) for m in
              re.finditer(r"^# Stage (\d+): ([\w-]+) - (.+)$", (REPO / "Dockerfile").read_text(), re.M)]
    return table(("Stage", "What it holds"), stages)


def roles() -> str:
    workloads: dict[str, list[str]] = {}
    for _, doc in chart.render(every_component=True):
        spec = chart.pod_spec(doc)
        for container in [*(spec or {}).get("initContainers", []), *(spec or {}).get("containers", [])]:
            args = container.get("args") or []
            if args[:2] == ["python3", "-m"] and args[2].startswith("misp_container."):
                name = re.sub(r"-\d+$", "-<revision>", doc["metadata"]["name"])
                workloads.setdefault(args[2], []).append(f"{doc['kind']} `{name}`")
    return table(("Command", "Run by, every component on", "Does"),
                 [(f"`python3 -m {module}`", ", ".join(dict.fromkeys(found)),
                   docstring(REPO / "files" / (module.replace(".", "/") + ".py")))
                  for module, found in sorted(workloads.items())])


def library() -> str:
    return table(("Module", "Does"),
                 [(f"`{p.name}`", docstring(p)) for p in sorted((REPO / "files/misp_container").glob("*.py"))])


def chart_templates() -> str:
    rendered: dict[str, list[str]] = {}
    for template, doc in chart.render(every_component=True):
        name = doc["metadata"]["name"]
        if doc["kind"] == "Job":
            name = re.sub(r"-\d+$", "-<revision>", name)
        rendered.setdefault(template, []).append(f"{doc['kind']} `{name}`")
    return table(("Template", "Renders, every component on", "Note"),
                 [(f"`{p.relative_to(chart.CHART / 'templates')}`",
                   ", ".join(rendered.get(str(p.relative_to(chart.CHART)), [])), comment_block(p))
                  for p in sorted((chart.CHART / "templates").rglob("*.yaml"))])


def settings_groups() -> str:
    """The groups of the chart's settings.yaml with the comment that opens each."""
    rows: list[list[str]] = []
    current: list[str] | None = None
    for line in (chart.CHART / "files/settings.yaml").read_text().splitlines():
        group = re.match(r"^  # -- ([\w_]+): (.+)$", line)
        if group:
            current = [f"`{group.group(1)}`", group.group(2).rstrip("- ").strip()]
            rows.append(current)
        elif current and line.startswith("  #"):
            piece = line.strip().lstrip("#").strip().rstrip("- ").strip()
            # A title line has no full stop; a capital after it starts the next sentence
            joint = ". " if piece[:1].isupper() and current[1][-1:].isalnum() else " "
            current[1] += joint + piece
        else:
            current = None
    return table(("Group", "Holds"), [tuple(r) for r in rows])


def patches() -> str:
    import apply_patches
    rows = []
    for path in sorted(apply_patches.PATCHES.glob("*.patch")):
        fields = apply_patches.header(path)
        rows.append((f"`{path.name}`", ", ".join(apply_patches.touched(path)), fields.get("title", ""),
                     fields.get("upstream", "")))
    return table(("Patch", "MISP file", "Change", "Upstream"), rows)


def scripts() -> str:
    rows = []
    for path in sorted(SCRIPTS.iterdir()):
        if path.suffix == ".py":
            rows.append((f"`{path.name}`", docstring(path)))
        elif path.suffix == ".sh":
            rows.append((f"`{path.name}`", comment_block(path)))
    return table(("Script", "Does"), rows)


def stack_tests() -> str:
    return table(("Module", "Does"), [(f"`{p.name}`", docstring(p)) for p in sorted((REPO / "tests/e2e").glob("*.py"))])


def stack_manifests() -> str:
    return table(("File", "Is"), [(f"`{rel(p)}`", comment_block(p)) for p in sorted((REPO / "tests/k3s").glob("*.yaml"))])


def commands() -> str:
    mise = tomllib.loads((REPO / "mise.toml").read_text())
    return table(("Command", "Does"),
                 [(f"`mise run {name}`", spec.get("description", "")) for name, spec in mise.get("tasks", {}).items()])


def ci_jobs() -> str:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yaml").read_text())
    rows = []
    for job, spec in workflow["jobs"].items():
        needs = spec.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        rows.append((f"`{job}`", ", ".join(f"`{n}`" for n in needs)))
    return table(("Job", "Needs"), rows)


def workflows() -> str:
    rows = []
    for path in sorted((REPO / ".github/workflows").glob("*.yaml")):
        spec = yaml.safe_load(path.read_text())
        # YAML 1.1 reads the key on as the boolean true
        triggers = spec.get("on", spec.get(True)) or {}
        names = []
        for event, config in (triggers.items() if isinstance(triggers, dict) else [(triggers, None)]):
            config = config if isinstance(config, dict) else {}
            refs = [*config.get("branches", []), *(f"tag {tag}" for tag in config.get("tags", []))]
            names.append(f"{event} ({', '.join(refs)})" if refs else str(event))
        rows.append((f"`{rel(path)}`", ", ".join(names), comment_block(path)))
    return table(("Workflow", "Runs on", "Does"), rows)


GENERATORS = {
    "chart-templates": chart_templates,
    "ci-jobs": ci_jobs,
    "commands": commands,
    "components": components,
    "image-stages": image_stages,
    "library": library,
    "patches": patches,
    "periodic-tasks": periodic_tasks,
    "read-first": read_first,
    "roles": roles,
    "scripts": scripts,
    "secrets": secrets,
    "settings-groups": settings_groups,
    "stack-manifests": stack_manifests,
    "stack-tests": stack_tests,
    "unit-tests": unit_tests,
    "upstream-checks": upstream_checks,
    "values": values,
    "workflows": workflows,
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
            stale.append(rel(doc))
            if "--check" not in argv:
                doc.write_text(rendered)
    if "--check" in argv and stale:
        print(f"generated regions differ from the code in {', '.join(stale)}: run `mise run docs`", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
