"""Render the Helm chart in deploy/chart for the doc generators and the tests.

render() runs helm template and returns each object with the template it came
from; components() and value_comments() read values.yaml. helm must be on
PATH (mise install).
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "deploy/chart"
SOURCE = re.compile(r"^# Source: [\w-]+/(templates/[\w./-]+)$", re.M)
TOP_KEY = re.compile(r"^([A-Za-z]\w*):")


def components() -> list[str]:
    """The top-level keys of values.yaml with an enabled flag, in file order."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    return [key for key, value in values.items() if isinstance(value, dict) and "enabled" in value]


def value_comments() -> dict[str, str]:
    """{top-level key: the comment block right above it in values.yaml, on one line}."""
    comments: dict[str, str] = {}
    block: list[str] = []
    for line in (CHART / "values.yaml").read_text().splitlines():
        if line.startswith("#"):
            block.append(line.lstrip("#").strip())
            continue
        match = TOP_KEY.match(line)
        if match and block:
            comments[match.group(1)] = " ".join(block)
        if line.strip() == "" or match:
            block = []
    return comments


def value_rows() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(values, components): the (`key`, comment) rows of values.yaml for the docs, the components apart."""
    comments = value_comments()
    parts = components()
    values = [(f"`{key}`", text) for key, text in comments.items() if key not in parts]
    return values, [(f"`{key}`", comments.get(key, "")) for key in parts]


def template_component(template: str) -> str:
    """The component a template belongs to, from its header ("# The <key> component"); empty for the rest."""
    match = re.match(r"# The (\w+) component", (CHART / template).read_text())
    return match.group(1) if match else ""


@functools.lru_cache(maxsize=None)
def _render_text(args: tuple[str, ...]) -> str:
    if not shutil.which("helm"):
        raise RuntimeError("helm is not on PATH: run mise install in the repository")
    result = subprocess.run(["helm", "template", "misp", str(CHART), "--namespace", "misp", *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"helm template {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


@functools.lru_cache(maxsize=None)
def _render(args: tuple[str, ...]) -> tuple[tuple[str, dict], ...]:
    found = []
    for chunk in re.split(r"^---\s*$", _render_text(args), flags=re.M):
        source = SOURCE.search(chunk)
        doc = yaml.safe_load(chunk)
        if source and isinstance(doc, dict):
            found.append((source.group(1), doc))
    return tuple(found)


def _args(sets: tuple[str, ...], every_component: bool) -> tuple[str, ...]:
    args: list[str] = []
    for value in [*(f"{c}.enabled=true" for c in components() if every_component), *sets]:
        args += ["--set", value]
    return tuple(args)


def render_text(*sets: str, every_component: bool = False) -> str:
    """The YAML helm template prints with these --set values, or with every component on."""
    return _render_text(_args(sets, every_component))


def render(*sets: str, every_component: bool = False) -> list[tuple[str, dict]]:
    """[(template, object)] of helm template with these --set values, or with every component on."""
    return list(_render(_args(sets, every_component)))


def objects(*sets: str, every_component: bool = False) -> list[dict]:
    return [doc for _, doc in render(*sets, every_component=every_component)]


def find(kind: str, name: str, *sets: str, every_component: bool = False) -> dict:
    """The rendered object of this kind and name; a Job's name without its revision."""
    for doc in objects(*sets, every_component=every_component):
        found = doc["metadata"]["name"]
        if doc["kind"] == "Job":
            found = re.sub(r"-\d+$", "", found)
        if doc["kind"] == kind and found == name:
            return doc
    raise KeyError(f"{kind} {name} is not in the render")


def pod_spec(doc: dict) -> dict | None:
    """The pod spec of a workload or a Pod; None for anything else."""
    spec = doc.get("spec", {})
    if doc.get("kind") == "Pod":
        return spec
    if doc.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec") if doc.get("kind") in ("Deployment", "StatefulSet", "Job", "CronJob") else None
