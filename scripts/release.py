#!/usr/bin/env python3
"""Prepare a release of one kind: hotfix, normal or breaking.

Usage: release.py <hotfix|normal|breaking>

The git tag and the images carry the MISP version of the Dockerfile, with -rN
from the second release of that version on. The chart has its own SemVer:
hotfix raises the patch, normal the minor, breaking the major. The Chart.yaml
of the previous release holds the version it published; the first release with
a chart publishes the version Chart.yaml has. The script sets the chart version,
appVersion and the image tag of the Compose files, shows the plan, and commits
and tags once you confirm. Run it on master, in step with origin.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHART_YAML = REPO / "deploy/chart/Chart.yaml"
COMPOSE_FILES = [REPO / "deploy/docker-compose.yml", *sorted((REPO / "tests").glob("docker-compose*.yml"))]
# The part of the chart version each kind raises: major, minor, patch
KINDS = {"breaking": 0, "normal": 1, "hotfix": 2}
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-r(\d+))?$")
IMAGE_DEFAULT = re.compile(r"(ghcr\.io/oivindoh/misp-container(?:-[a-z]+)?):\$\{MISP_IMAGE_TAG:-[^}]+\}")


def tag_key(tag: str) -> tuple[int, int, int, int]:
    """v2.5.48-r3 -> (2, 5, 48, 3); the first release of a MISP version is revision 0."""
    major, minor, patch, revision = TAG.match(tag).groups()
    return int(major), int(minor), int(patch), int(revision or 0)


def latest_release(tags: list[str]) -> str | None:
    """The newest release tag, by MISP version and then revision."""
    releases = [t for t in tags if TAG.match(t)]
    return max(releases, key=tag_key) if releases else None


def next_tag(misp: str, tags: list[str]) -> str:
    """The tag of the next release of MISP version misp (v2.5.48): v2.5.48, then v2.5.48-r1 and on."""
    revisions = [tag_key(t)[3] for t in tags if TAG.match(t) and (t == misp or t.startswith(f"{misp}-r"))]
    return f"{misp}-r{max(revisions) + 1}" if revisions else misp


def next_chart_version(previous: str | None, current: str, kind: str) -> str:
    """The chart version of a release of this kind; current when no release had a chart yet."""
    if previous is None:
        return current
    parts = [int(p) for p in previous.split(".")]
    raised = KINDS[kind]
    parts[raised] += 1
    parts[raised + 1:] = [0] * (2 - raised)
    return ".".join(map(str, parts))


def set_chart(text: str, version: str, app_version: str) -> str:
    text = re.sub(r"^version: .*$", f"version: {version}", text, count=1, flags=re.M)
    return re.sub(r"^appVersion: .*$", f'appVersion: "{app_version}"', text, count=1, flags=re.M)


def set_image_default(text: str, image_tag: str) -> str:
    return IMAGE_DEFAULT.sub(lambda m: f"{m.group(1)}:${{MISP_IMAGE_TAG:-{image_tag}}}", text)


# The docs whose helm install examples carry the chart version (tests/test_docs.py checks them)
DOC_FILES = [REPO / "README.md", REPO / "docs/kubernetes.md"]
INSTALL_VERSION = re.compile(r"(helm install .*?--version )\S+")


def set_install_version(text: str, chart_version: str) -> str:
    return INSTALL_VERSION.sub(lambda m: f"{m.group(1)}{chart_version}", text)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()


def chart_version_at(tag: str) -> str | None:
    """The chart version a release tag published; None before the chart existed."""
    try:
        return yaml.safe_load(git("show", f"{tag}:deploy/chart/Chart.yaml"))["version"]
    except subprocess.CalledProcessError:
        return None


def refusal() -> str:
    """Why the tree cannot be released from; empty when it can."""
    if git("rev-parse", "--abbrev-ref", "HEAD") != "master":
        return "release from master"
    if git("status", "--porcelain", "--untracked-files=no"):
        return "commit or stash the changes to tracked files first"
    git("fetch", "--quiet", "--tags", "origin", "master")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/master"):
        return "master differs from origin/master: pull or push first"
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in KINDS:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    kind = argv[0]
    problem = refusal()
    if problem:
        print(f"refusing to release: {problem}", file=sys.stderr)
        return 1

    misp = re.search(r"^ARG CORE_TAG=(\S+)$", (REPO / "Dockerfile").read_text(), re.M).group(1)
    tags = git("tag", "--list", "v*").split()
    previous = latest_release(tags)
    tag = next_tag(misp, tags)
    image_tag = tag.removeprefix("v")
    current_chart = yaml.safe_load(CHART_YAML.read_text())["version"]
    previous_chart = chart_version_at(previous) if previous else None
    chart_version = next_chart_version(previous_chart, current_chart, kind)

    CHART_YAML.write_text(set_chart(CHART_YAML.read_text(), chart_version, image_tag))
    for path in COMPOSE_FILES:
        path.write_text(set_image_default(path.read_text(), image_tag))
    for path in DOC_FILES:
        path.write_text(set_install_version(path.read_text(), chart_version))

    print(f"Release kind:   {kind}")
    print(f"Previous:       {previous or 'none'}, chart {previous_chart or 'none'}")
    print(f"Git tag:        {tag}")
    print(f"Image tag:      {image_tag} (MISP {misp})")
    print(f"Chart version:  {chart_version}")
    print()
    print(git("diff", "--stat"))
    changed = [str(CHART_YAML), *map(str, COMPOSE_FILES), *map(str, DOC_FILES)]
    if input(f"\nCommit and tag {tag}? [y/N] ").strip().lower() != "y":
        git("checkout", "--", *changed)
        print("Aborted; the files are as they were.")
        return 1

    git("add", *changed)
    git("commit", "--quiet", "-m", f"release {tag}, chart {chart_version}")
    git("tag", tag)
    print(f"\nDone. To publish:\n  git push --atomic origin master {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
