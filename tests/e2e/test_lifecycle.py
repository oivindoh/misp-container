"""Lifecycle suite: the chart from its install to a rollback, on k3s.

    pytest tests/e2e/test_lifecycle.py [--skip-build] [--keep]

The release starts as the newest published chart of this major version (from
GHCR, with its own images) and upgrades to this tree's chart and images; without
such a chart, it installs this tree. This tree runs with the network policies on.
Then the chart's helm test runs, two tasks and a housekeeping run start from their
CronJobs, an upgrade changes a value, and a rollback returns to the revision before
it. The smoke test (tests/e2e/test_smoke.py) runs after each install, upgrade and
rollback.
"""

import json
import os
import subprocess
import sys

import pytest
import yaml

import manifests
from cluster import CHART, REPO, Cluster, build_images
from stack import Release

PORTS = {28080: 30080}
# The chart of every release
REGISTRY_CHART = "oci://ghcr.io/oivindoh/charts/misp"
ADMIN_KEY = "lifecycleKEY0123456789abcdefghijklmnopqr"
# On top of tests/k3s/values.yaml for this tree's chart: the CronJobs, the network policies,
# and the admin key the smoke test and the helm test call the API with
VALUES = {"cronjobs": {"enabled": True}, "housekeeping": {"enabled": True}, "networkPolicy": {"enabled": True},
          "secrets": {"admin": {"ADMIN_KEY": ADMIN_KEY}}}
