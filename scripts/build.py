#!/usr/bin/env python3
"""Build the three images of this tree under one tag: misp, caddy and modules.

    python3 scripts/build.py [tag]

The tag defaults to MISP_IMAGE_TAG, else the chart's appVersion: the stack
suites run the images under that tag. CONTAINER_CMD names the engine, podman
by default. The image names are those of deploy/chart/values.yaml.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CHART = REPO / "deploy" / "chart"
# The Dockerfile target of each image of the chart
TARGETS = {"misp": "final", "caddy": "caddy", "modules": "modules"}


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
        if subprocess.run([*engine, "build", "--target", target, "--tag", image, str(REPO)]).returncode != 0:
            print(f"the build of {image} failed", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
