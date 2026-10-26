"""files/requirements-misp.txt was resolved for the MISP the Dockerfile pins."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_misp_requirements as umr  # noqa: E402


def test_misp_requirements_follow_the_pin():
    ref = umr.misp_ref(umr.DOCKERFILE.read_text())
    header = umr.TARGET.read_text().splitlines()[0]
    assert header.endswith(f" at {ref}."), f"{header!r}: run mise run misp-requirements for {ref}"
