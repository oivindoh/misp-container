"""helm lint, and every render of the chart validated against the Kubernetes and CRD schemas.

The renders: the default values, each component on, every component on, and the
values a release changes most. kubeconform validates each against the Kubernetes
schemas of the version the stack suites run (K3S_IMAGE), then, for a kind they lack,
the HTTPRoute, against the datree CRD catalog at a pinned commit. A schema it finds
downloads once into a cache under the user's cache directory; a render with the
HTTPRoute asks the Kubernetes schemas for it on every run, so it needs the network.
Pin a newer commit when the chart moves to another API version of the HTTPRoute.
Needs helm and kubeconform on PATH (mise install); without kubeconform the tests skip,
except in CI, where they fail.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import chart  # noqa: E402

CRD_SCHEMAS = ("https://raw.githubusercontent.com/datreeio/CRDs-catalog/041baf4c1d3740ca21461cab765e7432ecd073ef"
               "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")
# The Kubernetes version of the suites' k3s
KUBERNETES = re.search(r"rancher/k3s:v(\d+\.\d+\.\d+)", (REPO / "tests/e2e/cluster.py").read_text()).group(1)


def renders() -> dict[str, list[str]]:
    """{name: --set values}: what the chart must render and validate."""
    parts = chart.components()
    every = [v for c in parts for v in chart.component_sets(c)]
    cases = {"default": [], **{c: chart.component_sets(c) for c in parts},
             "all": every,
             "secrets-supplied": ["secrets.create=false"],
             "connection-secrets": every + [
                 "connectionSecrets[0].name=pg-app", "connectionSecrets[0].keys.DB_HOST=host",
                 "connectionSecrets[0].keys.DB_USER=user", "connectionSecrets[0].keys.DB_PASSWORD=password",
                 "databaseCA.name=pg-cluster-cert", "databaseCA.key=ca.crt"],
             "external-database-and-redis": ["networkPolicy.enabled=true", "cronjobs.enabled=true",
                                             "housekeeping.enabled=true"],
             "attachments-in-s3": ["attachments.claim=false", "env.PLUGIN_S3_BUCKET_NAME=misp"],
             "workflow-cronjob": ["cronjobs.enabled=true", "cronjobs.workflows.7=0 4 * * *"]}
    return cases


def kubeconform() -> str:
    path = shutil.which("kubeconform")
    if not path:
        if os.environ.get("CI"):
            pytest.fail("kubeconform is not on PATH: mise install installs it (mise.toml)")
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
        [kubeconform(), "-strict", "-summary", "-cache", str(cache), "-kubernetes-version", KUBERNETES,
         # The Kubernetes schemas first: the catalog answers only the kinds they lack
         "-schema-location", "default", "-schema-location", CRD_SCHEMAS],
        input=chart.render_text(*renders()[name]), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"render {name}:\n{result.stdout}{result.stderr}"
