"""Lifecycle suite: the chart from its install to a rollback, on k3s.

    pytest tests/e2e/test_lifecycle.py [--skip-build] [--keep]

The release starts as the newest published release of this major version
(chart from GHCR, its own images) and upgrades to this tree's chart and images;
without such a release, or without its images for the node's architecture, it
installs this tree. Then the chart's helm test runs,
two tasks and a housekeeping run start from their CronJobs, an upgrade changes
a value, and a rollback returns to the revision before it. The smoke test
(tests/e2e/test_smoke.py) runs after each install, upgrade and rollback.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request

import pytest
import yaml

import manifests
from cluster import CHART, IMAGES, REPO, Cluster, build_images
from stack import Release

PORTS = {28080: 30080}
# The chart of every release; a release vX.Y.Z publishes chart version X.Y.Z
REGISTRY_CHART = "oci://ghcr.io/oivindoh/charts/misp"
ADMIN_KEY = "lifecycleKEY0123456789abcdefghijklmnopqr"
# On top of tests/k3s/values.yaml for this tree's chart: the CronJobs, and the admin key the
# smoke test and the helm test call the API with
VALUES = {"cronjobs": {"enabled": True}, "housekeeping": {"enabled": True},
          "secrets": {"admin": {"ADMIN_KEY": ADMIN_KEY}}}


def version_key(version: str) -> tuple:
    """A SemVer version as a sort key: a pre-release sorts before its release."""
    core, _, pre = version.partition("-")
    numbers = tuple(int(n) for n in core.split("."))
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return numbers, not pre, parts


def newest_release() -> str:
    """The version of the newest release tag of this tree's major version, other than HEAD's; "" for none."""
    major = yaml.safe_load((CHART / "Chart.yaml").read_text())["version"].split(".")[0]
    listing = subprocess.run(["git", "ls-remote", "--tags", "origin", f"v{major}.*"], cwd=REPO,
                             capture_output=True, text=True, timeout=60)
    if listing.returncode != 0:
        pytest.fail(f"cannot list the release tags: {listing.stderr}")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    commits = {}
    for line in listing.stdout.splitlines():
        sha, ref = line.split("\t")
        tag = ref.removeprefix("refs/tags/").removesuffix("^{}")
        # An annotated tag lists its own object, then the commit it points at (^{})
        if ref.endswith("^{}") or tag not in commits:
            commits[tag] = sha
    versions = [tag[1:] for tag, sha in commits.items() if sha != head and re.fullmatch(r"v[0-9.]+(-[\w.]+)?", tag)]
    return max(versions, key=version_key, default="")


def architectures(image: str, tag: str) -> set[str]:
    """The Linux architectures of a public image in GHCR, from its image index."""
    repository = image.removeprefix("ghcr.io/")
    with urllib.request.urlopen(f"https://ghcr.io/token?scope=repository:{repository}:pull", timeout=30) as r:
        token = json.load(r)["token"]
    request = urllib.request.Request(f"https://ghcr.io/v2/{repository}/manifests/{tag}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"})
    with urllib.request.urlopen(request, timeout=30) as r:
        manifest = json.load(r)
    if "manifests" not in manifest:
        pytest.fail(f"{image}:{tag} is no image index: the publish job of CI pushes one per image")
    return {m["platform"]["architecture"] for m in manifest["manifests"] if m.get("platform", {}).get("os") == "linux"}


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
    s.apply(manifests.nodeport("web", "web", {8080: 30080}))
    yield s
    cluster.finish("lifecycle", options["keep"], failed_in_module())


@pytest.fixture(scope="module")
def published(stack):
    """The newest release, installed from GHCR: its version and the smoke test's result, else why not."""
    version = newest_release()
    if not version:
        return {"skip": "no release of this major version yet: the suite starts from this tree"}
    chart = subprocess.run(["helm", "show", "chart", REGISTRY_CHART, "--version", version],
                           capture_output=True, text=True, timeout=120)
    if chart.returncode != 0:
        pytest.fail(f"release v{version} has no chart {version} in GHCR: {chart.stderr}")
    node = stack.kubectl("get", "nodes", "-o", "jsonpath={.items[0].status.nodeInfo.architecture}").stdout.strip()
    for image in IMAGES:
        published_for = architectures(image, version)
        if node not in published_for:
            return {"skip": f"v{version} has no {node} image of {image} ({', '.join(sorted(published_for))} only): "
                            "the suite starts from this tree"}
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
