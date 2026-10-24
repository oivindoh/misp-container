"""Hub-spoke sync suite: two MISP instances of this tree's images and a MISP 2.5 partner.

    pytest tests/e2e/test_sync.py [--skip-build] [--keep]

The stack is the deploy stack's services once per instance of this tree's
images, prefixed a- and b-, and MISP 2.5 from the MISP project's own images as
c-, written to one Compose file by the stack fixture. B is the hub, A and C
spokes. The org sync container configures A and B, and the suite configures C
through its API. Layout 1: B pulls every event from A and C; A and C pull from
B only the events tagged for them; B pushes a tagged event to A; C pushes an
event to B. Layout 2: A is passive; B pulls from it, tags, and pushes A and C
only the events tagged for them.
"""

import json
import time

import pytest
import yaml

from stack import DEPLOY, TESTS, Stack, env_file, scratch_dir

PORTS = {"a": 18091, "b": 18092, "c": 18093}
# The instances on this tree's images; C runs MISP 2.5
OURS = ("a", "b")
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
SECRETS = TESTS / "test-compose-secrets.env"
REDIS_PASSWORD = env_file(SECRETS)["MISP_REDIS_PASSWORD"]
# The services of the deploy stack each instance runs; the MISP ones run read-only
SERVICES = ("postgres", "redis", "configure", "web", "worker", "caddy")
READ_ONLY = ("configure", "web", "worker", "caddy")
# C: MISP 2.5.50 from the MISP project's images (github.com/MISP/misp-docker), on the
# MariaDB of the migration suite. A rebuild moves the tag, so the digest pins it
MISP_25_CORE = ("ghcr.io/misp/misp-docker/misp-core:v2.5.50"
                "@sha256:ae32e751870fc7dab8bea3a6d16c41a9045f20d724e243c3125c1910e109c848")
MISP_25_NGINX = ("ghcr.io/misp/misp-docker/misp-nginx:v2.5.50"
                 "@sha256:10d8a3af27f49ade99e8f4a8f75802f5d5c565eb38635ac110da9dfb84534c7e")
PARTNER = ("c-db", "c-redis", "c-core", "c-nginx")


def instance_env(inst):
    return {"DB_HOST": f"{inst}-postgres", "MISP_REDIS_HOST": f"{inst}-redis",
            "SIMPLEBACKGROUNDJOBS_SUPERVISOR_HOST": f"{inst}-worker",
            "MISP_BASEURL": f"http://localhost:{PORTS[inst]}", "MISP_UUID": MISP_UUIDS[inst],
            "ADMIN_EMAIL": "admin@admin.test", "ADMIN_ORG": f"Org-{inst.upper()}",
            "ADMIN_ORG_UUID": ORG_UUIDS[inst], "ADMIN_KEY": ADMIN_KEYS[inst]}


def partner_env():
    """The settings of C, in the names of MISP's own images."""
    secrets = env_file(SECRETS)
    return {"BASE_URL": url("c"), "ADMIN_EMAIL": "admin@admin.test", "ADMIN_PASSWORD": secrets["ADMIN_PASSWORD"],
            "ADMIN_KEY": ADMIN_KEYS["c"], "ADMIN_ORG": "Org-C", "ADMIN_ORG_UUID": ORG_UUIDS["c"],
            "MYSQL_HOST": "c-db", "MYSQL_USER": secrets["MARIADB_USER"],
            "MYSQL_PASSWORD": secrets["MARIADB_PASSWORD"], "MYSQL_DATABASE": "misp",
            "REDIS_HOST": "c-redis", "REDIS_PASSWORD": REDIS_PASSWORD, "DISABLE_IPV6": "true",
            "DISABLE_CA_REFRESH": "true", "ENABLE_BACKGROUND_UPDATES": "true"}


