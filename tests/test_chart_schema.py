"""helm lint, and every render of the chart validated against the Kubernetes and CRD schemas.

The renders: the default values, each component on, every component on, and the
values a release changes most. kubeconform validates each against the Kubernetes
schemas and, for the Cilium policies and the HTTPRoute, the datree CRD catalog
(a schema cache under the user's cache directory). Needs helm and kubeconform
on PATH (mise install); without kubeconform the tests skip, except in CI, where
they fail.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import chart  # noqa: E402

CRD_SCHEMAS = "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"


def renders() -> dict[str, list[str]]:
    """{name: --set values}: what the chart must render and validate."""
    parts = chart.components()
    cases = {"default": [], **{c: [f"{c}.enabled=true"] for c in parts},
             "all": [f"{c}.enabled=true" for c in parts],
             "secrets-supplied": ["secrets.create=false"],
             "database-secret": [f"{c}.enabled=true" for c in parts] + [
                 "database.user.name=pg-app", "database.user.key=username",
                 "database.password.name=pg-app", "database.password.key=password"],
             "attachments-in-s3": ["attachments.claim=false", "env.PLUGIN_S3_BUCKET_NAME=misp"],
             "workflow-cronjob": ["cronjobs.enabled=true", "cronjobs.workflows.7=0 4 * * *"]}
    return cases


def kubeconform() -> str:
    path = shutil.which("kubeconform")
    if not path:
        if os.environ.get("CI"):
            pytest.fail("kubeconform is not on PATH; the install-helm action installs it")
        pytest.skip("kubeconform is not on PATH (mise install)")
    return path


def test_lint_strict():
    if not shutil.which("helm"):
        pytest.skip("helm is not on PATH (mise install)")
    result = subprocess.run(["helm", "lint", "--strict", str(chart.CHART), "--namespace", "misp"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", list(renders()))
def test_render_validates_against_the_schemas(name):
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kubeconform"
    cache.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [kubeconform(), "-strict", "-summary", "-cache", str(cache),
         "-schema-location", "default", "-schema-location", CRD_SCHEMAS],
        input=chart.render_text(*renders()[name]), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"render {name}:\n{result.stdout}{result.stderr}"
