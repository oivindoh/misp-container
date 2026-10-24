"""The patches of patches/ and their applier (scripts/apply_patches.py), on real git trees."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import apply_patches  # noqa: E402

ORIGINAL = "<?php\n$alias = $model->alias;\n$other = 1;\n"
PATCHED = "<?php\n$alias = $model->getDataSource()->name($model->alias);\n$other = 1;\n"


def misp_tree(tmp_path, text=ORIGINAL):
    root = tmp_path / "misp"
    (root / "app/Model").mkdir(parents=True)
    (root / "app/Model/Stats.php").write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def patch_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for side, text in ((a, ORIGINAL), (b, PATCHED)):
        (side / "app/Model").mkdir(parents=True)
        (side / "app/Model/Stats.php").write_text(text)
    diff = subprocess.run(["git", "diff", "--no-index", "--no-prefix", "a/app/Model/Stats.php", "b/app/Model/Stats.php"],
                          cwd=tmp_path, capture_output=True, text=True).stdout
    path = tmp_path / "patches" / "01-quote.patch"
    path.parent.mkdir()
    path.write_text("Quote the alias\n\nWhy: PostgreSQL folds it.\nUpstream: MISP#1\n\n" + diff)
    return path


def test_a_patch_that_fits_is_applied(tmp_path):
    misp, patch = misp_tree(tmp_path), patch_file(tmp_path)
    assert apply_patches.apply_all(misp, [patch]) == []
    assert (misp / "app/Model/Stats.php").read_text() == PATCHED


def test_a_change_misp_already_carries_stops_the_run(tmp_path):
    misp, patch = misp_tree(tmp_path, PATCHED), patch_file(tmp_path)
    assert apply_patches.apply_all(misp, [patch]) == ["01-quote.patch: MISP already carries this change; delete the patch"]


def test_a_patch_that_no_longer_fits_names_the_file_and_the_upstream_issue(tmp_path):
    misp, patch = misp_tree(tmp_path, "<?php\n$alias = quote($model->alias);\n$other = 1;\n"), patch_file(tmp_path)
    [problem] = apply_patches.apply_all(misp, [patch])
    assert problem.startswith("01-quote.patch: no longer applies to app/Model/Stats.php")
    assert "MISP#1" in problem
    assert (misp / "app/Model/Stats.php").read_text() == "<?php\n$alias = quote($model->alias);\n$other = 1;\n"


def test_the_header_carries_a_title_and_fields(tmp_path):
    assert apply_patches.header(patch_file(tmp_path)) == {"title": "Quote the alias", "why": "PostgreSQL folds it.",
                                                          "upstream": "MISP#1"}


def test_every_patch_of_the_repository_has_a_reason_and_an_upstream_line():
    patches = sorted(apply_patches.PATCHES.glob("*.patch"))
    assert patches
    for patch in patches:
        fields = apply_patches.header(patch)
        assert fields.get("title") and fields.get("why") and fields.get("upstream"), patch.name
        assert apply_patches.touched(patch), patch.name
