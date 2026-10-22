"""The merge of master into misp-3.0 (scripts/merge_into_misp3.py), on real git repositories."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_into_misp3 as sync  # noqa: E402

DOCKERFILE = """# syntax=docker/dockerfile:1
ARG CORE_TAG=v2.5.50
ARG CORE_COMMIT
ARG PHP_VER=20240924

RUN composer install --no-interaction
"""
GITATTRIBUTES = "files/composer.lock merge=ours\nfiles/misp-config/settings-upstream.yaml merge=ours\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def write(repo, files):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "change")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """master at 2.5, and misp-3.0 with its pins, its lock, its catalogue and its .gitattributes."""
    # No global or system git config: a signing setting there would fail every commit
    for key, value in {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}.items():
        monkeypatch.setenv(key, value)
    git(tmp_path, "init", "--quiet", "--initial-branch=master")
    write(tmp_path, {"Dockerfile": DOCKERFILE, "files/composer.lock": "lock 2.5.50\n",
                     "files/misp-config/settings-upstream.yaml": "catalogue 2.5.50\n", "README.md": "readme\n"})
    git(tmp_path, "checkout", "--quiet", "-b", "misp-3.0")
    write(tmp_path, {"Dockerfile": DOCKERFILE.replace(
        "ARG CORE_TAG=v2.5.50\nARG CORE_COMMIT\n",
        "# MISP 3.0 Early Access: the 3.0_wip branch\nARG CORE_TAG=3.0_wip\nARG CORE_COMMIT=8888b98\n"),
        "files/composer.lock": "lock 3.0\n", "files/misp-config/settings-upstream.yaml": "catalogue 3.0\n",
        ".gitattributes": GITATTRIBUTES})
    git(tmp_path, "checkout", "--quiet", "master")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def merge_master(repo):
    git(repo, "checkout", "--quiet", "misp-3.0")
    return sync.merge("master")


def test_a_25_bump_keeps_the_30_pins_lock_and_catalogue_and_takes_the_fix(repo):
    write(repo, {"Dockerfile": DOCKERFILE.replace("v2.5.50", "v2.5.51").replace(
                     "--no-interaction\n", "--no-interaction --no-dev\n"),
                 "files/composer.lock": "lock 2.5.51\n", "files/misp-config/settings-upstream.yaml": "catalogue 2.5.51\n"})
    assert merge_master(repo) == 0
    dockerfile = (repo / "Dockerfile").read_text()
    assert "ARG CORE_TAG=3.0_wip\nARG CORE_COMMIT=8888b98\n" in dockerfile and "--no-dev" in dockerfile
    assert (repo / "files/composer.lock").read_text() == "lock 3.0\n"
    assert (repo / "files/misp-config/settings-upstream.yaml").read_text() == "catalogue 3.0\n"
    assert git(repo, "log", "-1", "--format=%P").count(" ") == 1


def test_a_clean_fix_merges(repo):
    write(repo, {"README.md": "readme with a fix\n"})
    assert merge_master(repo) == 0
    assert (repo / "README.md").read_text() == "readme with a fix\n"


def test_a_conflict_beyond_the_pins_aborts_and_leaves_the_branch(repo):
    git(repo, "checkout", "--quiet", "misp-3.0")
    write(repo, {"README.md": "readme on 3.0\n"})
    git(repo, "checkout", "--quiet", "master")
    write(repo, {"README.md": "readme on 2.5\n"})
    head = git(repo, "rev-parse", "misp-3.0")
    assert merge_master(repo) == sync.EXIT_CONFLICT
    assert git(repo, "rev-parse", "HEAD") == head and not git(repo, "status", "--porcelain")


def test_a_branch_with_every_commit_has_nothing_to_merge(repo):
    write(repo, {"README.md": "readme with a fix\n"})
    assert merge_master(repo) == 0
    assert sync.merge("master") == sync.EXIT_NOTHING


class TestResolvePinConflicts:
    def test_pin_lines_take_the_branch_side(self):
        text = "a\n<<<<<<< HEAD\nARG CORE_TAG=3.0_wip\n=======\nARG CORE_TAG=v2.5.51\n>>>>>>> master\nb\n"
        assert sync.resolve_pin_conflicts(text) == "a\nARG CORE_TAG=3.0_wip\nb\n"

    def test_any_other_line_in_a_conflict_refuses(self):
        text = "<<<<<<< HEAD\nARG CORE_TAG=3.0_wip\n=======\nARG CORE_TAG=v2.5.51\nRUN true\n>>>>>>> master\n"
        assert sync.resolve_pin_conflicts(text) is None
