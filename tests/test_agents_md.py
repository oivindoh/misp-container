"""AGENTS.md is what scripts/generate_agents_md.py makes of the tree."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import generate_agents_md  # noqa: E402


def test_agents_md_matches_the_tree():
    assert generate_agents_md.OUT.read_text() == generate_agents_md.generate(), (
        "AGENTS.md differs from the tree: run `mise run agents-md` and commit the result")


def test_every_line_is_ascii():
    assert generate_agents_md.generate().isascii()
