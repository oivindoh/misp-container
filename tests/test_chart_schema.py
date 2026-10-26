"""helm lint, and every render of the chart validated against the Kubernetes and CRD schemas.

The renders: the default values, each component on, every component on, and the
values a release changes most. kubeconform validates each against the Kubernetes
schemas and, for the HTTPRoute, the datree CRD catalog at a pinned commit; both
download once into a cache under the user's cache directory. Pin a newer commit when
the chart moves to another API version of the HTTPRoute. Needs helm and kubeconform
on PATH (mise install); without kubeconform the tests skip, except in CI, where they
fail.
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

CRD_SCHEMAS = ("https://raw.githubusercontent.com/datreeio/CRDs-catalog/041baf4c1d3740ca21461cab765e7432ecd073ef"
               "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json")


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
         # The local copies first: a kind they do not hold falls through to the default
         "-schema-location", CRD_SCHEMAS, "-schema-location", "default"],
        input=chart.render_text(*renders()[name]), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"render {name}:\n{result.stdout}{result.stderr}"
