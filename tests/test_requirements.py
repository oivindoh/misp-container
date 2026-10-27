"""The hash-locked requirement files of the images: resolved for the MISP the Dockerfile pins, and from their .in files."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_misp_requirements as umr  # noqa: E402


def pins(path: Path) -> dict[str, str]:
    """{name: version} of the name==version lines of a requirements file."""
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.-]+)(?:\[[^]]*\])?==([^\s;\\]+)",
                                                                 path.read_text(), re.M)}


def test_misp_requirements_follow_the_pin():
    ref = umr.misp_ref(umr.DOCKERFILE.read_text())
    header = umr.TARGET.read_text().splitlines()[0]
    assert header.endswith(f" at {ref}."), f"{header!r}: run mise run misp-requirements for {ref}"


@pytest.mark.parametrize("source, target", umr.LOCKS, ids=lambda p: p.name)
def test_each_lock_holds_the_pins_of_its_in_file(source, target):
    locked = pins(target)
    for name, version in pins(source).items():
        assert locked.get(name) == version, f"{target.name} pins {name} {locked.get(name)}, {source.name} {version}: run mise run misp-requirements"


@pytest.mark.parametrize("target", [umr.TARGET, *(t for _, t in umr.LOCKS)], ids=lambda p: p.name)
def test_every_pin_carries_its_hashes(target):
    # The images install with --require-hashes
    body = "\n".join(line for line in target.read_text().splitlines() if not line.startswith("#")).strip()
    entries = re.split(r"\n(?=[A-Za-z0-9])", body)
    assert entries and all("--hash=sha256:" in entry for entry in entries), target.name