def write_stack(directory):
    """One Compose file with SERVICES of deploy/docker-compose.yml for A and B, and C; its path."""
    deploy = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]
    directory.mkdir(parents=True, exist_ok=True)
    services, volumes = {}, {}

    def prefixed(inst, volume):
        source, _, target = volume.partition(":")
        if "/" in source or source.startswith((".", "$")):
            return volume
        volumes[f"{inst}-{source}"] = None
        return f"{inst}-{source}:{target}"

    for inst in OURS:
        env = directory / f"{inst}.env"
        env.write_text("".join(f"{key}={value}\n" for key, value in instance_env(inst).items()))
        for name in SERVICES:
            spec = {k: v for k, v in deploy[name].items() if k not in ("build", "ports", "profiles")}
            spec["env_file"] = [str(DEPLOY / f) for f in spec.get("env_file", [])] + [str(SECRETS), str(env)]
            spec["volumes"] = [prefixed(inst, v) for v in spec.get("volumes", [])]
            deps = spec.pop("depends_on", {})
            deps = deps if isinstance(deps, dict) else {d: {"condition": "service_started"} for d in deps}
            spec["depends_on"] = {f"{inst}-{d}": c for d, c in deps.items() if d in SERVICES}
            if name == "caddy":
                spec["environment"] = {**spec.get("environment", {}), "PHP_FPM_HOST": f"{inst}-web"}
                spec["ports"] = [f"{PORTS[inst]}:8080"]
            if name in READ_ONLY:
                spec["read_only"] = True
            services[f"{inst}-{name}"] = {k: v for k, v in spec.items() if v not in ([], {})}
    mariadb = yaml.safe_load((TESTS / "docker-compose.migrate.yml").read_text())["services"]["mysql"]
    services["c-db"] = {**mariadb, "env_file": [str(SECRETS)], "volumes": ["c-db:/var/lib/mysql"]}
    volumes["c-db"] = None
    services["c-redis"] = {**deploy["redis"], "env_file": [str(SECRETS)]}
    partner = directory / "c.env"
    partner.write_text("".join(f"{key}={value}\n" for key, value in partner_env().items()))
    services["c-core"] = {"image": MISP_25_CORE, "env_file": [str(partner)], "depends_on": {
        "c-db": {"condition": "service_healthy"}, "c-redis": {"condition": "service_healthy"}}}
    services["c-nginx"] = {"image": MISP_25_NGINX, "ports": [f"{PORTS['c']}:8080"],
                           "environment": {"BASE_URL": url("c"), "FASTCGI_LISTEN": "c-core:9002",
                                           "FASTCGI_LISTEN_STATUS": "c-core:9003", "DISABLE_IPV6": "true"},
                           "depends_on": {"c-core": {"condition": "service_started"}}}
    # One org sync service; run_sync points it at an instance
    sync = {k: v for k, v in deploy["sync"].items() if k not in ("build", "depends_on")}
    sync["env_file"] = [str(DEPLOY / f) for f in sync["env_file"]] + [str(SECRETS)]
    services["sync"] = sync
    path = directory / "docker-compose.yml"
    path.write_text(yaml.safe_dump({"services": services, "volumes": volumes}, sort_keys=False))
    return path


def url(inst):
    return f"http://localhost:{PORTS[inst]}"


def api(stack, inst, method, path, data=None):
    return stack.api(method, path, ADMIN_KEYS[inst], data, base_url=url(inst))


def post_ok(stack, inst, path, data=None):
    """A POST that must succeed: pulls and pushes accept POST only."""
    status, body = stack.http("POST", path, ADMIN_KEYS[inst], data or {}, base_url=url(inst))
    assert status == 200, f"POST {path} on {inst}: {status} {body[:300]}"


def unfinished_jobs(stack, inst):
    query = "SELECT COUNT(*) FROM jobs WHERE status NOT IN (3,4)"
    if inst in OURS:
        return stack.sql(query + ";", service=f"{inst}-web")
    return stack.exec("c-db", f'MYSQL_PWD="$MARIADB_PASSWORD" mariadb -N -u "$MARIADB_USER" misp -e "{query}"')


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
    """Apply an orgs.yaml to one instance through the sync container; its exit code."""
    path = scratch_dir() / f"sync-test-{inst}.yaml"
    path.write_text(config)
    rc, _ = stack.run("sync", env={
        "ADMIN_KEY": ADMIN_KEYS[inst], "SYNC_BASE_URL": f"http://{inst}-caddy:8080",
        "DB_HOST": f"{inst}-postgres", "MISP_REDIS_HOST": f"{inst}-redis",
    }, volumes=[f"{path}:/etc/misp-docker/orgs.yaml:ro"])
    path.unlink()
    return rc


def spoke(hub_server=True):
    """orgs.yaml of A: its sync user for B and, in layout 1, B as a pull server for release-to:A."""
    config = f'''teams:
  - uuid: "{ORG_UUIDS['a']}"
    name: "Org-A"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-b@a.test
        authkey: "{SYNC_KEY[('a', 'b')]}"
'''
    if hub_server:
        config += f'''    servers:
      - name: "Hub B"
        url: "http://b-caddy:8080"
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
    for spoke_name, spoke_url, key in (("A", "http://a-caddy:8080", SYNC_KEY[("a", "b")]),
                                       ("C", "http://c-nginx:8080", c_key)):
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
    sync_users:
      - email: sync-a@b.test
        authkey: "{SYNC_KEY[('b', 'a')]}"
      - email: sync-c@b.test
        authkey: "{SYNC_KEY[('b', 'c')]}"
    servers:
{server}'''


# -- fixtures: one per phase --------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    if not options["skip_build"]:
        Stack([DEPLOY / "docker-compose.yml"]).compose("build", check=True, timeout=3600)
    s = Stack([write_stack(scratch_dir() / "hub-spoke")])
    s.compose("down", "-v")
    s.compose("up", "-d", *[f"{inst}-{name}" for inst in OURS for name in SERVICES], *PARTNER)
    for inst in PORTS:
        # MISP's own image loads its schema and data on its first start
        s.wait_for_misp(timeout=600 if inst == "c" else 360, base_url=url(inst))
    yield s
    s.finish("sync", options["keep"], failed_in_module())


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
        "name": "Hub B", "url": "http://b-caddy:8080", "authkey": SYNC_KEY[("b", "c")], "remote_org_id": org_id,
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
    stack.exec("a-redis", f"redis-cli -a {REDIS_PASSWORD} FLUSHALL")
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
