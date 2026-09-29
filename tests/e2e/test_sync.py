"""Hub-spoke sync suite: three MISP instances configured through the org sync container.

    pytest tests/e2e/test_sync.py [--skip-build] [--keep]

Layout 1: B (the hub) pulls every event from A and C; A and C pull from B only
the events tagged release-to:A and release-to:C; B pushes a tagged event to A.
Layout 2: A and C are passive; B pulls from both, tags, and pushes each spoke
only its own events.
"""

import json
import time

import pytest

from stack import TESTS, Stack, scratch_dir

PORTS = {"a": 18091, "b": 18092, "c": 18093}
ADMIN_KEYS = {i: f"adminKeyFor{i.upper()}" + "0" * 28 for i in PORTS}
SYNC_KEY = {  # the key the first instance's sync user holds, for the second instance
    ("a", "b"): "syncKeyAforB" + "0" * 28,
    ("b", "a"): "syncKeyBforA" + "0" * 28,
    ("b", "c"): "syncKeyBforC" + "0" * 28,
    ("c", "b"): "syncKeyCforB" + "0" * 28,
}
DB_PASSWORD = "misp-sync-test"


def url(inst):
    return f"http://localhost:{PORTS[inst]}"


def api(stack, inst, method, path, data=None):
    return stack.api(method, path, ADMIN_KEYS[inst], data, base_url=url(inst))


def post_ok(stack, inst, path, data=None):
    """A POST that must succeed: pulls and pushes accept POST only."""
    status, body = stack.http("POST", path, ADMIN_KEYS[inst], data or {}, base_url=url(inst))
    assert status == 200, f"POST {path} on {inst}: {status} {body[:300]}"


def sql(stack, inst, query):
    out = stack.exec(f"{inst}-mysql", f"mariadb -u misp -p{DB_PASSWORD} -N misp -e \"{query}\"")
    return "".join(out.split())


def wait_for_jobs(stack, inst, timeout=90):
    """Until no job on the instance is unfinished (MISP keeps it at 0 until 3 or 4)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sql(stack, inst, "SELECT COUNT(*) FROM jobs WHERE status NOT IN (3,4);") == "0":
            return
        time.sleep(2)


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
    """Apply an orgs.yaml to one instance through the sync container; its exit code."""
    path = scratch_dir() / f"sync-test-{inst}.yaml"
    path.write_text(config)
    rc, _ = stack.run("sync", env={
        "ADMIN_KEY": ADMIN_KEYS[inst], "SYNC_BASE_URL": f"http://{inst}-caddy:8080",
        "DB_HOST": f"{inst}-mysql", "DB_PORT": "3306", "DB_USER": "misp", "DB_PASSWORD": DB_PASSWORD,
        "DB_NAME": "misp", "MISP_REDIS_HOST": f"{inst}-redis", "ORG_CONFIG_FILE": "/etc/misp-docker/orgs.yaml",
    }, volumes=[f"{path}:/etc/misp-docker/orgs.yaml:ro"])
    path.unlink()
    return rc


def spoke(org_uuid, name, own, hub_server=True, pull_tag=""):
    """orgs.yaml of a spoke: its sync user for B and, in layout 1, B as a pull server."""
    config = f'''teams:
  - uuid: "{org_uuid}"
    name: "{name}"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-b@{own}.test
        authkey: "{SYNC_KEY[(own, 'b')]}"
'''
    if hub_server:
        config += f'''    servers:
      - name: "Hub B"
        url: "http://b-caddy:8080"
        authkey: "{SYNC_KEY[('b', own)]}"
        pull: true
        push: false
        pull_tags:
          - "{pull_tag}"
'''
    return config


def hub(org_uuid, push):
    """orgs.yaml of B: sync users for A and C, both spokes as pull (and in layout 2 push) servers."""
    servers = ""
    for own in ("a", "c"):
        servers += f'''      - name: "Spoke {own.upper()}"
        url: "http://{own}-caddy:8080"
        authkey: "{SYNC_KEY[(own, 'b')]}"
        pull: true
        push: {"true" if push else "false"}
'''
        if push:
            servers += f'''        push_tags:
          - "release-to:{own.upper()}"
'''
    return f'''tags:
  - name: "release-to:A"
    colour: "#0000ff"
  - name: "release-to:C"
    colour: "#ff0000"

teams:
  - uuid: "{org_uuid}"
    name: "Org-B"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-a@b.test
        authkey: "{SYNC_KEY[('b', 'a')]}"
      - email: sync-c@b.test
        authkey: "{SYNC_KEY[('b', 'c')]}"
    servers:
{servers}'''


# -- fixtures: one per phase --------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    s = Stack([TESTS / "docker-compose.sync-test.yml"])
    s.compose("down", "-v")
    if not options["skip_build"]:
        s.compose("build", check=True, timeout=3600)
    s.compose("up", "-d")
    for inst in PORTS:
        s.wait_for_misp(timeout=360, base_url=url(inst))
    # MISP stores only a hash of an authkey: write a known admin key per instance
    for inst, key in ADMIN_KEYS.items():
        s.run("sync", "python3", "-c", f"""
