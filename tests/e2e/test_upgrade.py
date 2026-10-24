"""Upgrade suite: seeds MISP 2.5 from the published 2.5.50 images, converts its
database to MISP 3.0 with the upgrade step of this tree's images, and checks the
data through the API and the database.

    pytest tests/e2e/test_upgrade.py [--skip-build] [--keep]

The 3.0 images run under MISP_IMAGE_TAG: CI loads them as ci, and a local run
builds them as upgrade-test. The tests run in file order and share one stack;
module fixtures carry each step's state to the next.
"""

import base64
import os
import subprocess
import time

import pytest

from stack import DEPLOY, TESTS, Stack, env_file

# The newest 2.5 release: what an operator upgrades from
SOURCE_TAG = "2.5.50"
# The 2.5.50 images take MySQL unless DB_ENGINE says otherwise
SOURCE_ENV = TESTS / "upgrade-source.env"
TARGET_TAG = os.environ.get("MISP_IMAGE_TAG") or "upgrade-test"
IMAGES = ("ghcr.io/oivindoh/misp-container", "ghcr.io/oivindoh/misp-container-caddy",
          "ghcr.io/oivindoh/misp-container-modules")
ADMIN_EMAIL = env_file(TESTS / "test-compose.env")["ADMIN_EMAIL"]
MISP_SERVICES = ("configure", "web", "worker", "caddy", "modules", "metrics")
SHARED_IP = "198.51.100.7"
ATTACHMENT = b"upgrade suite attachment"
# (type, category, value) of event A; text keeps a non-ASCII value through the value table
VALUES = (("ip-src", "Network activity", SHARED_IP),
          ("domain", "Network activity", "upgrade-suite.example"),
          ("ip-dst|port", "Network activity", "203.0.113.5|443"),
          ("md5", "Payload delivery", "d41d8cd98f00b204e9800998ecf8427e"),
          ("text", "Other", "Grüße aus der Prüfung"))


def use(tag, env=None):
    """Compose reads the image tag and the extra env file of every MISP service from these on each call."""
    os.environ["MISP_IMAGE_TAG"] = tag
    if env:
        os.environ["MISP_TEST_ENV"] = str(env)
    else:
        os.environ.pop("MISP_TEST_ENV", None)


def add_event(stack, info):
    event = stack.api("POST", "/events/add", stack.key, {
        "info": info, "distribution": "0", "threat_level_id": "4", "analysis": "0"}) or {}
    return event["Event"]["id"]


def add_attribute(stack, event_id, **fields):
    attribute = stack.api("POST", f"/attributes/add/{event_id}", stack.key, {"event_id": event_id, **fields}) or {}
    return attribute["Attribute"]["id"]


def related(stack, event_id):
    event = stack.api("GET", f"/events/view/{event_id}", stack.key) or {}
    return {r["Event"]["id"] for r in event.get("Event", {}).get("RelatedEvent", [])}


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    s = Stack([DEPLOY / "docker-compose.yml", TESTS / "docker-compose.test.yml"], profiles=("upgrade",))
    previous = {k: os.environ.get(k) for k in ("MISP_IMAGE_TAG", "MISP_TEST_ENV")}
    s.compose("down", "-v")
    if not options["skip_build"]:
        use(TARGET_TAG)
        s.compose("build", check=True, timeout=3600)
    # The engine pulls: a compose runner may skip a service it can build. The
    # releases are built for amd64 alone; another host runs them emulated
    for image in IMAGES:
        pulled = subprocess.run([s.engine, "pull", "--platform", "linux/amd64", f"{image}:{SOURCE_TAG}"],
                                capture_output=True, text=True)
        assert pulled.returncode == 0, pulled.stderr
    use(SOURCE_TAG, SOURCE_ENV)
    s.compose("up", "-d", profiles=())
    s.wait_for_misp(timeout=600)
    s.key = s.admin_key(ADMIN_EMAIL)
    yield s
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    s.finish("upgrade", options["keep"], failed_in_module())


