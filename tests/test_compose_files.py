"""The test overlays start a service with the same settings under podman-compose and docker compose.

podman-compose appends an overlay's env_file list to the list it overrides;
docker compose merges the two and drops repeats. When the overlay's list
starts with the list it overrides, both runners load the same files in the
same order. The integration suite's overlays all stack on
tests/docker-compose.test.yml, so the other overlays repeat its list too.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "deploy" / "docker-compose.yml"
TEST = REPO / "tests" / "docker-compose.test.yml"
# A separate project with its own services; it overrides nothing
STANDALONE = {"docker-compose.sync-test.yml"}
OVERLAYS = sorted(p for p in (REPO / "tests").glob("docker-compose.*.yml") if p.name not in STANDALONE)


def env_files(path: Path) -> dict[str, list[str]]:
    services = yaml.safe_load(path.read_text()).get("services") or {}
    return {name: list(spec["env_file"]) for name, spec in services.items()
            if isinstance(spec, dict) and spec.get("env_file")}


def rel(path: Path) -> str:
    return str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)


def broken_prefixes(overlay: Path) -> list[str]:
    """One line per service whose env_file list does not repeat the list it overrides."""
    layers = [BASE] if overlay == TEST else [BASE, TEST]
    problems = []
    for service, files in env_files(overlay).items():
        for layer in layers:
            below = env_files(layer).get(service)
            if below and files[:len(below)] != below:
                problems.append(
                    f"{rel(overlay)}: service {service} lists env files {files}, which do not start with "
                    f"{below} from {rel(layer)}. The runners merge the lists differently, so podman-compose "
                    f"and docker compose would start {service} with different settings. Repeat the list, "
                    f"then add the test files.")
    return problems


@pytest.mark.parametrize("overlay", OVERLAYS, ids=[p.name for p in OVERLAYS])
def test_overlay_repeats_the_env_files_it_overrides(overlay):
    problems = broken_prefixes(overlay)
    assert not problems, "\n".join(problems)


def test_the_check_finds_a_list_that_drops_a_file(tmp_path):
    overlay = tmp_path / "docker-compose.broken.yml"
    overlay.write_text("services:\n  web:\n    env_file:\n      - base/base.env\n      - ../tests/test-compose.env\n")
    problems = broken_prefixes(overlay)
    assert problems and "service web" in problems[0] and "deploy/docker-compose.yml" in problems[0]


def test_every_overlay_is_checked():
    # A new overlay that is not an override of the base compose file belongs in STANDALONE
    assert {p.name for p in OVERLAYS} >= {"docker-compose.test.yml", "docker-compose.postgres.yml",
                                          "docker-compose.migrate.yml", "docker-compose.migrate-mysql.yml"}