import bcrypt, pymysql, uuid
key = '{key}'
h = bcrypt.hashpw(key.encode(), bcrypt.gensalt(12)).decode().replace('$2b$', '$2y$')
conn = pymysql.connect(host='{inst}-mysql', user='misp', password='{DB_PASSWORD}', database='misp')
with conn.cursor() as cur:
    cur.execute('DELETE FROM auth_keys WHERE user_id = 1')
    cur.execute('INSERT INTO auth_keys (uuid, authkey, authkey_start, authkey_end, created, user_id, expiration) '
                'VALUES (%s, %s, %s, %s, UNIX_TIMESTAMP(), 1, 0)', (str(uuid.uuid4()), h, key[:4], key[-4:]))
conn.commit()
""")
    s.org_uuid = {inst: sql(s, inst, "SELECT uuid FROM organisations WHERE id=1;") for inst in PORTS}
    yield s
    s.finish("sync", options["keep"], failed_in_module())


@pytest.fixture(scope="module")
def layout1(stack):
    create_event(stack, "a", "Event from A", "10.0.0.1")
    create_event(stack, "c", "Event from C", "10.0.0.2")
    return {
        "a": run_sync(stack, "a", spoke(stack.org_uuid["a"], "Org-A", "a", pull_tag="release-to:A")),
        "b": run_sync(stack, "b", hub(stack.org_uuid["b"], push=False)),
        "c": run_sync(stack, "c", spoke(stack.org_uuid["c"], "Org-C", "c", pull_tag="release-to:C")),
    }


@pytest.fixture(scope="module")
def hub_pulled(stack, layout1):
    for name in ("Spoke A", "Spoke C"):
        post_ok(stack, "b", f"/servers/pull/{server_id(stack, 'b', name)}/full")
    wait_for_jobs(stack, "b")


@pytest.fixture(scope="module")
def hub_tagged(stack, hub_pulled):
    ids = {own: event_id(stack, "b", f"Event from {own.upper()}") for own in ("a", "c")}
    for own, eid in ids.items():
        tag_and_publish(stack, "b", eid, f"release-to:{own.upper()}")
    return ids


@pytest.fixture(scope="module")
def spokes_pulled(stack, hub_tagged):
    for inst in ("a", "c"):
        # Stale event indexes in Redis would hide the pulled events
        stack.exec(f"{inst}-redis", "redis-cli -a redis-sync-test FLUSHALL")
    for inst in ("a", "c"):
        post_ok(stack, inst, f"/servers/pull/{server_id(stack, inst, 'Hub B')}/full")
    wait_for_jobs(stack, "a")
    wait_for_jobs(stack, "c")


@pytest.fixture(scope="module")
def hub_pushed_to_a(stack, spokes_pulled):
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
def layout2(stack, hub_pushed_to_a):
    return {
        "a": run_sync(stack, "a", spoke(stack.org_uuid["a"], "Org-A", "a", hub_server=False)),
        "b": run_sync(stack, "b", hub(stack.org_uuid["b"], push=True)),
        "c": run_sync(stack, "c", spoke(stack.org_uuid["c"], "Org-C", "c", hub_server=False)),
    }


@pytest.fixture(scope="module")
def layout2_pulled(stack, layout2):
    create_event(stack, "a", "Layout2 from A", "10.1.0.1")
    create_event(stack, "c", "Layout2 from C", "10.1.0.2")
    for name in ("Spoke A", "Spoke C"):
        post_ok(stack, "b", f"/servers/pull/{server_id(stack, 'b', name)}/full")
    wait_for_jobs(stack, "b")


@pytest.fixture(scope="module")
def layout2_pushed(stack, layout2_pulled):
    for own in ("a", "c"):
        tag_and_publish(stack, "b", event_id(stack, "b", f"Layout2 from {own.upper()}"), f"release-to:{own.upper()}")
    for name in ("Spoke A", "Spoke C"):
        post_ok(stack, "b", f"/servers/push/{server_id(stack, 'b', name)}/full")
    wait_for_jobs(stack, "b")


# -- layout 1 ------------------------------------------------------------------------

def test_all_instances_ready(stack):
    for inst in PORTS:
        assert (api(stack, inst, "GET", "/users/view/me") or {}).get("User", {}).get("email") == "admin@admin.test"


def test_all_instances_configured_through_the_sync_container(layout1):
    assert layout1 == {"a": 0, "b": 0, "c": 0}


@pytest.mark.parametrize("own", ["A", "C"])
def test_hub_pulled_the_event(stack, hub_pulled, own):
    assert event_count(stack, "b", f"Event from {own}") == 1


@pytest.mark.parametrize("own", ["a", "c"])
def test_hub_event_tagged(stack, hub_tagged, own):
    event = api(stack, "b", "GET", f"/events/view/{hub_tagged[own]}") or {}
    assert len(event.get("Event", {}).get("Tag") or []) == 1


@pytest.mark.parametrize("inst,own,foreign", [("a", "A", "C"), ("c", "C", "A")])
def test_spoke_has_its_own_event(stack, spokes_pulled, inst, own, foreign):
    assert event_count(stack, inst, f"Event from {own}") == 1


@pytest.mark.parametrize("inst,own,foreign", [("a", "A", "C"), ("c", "C", "A")])
def test_spoke_pulled_no_foreign_event(stack, spokes_pulled, inst, own, foreign):
    assert event_count(stack, inst, f"Event from {foreign}") == 0


def test_a_received_the_tagged_push(stack, hub_pushed_to_a):
    assert event_count(stack, "a", "Push yes (tagged)") == 1


def test_a_did_not_receive_the_untagged_push(stack, hub_pushed_to_a):
    assert event_count(stack, "a", "Push no (untagged)") == 0


# -- layout 2: hub-initiated ---------------------------------------------------------

@pytest.mark.parametrize("inst", ["a", "c"])
def test_spoke_is_passive(stack, layout2, inst):
    assert layout2[inst] == 0
    active = [s for s in api(stack, inst, "GET", "/servers") or []
              if s["Server"].get("push") or s["Server"].get("pull")]
    assert active == []


@pytest.mark.parametrize("own", ["A", "C"])
def test_hub_pulled_the_layout2_event(stack, layout2_pulled, own):
    assert event_count(stack, "b", f"Layout2 from {own}") == 1


@pytest.mark.parametrize("inst,own,foreign", [("a", "A", "C"), ("c", "C", "A")])
def test_spoke_received_its_own_event_by_hub_push(stack, layout2_pushed, inst, own, foreign):
    assert event_count(stack, inst, f"Layout2 from {own}") == 1


@pytest.mark.parametrize("inst,own,foreign", [("a", "A", "C"), ("c", "C", "A")])
def test_spoke_received_no_foreign_event_by_hub_push(stack, layout2_pushed, inst, own, foreign):
    assert event_count(stack, inst, f"Layout2 from {foreign}") == 0
