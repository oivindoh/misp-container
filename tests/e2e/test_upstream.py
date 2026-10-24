"""Upstream guard: scripts/check_upstream.py in the MISP image, one test per check.

    pytest tests/e2e/test_upstream.py [--skip-build]

Each check reads MISP's files in the image and fails when MISP changed
something this repository patches or depends on. The failure names the
change, the MISP file and the file of ours to revisit. No cluster starts: one
container of the main image runs the checks under CONTAINER_CMD, with the
repository mounted.
"""

import json
import subprocess
import sys

import pytest

from cluster import IMAGES, REPO, build_images, image_engine, image_tag

sys.path.insert(0, str(REPO / "scripts"))

import check_upstream  # noqa: E402

NAMES = [check.name for check in check_upstream.CHECKS] + [check_upstream.PHP_CHECK.name]


@pytest.fixture(scope="module")
def results(options):
    build_images(options)
    result = subprocess.run([*image_engine(), "run", "--rm", "-v", f"{REPO}:/repo:ro", "--entrypoint", "python3",
                             f"{IMAGES[0]}:{image_tag()}", "/repo/scripts/check_upstream.py", "/var/www/MISP",
                             "--php", "--json"], capture_output=True, text=True, timeout=600)
    rc, out = result.returncode, result.stdout + "\n" + result.stderr
    for line in reversed(out.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    pytest.fail(f"check_upstream.py printed no JSON result (exit {rc}):\n{out[-3000:]}")


@pytest.mark.parametrize("name", NAMES)
def test_upstream(results, name):
    assert name in results, f"check {name} did not run"
    assert not results[name], "\n".join(results[name])
