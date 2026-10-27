"""Hub-spoke sync suite: two releases of the chart and a MISP 2.5 partner on one k3s cluster.

    pytest tests/e2e/test_sync.py [--skip-build] [--keep]

A and B are releases of this tree's chart in the namespaces a and b; C is MISP
2.5 from the MISP project's own images (tests/k3s/misp-25.yaml) in the
namespace c. B is the hub, A and C spokes. The org sync configures A and B, and
the suite configures C through its API. Layout 1: B pulls every event from A
and C; A and C pull from B only the events tagged for them; B pushes a tagged
event to A; C pushes an event to B. Layout 2: A is passive; B pulls from it,
tags, and pushes A and C only the events tagged for them.
"""

import json
import shlex
import time

import pytest

import manifests
from cluster import Cluster, build_images
from stack import VALUES, Release, values_file

# The host port of each instance and its node port
PORTS = {"a": 18091, "b": 18092, "c": 18093}
NODE_PORTS = {"a": 30091, "b": 30092, "c": 30093}
# The instances on this tree's images; C runs MISP 2.5
OURS = ("a", "b")
# Each instance as the others reach it in the cluster
INTERNAL = {"a": "http://web.a:8080", "b": "http://web.b:8080", "c": "http://nginx.c:8080"}
# The configure step refuses a key with ten zeros in a row as a placeholder
ADMIN_KEYS = {i: f"adminKeyFor{i.upper()}" + "k7" * 14 for i in PORTS}
MISP_UUIDS = {"a": "1a4e2c6d-0b3f-4a58-9c1d-7e2f3b4c5d6a", "b": "2b5f3d7e-1c40-4b69-8d2e-8f3a4c5d6e7b"}
ORG_UUIDS = {"a": "1a4e2c6d-0b3f-4a58-9c1d-7e2f3b4c5d6f", "b": "2b5f3d7e-1c40-4b69-8d2e-8f3a4c5d6e7f",
             "c": "3c6a4e8f-2d51-4c7a-9e3f-9a4b5c6d7e8f"}
SYNC_KEY = {  # the key the first instance's sync user holds, for the second instance
    ("a", "b"): "syncKeyAforB" + "s5" * 14,
    ("b", "a"): "syncKeyBforA" + "s5" * 14,
    ("b", "c"): "syncKeyBforC" + "s5" * 14,
}
SECRETS = values_file(VALUES)["secrets"]
REDIS_PASSWORD = SECRETS["app"]["MISP_REDIS_PASSWORD"]
PARTNER_DB_PASSWORD = "misp-25-db-pw"
# The partner's Redis is its own, and the entrypoint of MISP's 2.5 images writes the password
# unquoted into its config: a plain one
PARTNER_REDIS_PASSWORD = "misp-25-redis-pw"


class Instances(dict):
    """The releases of A and B and the partner C, by name; dump_logs serves the failure hook."""

    def __init__(self, cluster, releases):
        super().__init__(releases)
        self.cluster = cluster

    def dump_logs(self, name):
        return self.cluster.dump_logs(name)


def instance_values(inst):
    return {"env": {"MISP_BASEURL": url(inst), "MISP_UUID": MISP_UUIDS[inst], "ADMIN_EMAIL": "admin@admin.test",
                    "ADMIN_ORG": f"Org-{inst.upper()}", "ADMIN_ORG_UUID": ORG_UUIDS[inst]},
            "secrets": {"admin": {"ADMIN_KEY": ADMIN_KEYS[inst]}}}


def partner_settings():
    """The settings of C, in the names of MISP's own images."""
    return {"BASE_URL": url("c"), "ADMIN_EMAIL": "admin@admin.test", "ADMIN_PASSWORD": SECRETS["admin"]["ADMIN_PASSWORD"],
            "ADMIN_KEY": ADMIN_KEYS["c"], "ADMIN_ORG": "Org-C", "ADMIN_ORG_UUID": ORG_UUIDS["c"],
            "MYSQL_HOST": "mariadb", "MYSQL_USER": "misp", "MYSQL_PASSWORD": PARTNER_DB_PASSWORD,
            "MYSQL_DATABASE": "misp", "REDIS_HOST": "redis", "REDIS_PASSWORD": PARTNER_REDIS_PASSWORD,
            "DISABLE_IPV6": "true", "DISABLE_CA_REFRESH": "true", "ENABLE_BACKGROUND_UPDATES": "true"}


def url(inst):
    return f"http://localhost:{PORTS[inst]}"


def api(stack, inst, method, path, data=None):
    return stack[inst].api(method, path, ADMIN_KEYS[inst], data)