@pytest.fixture(scope="module")
def seed(stack):
    a, b = add_event(stack, "Upgrade suite event A"), add_event(stack, "Upgrade suite event B")
    values = {add_attribute(stack, a, type=t, category=c, value=v, to_ids=False): (t, v) for t, c, v in VALUES}
    add_attribute(stack, b, type="ip-src", category="Network activity", value=SHARED_IP, to_ids=False)
    attachment = add_attribute(stack, a, type="attachment", category="Payload delivery", value="upgrade.txt",
                               data=base64.b64encode(ATTACHMENT).decode())
    shared = next(i for i, (t, v) in values.items() if v == SHARED_IP)
    stack.api("POST", "/sightings/add", stack.key, {"id": shared})
    return {"a": a, "b": b, "values": values, "attachment": attachment,
            "sightings": stack.sql("SELECT COUNT(*) FROM sightings")}


@pytest.fixture(scope="module")
def stopped(stack, seed):
    # Nothing of 2.5 may write while the database converts
    stack.compose("stop", "web", "worker", profiles=())
    use(TARGET_TAG)
    return stack


@pytest.fixture(scope="module")
def upgraded(stopped):
    return stopped.run("upgrade", env={"UPGRADE_BACKUP_CONFIRMED": "true"}, timeout=1800)


@pytest.fixture(scope="module")
def on_30(stack, upgraded):
    # New containers, through the engine: podman-compose's --force-recreate also
    # recreates Redis, which loses the recorrelation job the conversion queued
    for service in MISP_SERVICES:
        ids = stack.container_ids(service, stopped=True)
        if ids:
            subprocess.run([stack.engine, "rm", "-f", *ids], capture_output=True)
    stack.compose("up", "-d", *MISP_SERVICES, profiles=())
    stack.wait_for_misp(timeout=600)
    return stack


def has_text_values(stack):
    # Plain SQL: the 2.5.50 image has no db.has_column
    return stack.sql("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = "
                     "current_schema() AND table_name = 'attributes' AND column_name = 'value1'") == "1"


# -- MISP 2.5 ------------------------------------------------------------------------

def test_the_25_instance_correlates_its_events(stack, seed):
    assert seed["b"] in related(stack, seed["a"])


def test_the_25_database_holds_values_as_text(stack, seed):
    assert has_text_values(stack)


# -- the conversion ------------------------------------------------------------------

def test_a_30_configure_refuses_the_25_database(stopped):
    rc, out = stopped.run("configure")
    assert rc != 0 and "MISP 2.5's layout" in out, out[-2000:]


def test_the_upgrade_needs_a_confirmed_backup(stopped):
    rc, out = stopped.run("upgrade")
    assert rc == 2 and "UPGRADE_BACKUP_CONFIRMED" in out, out[-2000:]


def test_the_upgrade_converts_the_database(upgraded):
    rc, out = upgraded
    assert rc == 0 and "database converted to MISP 3.0" in out, out[-4000:]


def test_a_second_upgrade_converts_nothing(stopped, upgraded):
    rc, out = stopped.run("upgrade", env={"UPGRADE_BACKUP_CONFIRMED": "true"})
    assert rc == 0 and "needs no conversion" in out, out[-2000:]


# -- MISP 3.0 ------------------------------------------------------------------------

def test_misp_30_serves_the_converted_database(on_30):
    version = (on_30.api("GET", "/servers/getVersion.json", on_30.key) or {}).get("version", "")
    assert version.startswith("3."), version


def test_the_text_values_are_gone(on_30):
    assert not has_text_values(on_30)


def test_every_attribute_keeps_its_type_and_value(on_30, seed):
    for attribute_id, (type_, value) in seed["values"].items():
        attribute = (on_30.api("GET", f"/attributes/view/{attribute_id}", on_30.key) or {}).get("Attribute", {})
        assert (attribute.get("type"), attribute.get("value")) == (type_, value), attribute_id


def test_the_attachment_downloads_whole(on_30, seed):
    # Without a JSON Accept header MISP does not take the key as an API login
    status, body = on_30.http("GET", f"/attributes/download/{seed['attachment']}", on_30.key)
    assert (status, body.encode()) == (200, ATTACHMENT)


def test_the_sightings_stay(on_30, seed):
    assert on_30.sql("SELECT COUNT(*) FROM sightings") == seed["sightings"]


def test_the_workers_rebuild_the_correlations(on_30, seed):
    # The conversion empties the correlation tables and queues a full recorrelation
    deadline = time.monotonic() + 300
    while seed["b"] not in related(on_30, seed["a"]) and time.monotonic() < deadline:
        time.sleep(5)
    assert seed["b"] in related(on_30, seed["a"])
