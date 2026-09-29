"""Upstream guard: scripts/check_upstream.py in the MISP image, one test per check.

    pytest tests/e2e/test_upstream.py [--skip-build]

Each check reads MISP's files in the image and fails when MISP changed
something this repository patches or depends on. The failure names the
change, the MISP file and the file of ours to revisit. No stack starts: one
container of the web image runs the checks, with the repository mounted.
"""

import json
import sys

import pytest

from stack import DEPLOY, REPO, TESTS, Stack

sys.path.insert(0, str(REPO / "scripts"))

import check_upstream  # noqa: E402

NAMES = [check.name for check in check_upstream.CHECKS] + [check_upstream.PHP_CHECK.name]


@pytest.fixture(scope="module")
def results(options):
    stack = Stack([DEPLOY / "docker-compose.yml", TESTS / "docker-compose.test.yml"])
    if not options["skip_build"]:
        stack.compose("build", "web", check=True, timeout=3600)
    rc, out = stack.run("web", "/repo/scripts/check_upstream.py", "/var/www/MISP", "--php", "--json",
                        entrypoint="python3", volumes=[f"{REPO}:/repo:ro"])
    line = next((l for l in reversed(out.splitlines()) if l.startswith("{")), "")
    assert line, f"check_upstream.py printed no result (exit {rc}):\n{out[-3000:]}"
    return json.loads(line)


@pytest.mark.parametrize("name", NAMES)
def test_upstream(results, name):
    assert name in results, f"check {name} did not run"
    assert not results[name], "\n".join(results[name])