def post_ok(stack, inst, path, data=None):
    """A POST that must succeed: pulls and pushes accept POST only."""
    status, body = stack[inst].http("POST", path, ADMIN_KEYS[inst], data or {})
    assert status == 200, f"POST {path} on {inst}: {status} {body[:300]}"


def unfinished_jobs(stack, inst):
    query = "SELECT COUNT(*) FROM jobs WHERE status NOT IN (3,4)"
    if inst in OURS:
        return stack[inst].sql(query + ";")
    return stack[inst].exec("mariadb", f'MYSQL_PWD="$MARIADB_PASSWORD" mariadb -N -u "$MARIADB_USER" misp -e "{query}"')


def wait_for_jobs(stack, inst, timeout=90):
    """Until no job on the instance is unfinished (MISP keeps it at 0 until 3 or 4)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if unfinished_jobs(stack, inst) == "0":
            return
        time.sleep(2)
    raise RuntimeError(f"{inst}: a job is still unfinished after {timeout}s")


def event_count(stack, inst, info):
    return sum(1 for e in api(stack, inst, "GET", "/events/index") or [] if e.get("info") == info)


def event_id(stack, inst, info):
    return next(e["id"] for e in api(stack, inst, "GET", "/events/index") or [] if e.get("info") == info)


def server_id(stack, inst, name):
    return next(s["Server"]["id"] for s in api(stack, inst, "GET", "/servers") or [] if s["Server"]["name"] == name)


def create_event(stack, inst, info, ip, publish=True):
    event = api(stack, inst, "POST", "/events/add", {"Event": {
        "info": info, "distribution": 3,
        "Attribute": [{"category": "Network activity", "type": "ip-dst", "value": ip}]}}) or {}
    eid = event.get("Event", {}).get("id") or event.get("id")
    if publish:
        api(stack, inst, "POST", f"/events/publish/{eid}", {})
    return eid


def tag_and_publish(stack, inst, eid, tag):
    api(stack, inst, "POST", "/events/addTag", {"event": eid, "tag": tag})
    api(stack, inst, "POST", f"/events/publish/{eid}", {})


def run_sync(stack, inst, config: str) -> int:
    """Apply an orgs.yaml to one instance through the org sync in its toolbox; the exit code."""
    return stack[inst].org_sync(config, ADMIN_KEYS[inst])[0]


def spoke(hub_server=True):
    """orgs.yaml of A: its sync user for B and, in layout 1, B as a pull server for release-to:A."""
    config = f'''teams:
  - uuid: "{ORG_UUIDS['a']}"
    name: "Org-A"
    users:
      - email: admin@admin.test
        role: admin
      - email: sync-b@a.test
        role: Sync user
        authkey: "{SYNC_KEY[('a', 'b')]}"
'''
    if hub_server:
        config += f'''    servers:
      - name: "Hub B"
        url: "{INTERNAL['b']}"
        authkey: "{SYNC_KEY[('b', 'a')]}"
        pull: true
        push: false
        pull_tags:
          - "release-to:A"
'''
    return config


def hub(push, c_key):
    """orgs.yaml of B: the sync users for A and C, and A and C as pull (and in layout 2 push) servers."""
    server = ""
    for spoke_name, spoke_url, key in (("A", INTERNAL["a"], SYNC_KEY[("a", "b")]),
                                       ("C", INTERNAL["c"], c_key)):
        server += f'''      - name: "Spoke {spoke_name}"
        url: "{spoke_url}"
        authkey: "{key}"
        pull: true
        push: {"true" if push else "false"}
'''
        if push:
            server += f'''        push_tags:
          - "release-to:{spoke_name}"
'''
    return f'''tags:
  - name: "release-to:A"
    colour: "#0000ff"
  - name: "release-to:C"
    colour: "#ff0000"

teams:
  - uuid: "{ORG_UUIDS['b']}"
    name: "Org-B"
    users:
      - email: admin@admin.test
        role: admin
      - email: sync-a@b.test
        role: Sync user
        authkey: "{SYNC_KEY[('b', 'a')]}"
      - email: sync-c@b.test
        role: Sync user
        authkey: "{SYNC_KEY[('b', 'c')]}"
    servers:
{server}'''


# -- fixtures: one per phase --------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    build_images(options)
    cluster = Cluster("misp-sync", {PORTS[i]: NODE_PORTS[i] for i in PORTS})
    cluster.start()
    cluster.load_images_under_test()
    instances = Instances(cluster, {i: Release(cluster, i, PORTS[i], instance_values(i) if i in OURS else None)
                                    for i in PORTS})
    instances["c"].apply(*manifests.mariadb("misp", PARTNER_DB_PASSWORD), *manifests.misp25(partner_settings()),
                         manifests.nodeport("web", "nginx", {8080: NODE_PORTS["c"]}))
    for inst in OURS:
        instances[inst].apply(manifests.nodeport("web", "web", {8080: NODE_PORTS[inst]}))
        instances[inst].install(wait=False)
    for inst in OURS:
        instances[inst].wait_for_revision()
    for inst in PORTS:
        # MISP's own image loads its schema and data on its first start
        instances[inst].wait_for_misp(timeout=600 if inst == "c" else 360)
    yield instances
    cluster.finish("sync", options["keep"], failed_in_module())


@pytest.fixture(scope="module")
def partner(stack):
    """C, configured through its API: a sync user for B, and B as a pull server for release-to:C.

    The org sync writes authkeys into PostgreSQL, so it cannot configure C. The sync
    user's key is the one C generates; B's hub config carries it.
    """
    org_id = (api(stack, "c", "GET", "/users/view/me") or {})["User"]["org_id"]
    user = api(stack, "c", "POST", "/admin/users/add", {"User": {
        "email": "sync-b@c.test", "org_id": org_id, "role_id": 5, "password": "Sync-Us3r!Pass#2026"}}) or {}
    key = (api(stack, "c", "POST", f"/auth_keys/add/{user['User']['id']}", {}) or {})["AuthKey"]["authkey_raw"]
    rules = {"tags": {"OR": ["release-to:C"], "NOT": []}, "orgs": {"OR": [], "NOT": []}, "url_params": ""}
    server = api(stack, "c", "POST", "/servers/add", {"Server": {
        "name": "Hub B", "url": INTERNAL["b"], "authkey": SYNC_KEY[("b", "c")], "remote_org_id": org_id,
        "pull": True, "push": False, "self_signed": False, "pull_rules": json.dumps(rules),
        "push_rules": json.dumps({"tags": {"OR": [], "NOT": []}, "orgs": {"OR": [], "NOT": []}})}}) or {}
    assert server.get("Server", {}).get("id"), server
    create_event(stack, "c", "Event from C", "10.2.0.1")
    return key


@pytest.fixture(scope="module")
def layout1(stack, partner):
    create_event(stack, "a", "Event from A", "10.0.0.1")
    return {"a": run_sync(stack, "a", spoke()), "b": run_sync(stack, "b", hub(push=False, c_key=partner))}


@pytest.fixture(scope="module")
def hub_pulled(stack, layout1):
    for spoke_name in ("Spoke A", "Spoke C"):
        post_ok(stack, "b", f"/servers/pull/{server_id(stack, 'b', spoke_name)}/full")
    wait_for_jobs(stack, "b")


@pytest.fixture(scope="module")
def hub_tagged(stack, hub_pulled):
    eid = event_id(stack, "b", "Event from A")
    tag_and_publish(stack, "b", eid, "release-to:A")
    for release in ("A", "C"):
        tag_and_publish(stack, "b", create_event(stack, "b", f"Release to {release}", "10.0.0.2", publish=False),
                        f"release-to:{release}")
    return eid


@pytest.fixture(scope="module")
def spoke_pulled(stack, hub_tagged):
    # Stale event indexes in Redis would hide the pulled events
    stack["a"].exec("redis", f"redis-cli -a {shlex.quote(REDIS_PASSWORD)} FLUSHALL")
    for inst in ("a", "c"):
        post_ok(stack, inst, f"/servers/pull/{server_id(stack, inst, 'Hub B')}/full")
    for inst in ("a", "c"):
        wait_for_jobs(stack, inst)


@pytest.fixture(scope="module")
def hub_pushed_to_a(stack, spoke_pulled):
    tags = (api(stack, "b", "GET", "/tags") or {}).get("Tag", [])
    tag_a = next(t["id"] for t in tags if t["name"] == "release-to:A")
    rules = {"tags": {"OR": [int(tag_a)], "NOT": []}, "orgs": {"OR": [], "NOT": []}}
    server_a = server_id(stack, "b", "Spoke A")
    api(stack, "b", "POST", f"/servers/edit/{server_a}", {"Server": {"push": True, "push_rules": json.dumps(rules)}})
    tagged = create_event(stack, "b", "Push yes (tagged)", "10.0.0.99", publish=False)
    tag_and_publish(stack, "b", tagged, "release-to:A")
    create_event(stack, "b", "Push no (untagged)", "10.0.0.100")
    post_ok(stack, "b", f"/servers/push/{server_a}/full")
    wait_for_jobs(stack, "b")


@pytest.fixture(scope="module")
def partner_pushed(stack, partner, hub_pushed_to_a):
    """C pushes a new event to B."""
    hub_b = server_id(stack, "c", "Hub B")
    api(stack, "c", "POST", f"/servers/edit/{hub_b}", {"Server": {"push": True}})
    create_event(stack, "c", "Pushed from C", "10.2.0.2")
    post_ok(stack, "c", f"/servers/push/{hub_b}/full")
    wait_for_jobs(stack, "c")


@pytest.fixture(scope="module")
def layout2(stack, partner, partner_pushed):
    return {"a": run_sync(stack, "a", spoke(hub_server=False)),
            "b": run_sync(stack, "b", hub(push=True, c_key=partner))}


@pytest.fixture(scope="module")
def layout2_pulled(stack, layout2):
    create_event(stack, "a", "Layout2 from A", "10.1.0.1")
    post_ok(stack, "b", f"/servers/pull/{server_id(stack, 'b', 'Spoke A')}/full")
    wait_for_jobs(stack, "b")


@pytest.fixture(scope="module")
def layout2_pushed(stack, layout2_pulled):
    for release in ("A", "C"):
        tag_and_publish(stack, "b", create_event(stack, "b", f"Layout2 to {release}", "10.1.0.2", publish=False),
                        f"release-to:{release}")
    for spoke_name in ("Spoke A", "Spoke C"):
        post_ok(stack, "b", f"/servers/push/{server_id(stack, 'b', spoke_name)}/full")
    wait_for_jobs(stack, "b")


# -- layout 1 ------------------------------------------------------------------------

def test_all_instances_ready(stack):
    for inst in PORTS:
        assert (api(stack, inst, "GET", "/users/view/me") or {}).get("User", {}).get("email") == "admin@admin.test"


def test_all_instances_configured_through_the_sync_container(layout1):
    assert layout1 == {"a": 0, "b": 0}


def test_hub_pulled_the_event(stack, hub_pulled):
    assert event_count(stack, "b", "Event from A") == 1


def test_hub_event_tagged(stack, hub_tagged):
    event = api(stack, "b", "GET", f"/events/view/{hub_tagged}") or {}
    assert len(event.get("Event", {}).get("Tag") or []) == 1


def test_spoke_pulled_its_release(stack, spoke_pulled):
    assert event_count(stack, "a", "Release to A") == 1


def test_spoke_pulled_no_foreign_release(stack, spoke_pulled):
    assert event_count(stack, "a", "Release to C") == 0


def test_a_received_the_tagged_push(stack, hub_pushed_to_a):
    assert event_count(stack, "a", "Push yes (tagged)") == 1


def test_a_did_not_receive_the_untagged_push(stack, hub_pushed_to_a):
    assert event_count(stack, "a", "Push no (untagged)") == 0


# -- layout 1 with the MISP 2.5 partner ---------------------------------------------

def test_hub_pulled_the_event_from_misp_25(stack, hub_pulled):
    assert event_count(stack, "b", "Event from C") == 1


def test_misp_25_pulled_its_release(stack, spoke_pulled):
    assert event_count(stack, "c", "Release to C") == 1


def test_misp_25_pulled_no_foreign_release(stack, spoke_pulled):
    assert event_count(stack, "c", "Release to A") == 0


def test_hub_received_the_push_from_misp_25(stack, partner_pushed):
    assert event_count(stack, "b", "Pushed from C") == 1


# -- layout 2: hub-initiated ---------------------------------------------------------

def test_spoke_is_passive(stack, layout2):
    assert layout2 == {"a": 0, "b": 0}
    active = [s for s in api(stack, "a", "GET", "/servers") or []
              if s["Server"].get("push") or s["Server"].get("pull")]
    assert active == []


def test_hub_pulled_the_layout2_event(stack, layout2_pulled):
    assert event_count(stack, "b", "Layout2 from A") == 1


def test_spoke_received_its_release_by_hub_push(stack, layout2_pushed):
    assert event_count(stack, "a", "Layout2 to A") == 1


def test_spoke_received_no_foreign_release_by_hub_push(stack, layout2_pushed):
    assert event_count(stack, "a", "Layout2 to C") == 0


def test_misp_25_received_its_release_by_hub_push(stack, layout2_pushed):
    assert event_count(stack, "c", "Layout2 to C") == 1


def test_misp_25_received_no_foreign_release_by_hub_push(stack, layout2_pushed):
    assert event_count(stack, "c", "Layout2 to A") == 0