# The suite reaches web through a NodePort from the host, from no namespace the chart's
# policies name: the policies add up, so this one lets it in
NODEPORT_POLICY = {
    "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "suite-nodeport"},
    "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "web"}}, "policyTypes": ["Ingress"],
             "ingress": [{"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                          "ports": [{"port": 8080, "protocol": "TCP"}]}]}}


def newest_release() -> str:
    """The newest published chart of this tree's major version; "" while there is none.

    A tree's own chart reaches GHCR only after its suites pass, so the newest one is
    always an earlier release. Every published chart's images carry amd64 and arm64.
    """
    major = yaml.safe_load((CHART / "Chart.yaml").read_text())["version"].split(".")[0]
    shown = subprocess.run(["helm", "show", "chart", REGISTRY_CHART, "--version", f"^{major}.0.0-0"],
                           capture_output=True, text=True, timeout=120)
    if shown.returncode != 0:
        if "could not locate a version" in shown.stderr:
            return ""
        pytest.fail(f"cannot read the published charts of {REGISTRY_CHART}: {shown.stderr}")
    return yaml.safe_load(shown.stdout)["version"]


def smoke(release):
    """tests/e2e/test_smoke.py against the release: its exit code and output."""
    result = subprocess.run([sys.executable, "-m", "pytest", str(REPO / "tests/e2e/test_smoke.py"), "-q",
                             "-p", "no:cacheprovider", "--url", release.base_url, "--key", ADMIN_KEY],
                            capture_output=True, text=True, timeout=600, env={**os.environ})
    return result.returncode, result.stdout + result.stderr


def web_pods(release):
    return sorted(release.pods("web"))


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    build_images(options)
    cluster = Cluster("misp-lifecycle", PORTS)
    cluster.start()
    cluster.load_images_under_test()
    s = Release(cluster)
    s.apply(manifests.nodeport("web", "web", {8080: 30080}), NODEPORT_POLICY)
    yield s
    cluster.finish("lifecycle", options["keep"], failed_in_module())


@pytest.fixture(scope="module")
def published(stack):
    """The newest release, installed from GHCR: its version and the smoke test's result, else why not."""
    version = newest_release()
    if not version:
        return {"skip": "no chart of this major version in GHCR yet: the suite starts from this tree"}
    stack.install({"secrets": {"admin": {"ADMIN_KEY": ADMIN_KEY}}}, chart=REGISTRY_CHART, version=version, tag="")
    stack.wait_for_misp()
    return {"version": version, "smoke": smoke(stack)}


@pytest.fixture(scope="module")
def installed(stack, published):
    """This tree's chart and images: an upgrade of the published release, or a first install."""
    stack.install(VALUES)
    stack.wait_for_misp()
    return smoke(stack)


@pytest.fixture(scope="module")
def upgraded(stack, installed):
    """An upgrade that changes a value: a new configure Job, the old one gone, the pods rolled."""
    before = {"revision": stack.revision, "pods": web_pods(stack)}
    stack.install({"env": {"PHP_MAX_FILE_UPLOADS": "51"}})
    stack.wait_for_misp()
    return {**before, "smoke": smoke(stack)}


@pytest.fixture(scope="module")
def rolled_back(stack, upgraded):
    """A rollback to the revision before the upgrade: its configure Job runs again, the value goes back."""
    revision, pods = stack.revision, web_pods(stack)
    stack.rollback(upgraded["revision"])
    stack.wait_for_misp()
    return {"revision": revision, "pods": pods, "smoke": smoke(stack)}


def helm_test(stack):
    result = subprocess.run(["helm", "test", "misp", "-n", stack.namespace, "--logs", "--timeout", "5m"],
                            capture_output=True, text=True, env=stack.cluster.env(), timeout=400)
    return result.returncode, result.stdout + result.stderr


def job_from_cronjob(stack, cronjob):
    stack.kubectl("create", "job", f"--from=cronjob/{cronjob}", f"{cronjob}-test")
    stack.wait_for_job(f"{cronjob}-test", 300)
    return stack.kubectl("logs", f"job/{cronjob}-test").stdout


# -- from the newest release -------------------------------------------------------

def test_the_newest_release_serves(published):
    if "skip" in published:
        pytest.skip(published["skip"])
    rc, out = published["smoke"]
    assert rc == 0, f"v{published['version']}:\n{out[-3000:]}"


def test_this_tree_serves(installed):
    rc, out = installed
    assert rc == 0, out[-3000:]


def test_helm_test_passes(stack, installed):
    rc, out = helm_test(stack)
    assert rc == 0, out[-3000:]


# -- periodic work from the CronJobs -----------------------------------------------

@pytest.mark.parametrize("cronjob", ["update-noticelists", "periodic-summary", "housekeeping-jobs"])
def test_a_cronjob_runs(stack, installed, cronjob):
    job_from_cronjob(stack, cronjob)


def test_configure_writes_json_lines(stack, installed):
    # The chart's default is LOG_FORMAT=json: every line of the configure Job is one JSON object
    lines = [line for line in stack.logs("configure").splitlines() if line.strip()]
    assert len(lines) >= 10
    for line in lines:
        json.loads(line)


# -- upgrade and rollback ----------------------------------------------------------

def test_the_upgrade_serves(upgraded):
    rc, out = upgraded["smoke"]
    assert rc == 0, out[-3000:]


def test_the_upgrade_removed_the_old_configure_job(stack, upgraded):
    assert stack.kubectl("get", "job", f"configure-{upgraded['revision']}").returncode != 0


def test_the_upgrade_rolled_the_web_pods(stack, upgraded):
    assert web_pods(stack) != upgraded["pods"]


def test_the_rollback_serves(rolled_back):
    rc, out = rolled_back["smoke"]
    assert rc == 0, out[-3000:]


def test_the_rollback_removed_the_upgrades_configure_job(stack, rolled_back):
    assert stack.kubectl("get", "job", f"configure-{rolled_back['revision']}").returncode != 0


def test_the_rollback_restored_the_value(stack, rolled_back):
    value = stack.kubectl("get", "configmap", "misp-env", "-o", "jsonpath={.data.PHP_MAX_FILE_UPLOADS}").stdout
    assert value != "51"


def test_the_rollback_rolled_the_web_pods(stack, rolled_back):
    assert web_pods(stack) != rolled_back["pods"]


def test_helm_test_passes_after_the_rollback(stack, rolled_back):
    rc, out = helm_test(stack)
    assert rc == 0, out[-3000:]
