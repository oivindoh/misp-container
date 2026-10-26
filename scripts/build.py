#!/usr/bin/env python3
"""Build the two images of this tree under one tag: misp and modules.

    python3 scripts/build.py [tag]

The tag defaults to MISP_IMAGE_TAG, else the chart's appVersion: the stack
suites run the images under that tag. CONTAINER_CMD names the engine, podman
by default. The image names are those of deploy/chart/values.yaml. APT_REFRESH,
the day of the build, makes the final stage take that day's Debian security fixes.
"""

import datetime
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "deploy" / "chart"
# The Dockerfile target of each image of the chart
TARGETS = {"misp": "final", "modules": "modules"}


def default_tag() -> str:
    return os.environ.get("MISP_IMAGE_TAG") or yaml.safe_load((CHART / "Chart.yaml").read_text())["appVersion"]


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print(__doc__, file=sys.stderr)
        return 2
    tag = argv[0] if argv else default_tag()
    engine = shlex.split(os.environ.get("CONTAINER_CMD", "podman"))
    images = yaml.safe_load((CHART / "values.yaml").read_text())["image"]
    for name, target in TARGETS.items():
        image = f"{images[name]}:{tag}"
        print(f"building {image} (target {target})", flush=True)
        refresh = f"APT_REFRESH={datetime.date.today().isoformat()}"
        command = [*engine, "build", "--target", target, "--build-arg", refresh, "--tag", image, str(REPO)]
        if subprocess.run(command).returncode != 0:
            print(f"the build of {image} failed", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
