"""Unit tests for the release tool (scripts/release.py): the next tag and chart version."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import release  # noqa: E402


class TestNextTag:
    def test_the_first_release_of_a_misp_version_is_the_version(self):
        assert release.next_tag("v2.5.49", ["v2.5.37", "v2.5.48", "v2.5.48-r1"]) == "v2.5.49"

    def test_a_second_release_is_revision_one(self):
        assert release.next_tag("v2.5.48", ["v2.5.48"]) == "v2.5.48-r1"

    def test_revisions_count_as_numbers(self):
        tags = ["v2.5.48", *(f"v2.5.48-r{n}" for n in range(1, 11))]
        assert release.next_tag("v2.5.48", tags) == "v2.5.48-r11"

    def test_another_misp_version_with_the_same_prefix_is_not_counted(self):
        assert release.next_tag("v2.5.4", ["v2.5.48", "v2.5.48-r1"]) == "v2.5.4"


class TestNextWipTag:
    def test_the_first_pre_release(self):
        assert release.next_wip_tag("3.0.0", ["v2.5.50", "v2.5.50-r1"]) == "v3.0.0-wip.1"

    def test_pre_releases_count_as_numbers(self):
        tags = [f"v3.0.0-wip.{n}" for n in range(1, 11)]
        assert release.next_wip_tag("3.0.0", tags) == "v3.0.0-wip.11"

    def test_a_work_in_progress_pin_names_its_version(self):
        assert release.wip_version("3.0_wip") == "3.0.0"
        assert release.wip_version("v3.0.0") is None


class TestLatestRelease:
    def test_the_newest_by_version_then_revision(self):
        tags = ["v2.5.48-r10", "v2.5.48-r9", "v2.5.37", "v2.5.48"]
        assert release.latest_release(tags) == "v2.5.48-r10"

    def test_a_pre_release_sorts_after_older_versions_and_before_its_own(self):
        assert release.latest_release(["v2.5.50-r3", "v3.0.0-wip.2", "v3.0.0-wip.10"]) == "v3.0.0-wip.10"
        assert release.latest_release(["v3.0.0-wip.10", "v3.0.0"]) == "v3.0.0"

    def test_other_tags_do_not_count(self):
        assert release.latest_release(["v2.5.37", "vtest", "v3"]) == "v2.5.37"
        assert release.latest_release([]) is None


class TestNextChartVersion:
    @pytest.mark.parametrize("kind, expected", [("hotfix", "1.4.3"), ("normal", "1.5.0"), ("breaking", "2.0.0")])
    def test_each_kind_raises_its_part(self, kind, expected):
        assert release.next_chart_version("1.4.2", "1.4.2", kind) == expected

    def test_the_first_release_with_a_chart_publishes_chart_yaml(self):
        assert release.next_chart_version(None, "1.0.0", "normal") == "1.0.0"

    def test_the_first_release_after_the_pre_releases_drops_the_suffix(self):
        assert release.next_chart_version("3.0.0-wip.7", "3.0.0-wip.7", "normal") == "3.0.0"


def test_set_chart_changes_the_two_versions_only():
    text = '# comment\nversion: 1.0.0\n# about the app\nappVersion: "2.5.37"\nname: misp\n'
    assert release.set_chart(text, "1.1.0", "2.5.48") == \
        '# comment\nversion: 1.1.0\n# about the app\nappVersion: "2.5.48"\nname: misp\n'


def test_set_image_default_changes_every_image_of_ours():
    text = ("image: ghcr.io/oivindoh/misp-container:${MISP_IMAGE_TAG:-2.5.37}\n"
            "image: ghcr.io/oivindoh/misp-container-caddy:${MISP_IMAGE_TAG:-2.5.37}\n"
            "image: docker.io/library/redis:8\n")
    assert release.set_image_default(text, "2.5.48-r1") == (
        "image: ghcr.io/oivindoh/misp-container:${MISP_IMAGE_TAG:-2.5.48-r1}\n"
        "image: ghcr.io/oivindoh/misp-container-caddy:${MISP_IMAGE_TAG:-2.5.48-r1}\n"
        "image: docker.io/library/redis:8\n")


def test_the_image_tag_sits_in_every_compose_file_of_ours():
    # A compose file with our image but no MISP_IMAGE_TAG default would keep an old tag
    for path in release.COMPOSE_FILES:
        text = path.read_text()
        if "ghcr.io/oivindoh/misp-container" in text:
            assert release.IMAGE_DEFAULT.search(text), path
