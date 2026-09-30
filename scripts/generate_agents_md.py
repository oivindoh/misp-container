#!/usr/bin/env python3
"""Write AGENTS.md: a map of the repository, read from the tree itself.

Usage: generate_agents_md.py [--check]

Every line comes from the tree: module docstrings, the first comment block of
a chart template, compose file or script, the objects the chart renders, the
comments of the chart's values, the stage comments of the Dockerfile, the task runner's tasks, the upstream guard's
checks, the mise tasks and the CI jobs. To change a line, change its source
and run this again. --check exits 1 when AGENTS.md differs from the output;
tests/test_agents_md.py runs it in the unit tests.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "files"))
sys.path.insert(0, str(REPO / "scripts"))

import chart  # noqa: E402

OUT = REPO / "AGENTS.md"
DOCS = ["README.md", "DEVELOPING.md", "tests/README.md", *sorted(str(p.relative_to(REPO)) for p in (REPO / "docs").glob("*.md"))]


def rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def one_line(text: str) -> str:
    """The first paragraph of a text, on one line."""
    paragraph = text.strip().split("\n\n")[0]
    return " ".join(line.strip() for line in paragraph.splitlines())


def docstring(path: Path) -> str:
    return one_line(ast.get_docstring(ast.parse(path.read_text())) or "")


# Lines a header comment may follow: a shebang, shell options, a manifest's kind
PREAMBLE = re.compile(r"^(#!|set -|apiVersion:|kind:|---$)")


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


# -- sections --------------------------------------------------------------------------

def docs() -> str:
    rows = []
    for name in DOCS:
        title = next((l[2:].strip() for l in (REPO / name).read_text().splitlines() if l.startswith("# ")), "")
        rows.append((f"`{name}`", title))
    return "## Read first\n\n" + table(("File", "Title"), rows)


def image() -> str:
    stages = [(m.group(2), m.group(3)) for m in
              re.finditer(r"^# Stage (\d+): ([\w-]+) - (.+)$", (REPO / "Dockerfile").read_text(), re.M)]
    entrypoints = [(f"`{rel(p)}`", docstring(p)) for p in sorted((REPO / "files").glob("entrypoint-*.py"))]
    return ("## Image\n\nThe `Dockerfile` builds the images from these stages:\n\n"
            + table(("Stage", "What it holds"), stages)
            + "\n\nEach role of the main image starts from its own entrypoint:\n\n"
            + table(("Entrypoint", "Does"), entrypoints))


def library() -> str:
    rows = [(f"`{p.name}`", docstring(p)) for p in sorted((REPO / "files/misp_container").glob("*.py"))]
    return ("## Python library: `files/misp_container/`\n\nThe entrypoints, the task runner and the Jobs "
            "share this package; it is `/opt/misp_container` in the image.\n\n" + table(("Module", "Does"), rows))


def periodic() -> str:
    from misp_container import task
    rows = [(f"`{name}`", f"POST `{path}`") for name, path in task.ENDPOINTS.items()]
    rows += [(f"`{name}`", f"POST `{path}` for every server with `{flag}` on") for name, (flag, path) in task.SERVER_TASKS.items()]
    rows += [(f"`{name}`", "lists the items, then acts on each") for name in task.INDEX_TASKS]
    rows += [(f"`{task.WORKFLOW_TASK}`", "runs one workflow by ID")]
    rows += [(f"`{name}`", f"`cake {' '.join(args)}` in the pod") for name, args in task.CAKE_TASKS.items()]
    jobs = []
    for template, doc in chart.render(every_component=True):
        if doc["kind"] == "CronJob":
            command = " ".join(chart.pod_spec(doc)["containers"][0]["command"][-3:])
            jobs.append((f"`{doc['metadata']['name']}`", f"`{doc['spec']['schedule']}`", f"`{command}`",
                         f"`{chart.template_component(template)}`"))
    return ("## Periodic work\n\nMISP's own scheduler never runs. The task runner "
            "(`python3 -m misp_container.task <task>`) does each piece of periodic work:\n\n"
            + table(("Task", "Does"), rows)
            + "\n\nThe chart's components run periodic work as CronJobs, with their default schedules:\n\n"
            + table(("CronJob", "Schedule", "Runs", "Component"), jobs))


def kubernetes() -> str:
    rendered: dict[str, list[str]] = {}
    for template, doc in chart.render(every_component=True):
        name = doc["metadata"]["name"]
        if doc["kind"] == "Job":
            name = re.sub(r"-\d+$", "-<revision>", name)
        rendered.setdefault(template, []).append(f"{doc['kind']} `{name}`")
    templates = [(f"`{p.name}`", ", ".join(rendered.get(f"templates/{p.name}", [])), comment_block(p))
                 for p in sorted((chart.CHART / "templates").glob("*.yaml"))]
    comments = chart.value_comments()
    parts = chart.components()
    values = [(f"`{key}`", comments.get(key, "")) for key in comments if key not in parts]
    components = [(f"`{key}`", comments.get(key, "")) for key in parts]
    return ("## Kubernetes: the Helm chart in `deploy/chart/`\n\n" + comment_block(chart.CHART / "values.yaml")
            + "\n\n" + table(("Template", "Renders, every component on", "Note"), templates)
            + "\n\nValues:\n\n" + table(("Value", "Is"), values)
            + "\n\nComponents, each off by default:\n\n" + table(("Component", "Adds"), components))


def compose() -> str:
    rows, pending = [], []
    in_services = False
    for line in (REPO / "deploy/docker-compose.yml").read_text().splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services and line and not line.startswith(" "):
            break
        if not in_services:
            continue
        if line.startswith("  #"):
            text = line.strip().lstrip("#").strip().strip("- ").strip()
            if text:
                pending.append(text)
        elif re.match(r"^  [\w-]+:$", line):
            rows.append((f"`{line.strip()[:-1]}`", " ".join(pending)))
            pending = []
        elif not line.startswith("    #"):
            pending = [] if re.match(r"^  \S", line) else pending
    return ("## Compose: `deploy/docker-compose.yml`\n\n" + comment_block(REPO / "deploy/docker-compose.yml")
            + "\n\n" + table(("Service", "Comment"), rows))


def settings() -> str:
    """The groups of settings.yaml with the comment that opens each."""
    rows: list[list[str]] = []
    current: list[str] | None = None
    for line in (REPO / "files/misp-config/settings.yaml").read_text().splitlines():
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
    return ("## Settings: `files/misp-config/settings.yaml`\n\nMISP settings in groups; each setting's env var "
            "is its name in upper case with dots as underscores (DEVELOPING.md, Settings Engine).\n\n"
            + table(("Group", "Holds"), [tuple(r) for r in rows]))


def upstream() -> str:
    import check_upstream
    rows = [(f"`{c.name}`", c.upstream, c.ours) for c in [*check_upstream.CHECKS, check_upstream.PHP_CHECK]]
    return ("## What the image depends on in MISP\n\n" + docstring(REPO / "scripts/check_upstream.py")
            + " A failure names the change and the file of ours to revisit.\n\n"
            + table(("Check", "MISP file", "Ours"), rows))


def scripts() -> str:
    rows = []
    for path in sorted((REPO / "scripts").iterdir()):
        if path.suffix == ".py":
            rows.append((f"`{path.name}`", docstring(path)))
        elif path.suffix == ".sh":
            rows.append((f"`{path.name}`", comment_block(path)))
    return "## Scripts: `scripts/`\n\n" + table(("Script", "Does"), rows)


def tests() -> str:
    e2e = [(f"`{p.name}`", docstring(p)) for p in sorted((REPO / "tests/e2e").glob("*.py"))]
    unit = [(f"`{p.name}`", docstring(p)) for p in sorted((REPO / "tests").glob("test_*.py"))]
    other = [(f"`{rel(p)}`", comment_block(p)) for p in sorted((REPO / "tests").glob("*.sh"))]
    other += [(f"`{rel(p)}`", comment_block(p)) for p in sorted((REPO / "tests").glob("docker-compose*.yml"))]
    other += [(f"`{rel(p)}`", comment_block(p)) for p in sorted((REPO / "tests/kind").glob("*.yaml"))]
    return ("## Tests: `tests/`\n\nThe stack suites and the smoke test, in pytest (`tests/e2e/`):\n\n"
            + table(("Module", "Does"), e2e)
            + "\n\nUnit tests (`tests/`, no containers):\n\n" + table(("Module", "Tests"), unit)
            + "\n\nStack files and runners:\n\n" + table(("File", "Is"), other))


def tasks() -> str:
    mise = tomllib.loads((REPO / "mise.toml").read_text())
    rows = [(f"`mise run {name}`", spec.get("description", "")) for name, spec in mise.get("tasks", {}).items()]
    return "## Commands\n\n" + table(("Command", "Does"), rows)


def ci() -> str:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yaml").read_text())
    rows = []
    for job, spec in workflow["jobs"].items():
        needs = spec.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        steps = [s["name"] for s in spec.get("steps", []) if s.get("name")]
        rows.append((f"`{job}`", ", ".join(f"`{n}`" for n in needs), "; ".join(steps)))
    return "## CI: `.github/workflows/ci.yaml`\n\n" + table(("Job", "Needs", "Steps"), rows)


def generate() -> str:
    sections = [docs(), image(), library(), periodic(), kubernetes(), compose(), settings(), upstream(), scripts(),
                tests(), tasks(), ci()]
    head = ("# AGENTS.md\n\nA map of this repository for agents, generated by `scripts/generate_agents_md.py` from "
            "the tree. Do not edit it: change the docstring, comment or code a line comes from, then run "
            "`mise run agents-md`. The unit tests fail while this file differs from the generator's output.")
    return "\n\n".join([head, *sections]) + "\n"


def main(argv: list[str]) -> int:
    text = generate()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("AGENTS.md differs from the tree: run `mise run agents-md`", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
