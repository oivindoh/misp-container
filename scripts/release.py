#!/usr/bin/env python3
"""Prepare a release.

Usage: release.py <hotfix|normal|breaking>
       release.py [hotfix|normal|breaking]   (while the Dockerfile pins a MISP work in progress; default normal)

Once CORE_TAG is a MISP release tag, the git tag and the images carry that MISP
version, with -rN from the second release of it on, and the chart has its own
SemVer: hotfix raises the patch, normal the minor, breaking the major. While
CORE_TAG is a work-in-progress branch such as 3.0_wip, every release is the next
pre-release of its version for the git tag and the images: v3.0.0-wip.1, then
v3.0.0-wip.2. The chart takes the version the next release of the kind would
take, with the same suffix: 3.0.0-wip.1 before the first release, 4.1.0-wip.1
after chart 4.0.0. The first release after the pre-releases is their chart
version without the suffix. The script sets the chart version and appVersion,
refuses a chart version that GHCR has, shows the plan, and commits and tags once
you confirm. Run it on master, in step with origin.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHART_YAML = REPO / "deploy/chart/Chart.yaml"
CHART_REF = "oci://ghcr.io/oivindoh/charts/misp"
# The part of the chart version each kind raises: major, minor, patch
KINDS = {"breaking": 0, "normal": 1, "hotfix": 2}
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-r(\d+)|-wip\.(\d+))?$")
WIP = re.compile(r"^(\d+)\.(\d+)_wip$")


def tag_key(tag: str) -> tuple[int, int, int, int, int]:
    """v2.5.48-r3 -> (2, 5, 48, 1, 3); a pre-release sorts before its version: v3.0.0-wip.2 -> (3, 0, 0, 0, 2)."""
    major, minor, patch, revision, wip = TAG.match(tag).groups()
    if wip is not None:
        return int(major), int(minor), int(patch), 0, int(wip)
    return int(major), int(minor), int(patch), 1, int(revision or 0)


def wip_version(core_tag: str) -> str | None:
    """The version a work-in-progress pin prepares: 3.0_wip -> 3.0.0; None for a MISP release tag."""
    match = WIP.match(core_tag)
    return f"{match.group(1)}.{match.group(2)}.0" if match else None


def next_wip_tag(version: str, tags: list[str]) -> str:
    """The next pre-release tag of version (3.0.0): v3.0.0-wip.1, then v3.0.0-wip.2 and on."""
    numbers = [tag_key(t)[4] for t in tags if t.startswith(f"v{version}-wip.") and TAG.match(t)]
    return f"v{version}-wip.{max(numbers, default=0) + 1}"


def latest_release(tags: list[str]) -> str | None:
    """The newest release tag, by MISP version and then revision."""
    releases = [t for t in tags if TAG.match(t)]
    return max(releases, key=tag_key) if releases else None


def next_tag(misp: str, tags: list[str]) -> str:
    """The tag of the next release of MISP version misp (v2.5.48): v2.5.48, then v2.5.48-r1 and on."""
    revisions = [tag_key(t)[4] for t in tags if TAG.match(t) and (t == misp or t.startswith(f"{misp}-r"))]
    return f"{misp}-r{max(revisions) + 1}" if revisions else misp


def next_chart_version(previous: str | None, current: str, kind: str) -> str:
    """The chart version of a release of this kind; current when no release had a chart yet."""
    if previous is None:
        return current
    if "-" in previous:
        return previous.split("-")[0]
    parts = [int(p) for p in previous.split(".")]
    raised = KINDS[kind]
    parts[raised] += 1
    parts[raised + 1:] = [0] * (2 - raised)
    return ".".join(map(str, parts))


def wip_chart_version(previous_chart: str | None, tag: str, kind: str) -> str:
    """The chart version of a pre-release: the next release's of this kind, with the tag's suffix."""
    suffix = tag.rsplit("-", 1)[1]
    if previous_chart is None or "-" in previous_chart:
        # Before the first release, or after a pre-release: the version that one prepared
        base = (previous_chart or tag.removeprefix("v")).split("-")[0]
    else:
        base = next_chart_version(previous_chart, previous_chart, kind)
    return f"{base}-{suffix}"


# What helm prints when GHCR has no such chart; any other failure leaves the answer open
NOT_FOUND = re.compile(r"not found|manifest unknown|name unknown", re.I)


def published(chart_version: str) -> bool:
    """Whether GHCR has this chart version; helm push would replace it.

    Raises when the lookup fails for another reason than "not there", such as no
    route to GHCR: that must not pass as "not there".
    """
    result = subprocess.run(["helm", "show", "chart", CHART_REF, "--version", chart_version],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return True
    if NOT_FOUND.search(result.stderr):
        return False
    raise RuntimeError(f"cannot tell whether GHCR has chart {chart_version}: {result.stderr.strip()}")


def set_chart(text: str, version: str, app_version: str) -> str:
    text = re.sub(r"^version: .*$", f"version: {version}", text, count=1, flags=re.M)
    return re.sub(r"^appVersion: .*$", f'appVersion: "{app_version}"', text, count=1, flags=re.M)



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
    misp = re.search(r"^ARG CORE_TAG=(\S+)$", (REPO / "Dockerfile").read_text(), re.M).group(1)
    wip = wip_version(misp)
    kind = argv[0] if len(argv) == 1 else "normal" if wip and not argv else ""
    if kind not in KINDS:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    problem = refusal()
    if problem:
        print(f"refusing to release: {problem}", file=sys.stderr)
        return 1

    tags = git("tag", "--list", "v*").split()
    previous = latest_release(tags)
    previous_chart = chart_version_at(previous) if previous else None
    if wip:
        tag = next_wip_tag(wip, tags)
        image_tag = tag.removeprefix("v")
        chart_version = wip_chart_version(previous_chart, tag, kind)
    else:
        tag = next_tag(misp, tags)
        image_tag = tag.removeprefix("v")
        current_chart = yaml.safe_load(CHART_YAML.read_text())["version"]
        chart_version = next_chart_version(previous_chart, current_chart, kind)
    if published(chart_version):
        print(f"refusing to release: GHCR has chart {chart_version} already", file=sys.stderr)
        return 1

    CHART_YAML.write_text(set_chart(CHART_YAML.read_text(), chart_version, image_tag))

    print(f"Release kind:   {f'pre-release of a {kind} release' if wip else kind}")
    print(f"Previous:       {previous or 'none'}, chart {previous_chart or 'none'}")
    print(f"Git tag:        {tag}")
    print(f"Image tag:      {image_tag} (MISP {misp})")
    print(f"Chart version:  {chart_version}")
    print()
    changed = [str(CHART_YAML)]
    # Chart.yaml can name the release already, as for the first one; then HEAD takes the tag
    pending = bool(git("status", "--porcelain", "--", *changed))
    print(git("diff", "--stat") if pending else "Chart.yaml already names this release.")
    if input(f"\n{'Commit and tag' if pending else 'Tag HEAD as'} {tag}? [y/N] ").strip().lower() != "y":
        if pending:
            git("checkout", "--", *changed)
        print("Aborted; Chart.yaml is as it was.")
        return 1

    if pending:
        git("add", *changed)
        git("commit", "--quiet", "-m", f"release {tag}, chart {chart_version}")
    git("tag", tag)
    print(f"\nDone. To publish:\n  git push --atomic origin master {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
