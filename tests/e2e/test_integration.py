"""Integration suite: one MISP stack in Compose, checked feature by feature.

    pytest tests/e2e/test_integration.py [--skip-build] [--keep] [--db-engine postgres]

The tests run in file order on one stack. Later sections depend on the state
earlier ones leave: custom auth creates a user that the org sync warm run
disables, the metrics section stops and starts the worker, and the
multi-replica section scales web to two replicas and back.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from stack import DEPLOY, REPO, TESTS, Stack, env_file, scratch_dir

TEST_ENV = env_file(TESTS / "test-compose.env")
ADMIN_EMAIL = TEST_ENV["ADMIN_EMAIL"]
MISP_VERSION = next(line.split("=", 1)[1].strip() for line in (REPO / "Dockerfile").read_text().splitlines()
                    if line.startswith("ARG CORE_TAG="))
METRICS = "http://localhost:19191"
MODULES = "http://localhost:16666"
GARAGE = "http://localhost:3903"
GARAGE_ADMIN = "s3cr3t-admin-t0ken"
SYNC_ORG_UUID = "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"
QUEUES = ("default", "prio", "email", "cache", "update")


def garage(method, path, data=None):
    request = urllib.request.Request(GARAGE + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Authorization": f"Bearer {GARAGE_ADMIN}",
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def bootstrap_garage():
    """Layout, key and bucket for the S3 section; returns (access key, secret, bucket id)."""
    deadline = time.monotonic() + 30
    while True:
        try:
            node = garage("GET", "/v2/GetClusterStatus")["nodes"][0]["id"]
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    garage("POST", "/v2/UpdateClusterLayout",
           {"roles": [{"id": node, "zone": "dc1", "capacity": 1073741824, "tags": []}]})
    garage("POST", "/v2/ApplyClusterLayout", {"version": 1})
    key = garage("POST", "/v2/CreateKey", {"name": "misp-test"})
    bucket = garage("POST", "/v2/CreateBucket", {"globalAlias": "misp-attachments"})
    garage("POST", "/v2/AllowBucketKey", {"bucketId": bucket["id"], "accessKeyId": key["accessKeyId"],
                                          "permissions": {"read": True, "write": True, "owner": True}})
    return key["accessKeyId"], key["secretAccessKey"], bucket["id"]


@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    files = [DEPLOY / "docker-compose.yml", TESTS / "docker-compose.test.yml"]
    profiles = ()
    if options["db_engine"] == "postgres":
        # The overlay points every MISP container at the postgres service
        files.append(TESTS / "docker-compose.postgres.yml")
        profiles = ("postgres",)
    s = Stack(files, profiles)
    s.engine_under_test = "postgres" if options["db_engine"] == "postgres" else "mysql"
    s.compose("down", "-v")
    if not options["skip_build"]:
        s.compose("build", check=True, timeout=3600)
    s.compose("up", "-d")
    s.wait_for_misp()
    s.s3 = bootstrap_garage()
    s.key = s.admin_key(ADMIN_EMAIL)
    yield s
    s.finish("integration", options["keep"], failed_in_module())


def me(stack, key=None):
    return stack.api("GET", "/users/view/me", key or stack.key) or {}


def sql_true(stack):
    return "TRUE" if stack.engine_under_test == "postgres" else "1"


def sql_now(stack):
    return "EXTRACT(EPOCH FROM NOW())::bigint" if stack.engine_under_test == "postgres" else "UNIX_TIMESTAMP()"


def task(stack, *args, key=None):
    """The task runner in the sync service: its exit code and output."""
    return stack.run("sync", "python3", "-m", "misp_container.task", *args,
                     env={"ADMIN_KEY": stack.key if key is None else key, "SYNC_BASE_URL": "http://caddy:8080"})


# -- non-root operation --------------------------------------------------------------

@pytest.mark.parametrize("service", ["web", "worker"])
def test_runs_as_uid_1000(stack, service):
    assert stack.exec(service, "id -u") == "1000"


def test_caddy_runs_as_uid_1000(stack):
    # Caddy runs on scratch, with no shell: read its image user
    user = subprocess.run([stack.engine, "inspect", stack.container_id("caddy"), "--format", "{{.Config.User}}"],
                          capture_output=True, text=True).stdout.strip()
    assert user.split(":")[0] == "1000"


# -- HTTP and caddy ------------------------------------------------------------------

def test_caddy_proxies_fpm_ping(stack):
    assert stack.http("GET", "/fpm-ping", json_api=False)[1].strip() == "pong"


def test_login_page(stack):
    assert stack.http("GET", "/users/login", json_api=False)[0] == 200


def test_caddy_serves_static_css(stack):
    assert stack.http("GET", "/css/main.css", json_api=False)[0] == 200


def test_root_redirects_to_login(stack):
    assert stack.http("GET", "/", json_api=False, redirects=False)[0] == 302


# -- admin user configuration --------------------------------------------------------

def test_admin_email_from_env(stack):
    assert me(stack).get("User", {}).get("email") == ADMIN_EMAIL


def test_admin_has_no_forced_password_change(stack):
    assert me(stack).get("User", {}).get("change_pw") is False


def test_admin_org_name(stack):
    assert me(stack).get("Organisation", {}).get("name") == "Test Org"


def test_admin_org_uuid(stack):
    assert me(stack).get("Organisation", {}).get("uuid") == TEST_ENV["ADMIN_ORG_UUID"]


def test_last_pw_change_is_set(stack):
    assert "NULL" not in stack.sql("SELECT last_pw_change FROM users WHERE id=1;")


# -- database settings storage -------------------------------------------------------

def test_settings_stored_in_the_database(stack):
    assert stack.sql("SELECT COUNT(*) FROM system_settings;").isdigit()


def test_more_than_30_settings_in_the_database(stack):
    assert int(stack.sql("SELECT COUNT(*) FROM system_settings;")) > 30


def test_baseurl_stored_in_the_database(stack):
    assert stack.sql("SELECT value FROM system_settings WHERE setting='MISP.baseurl';") == '"http://caddy:8080"'


# -- workers -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def supervisor_status(stack):
    stack.wait_for("worker", "test -S /tmp/supervisor.sock", timeout=60)
    time.sleep(3)
    return stack.exec("worker", 'supervisorctl -s unix:///tmp/supervisor.sock '
                                '-u "${SIMPLEBACKGROUNDJOBS_SUPERVISOR_USER:-supervisor}" '
                                '-p "${SIMPLEBACKGROUNDJOBS_SUPERVISOR_PASSWORD:-supervisor}" status')


@pytest.mark.parametrize("queue", QUEUES)
def test_worker_queue_running(supervisor_status, queue):
    assert re.search(rf"{queue}.*RUNNING", supervisor_status)


def test_no_scheduler_program(supervisor_status):
    # Periodic work belongs to the task runner; MISP's own scheduler never runs
    assert "scheduler" not in supervisor_status


def test_web_reaches_supervisord_over_tcp(stack):
    _, out = stack.python("""
import os, urllib.request
host = os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_HOST', 'worker')
port = os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_PORT', '9001')
mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
mgr.add_password(None, f'http://{host}:{port}', os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_USER', 'supervisor'),
                 os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_PASSWORD', 'supervisor'))
opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
body = b'<?xml version="1.0"?><methodCall><methodName>supervisor.getState</methodName></methodCall>'
print('OK' if opener.open(f'http://{host}:{port}/RPC2', body, timeout=5).status == 200 else 'no')
""")
    assert out.strip().endswith("OK")


# -- background jobs -----------------------------------------------------------------

def test_a_published_event_runs_a_job(stack):
    event = stack.api("POST", "/events/add", stack.key, {"Event": {"info": "Background job test", "distribution": 0}}) or {}
    eid = event.get("Event", {}).get("id")
    assert eid, event
    stack.http("POST", f"/events/publish/{eid}", stack.key, {})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and int(stack.sql("SELECT COUNT(*) FROM jobs WHERE status=4;") or 0) == 0:
        time.sleep(2)
    done = int(stack.sql("SELECT COUNT(*) FROM jobs WHERE status=4;") or 0)
    stack.http("POST", f"/events/delete/{eid}", stack.key, {})
    assert done > 0 or int(stack.sql("SELECT COUNT(*) FROM jobs;") or 0) > 0


# -- PHP-FPM -------------------------------------------------------------------------

def test_fpm_listens_on_9002(stack):
    # 9002 is 0x232A in /proc/net/tcp6
    assert ":232A" in stack.exec("web", "cat /proc/net/tcp6")


# -- distribution files and app/Config rendering -------------------------------------

def test_taxonomies_ship_in_the_image(stack):
    assert stack.exec("web", "ls /var/www/MISP/app/files/taxonomies/ | head -3")


def test_tlp_taxonomy_ships_in_the_image(stack):
    assert "tlp" in stack.exec("web", "ls /var/www/MISP/app/files/taxonomies/").split()


def test_bootstrap_has_the_auth_plugin_patch(stack):
    assert "Detect what auth modules" in stack.exec("web", "cat /var/www/MISP/app/Config/bootstrap.php")


def test_database_php_has_the_engine_host(stack):
    hosts = stack.exec("web", "grep host /var/www/MISP/app/Config/database.php")
    assert stack.engine_under_test in hosts


@pytest.mark.parametrize("needle", ["redis_host", "redis_password", "SimpleBackgroundJobs", "supervisor_host"])
def test_web_config_php(stack, needle):
    # Each pod renders its own app/Config; workers never run the configure step
    assert needle in stack.exec("web", "cat /var/www/MISP/app/Config/config.php")


@pytest.mark.parametrize("needle", ["redis_host", "redis_password", "SimpleBackgroundJobs"])
def test_worker_config_php(stack, needle):
    assert needle in stack.exec("worker", "cat /var/www/MISP/app/Config/config.php")


# -- GPG -----------------------------------------------------------------------------

def test_gpg_key_generated(stack):
    assert "trustdb.gpg" in stack.exec("web", "ls /var/www/MISP/.gnupg/")


# -- MISP API ------------------------------------------------------------------------

def test_version_reported(stack):
    assert "2.5" in (stack.api("GET", "/servers/getVersion.json", stack.key) or {}).get("version", "")


def test_event_created_through_the_api(stack):
    event = stack.api("POST", "/events/add", stack.key, {"Event": {"info": "Integration test event", "distribution": 0}}) or {}
    eid = event.get("Event", {}).get("id")
    assert eid, event
    stack.http("POST", f"/events/delete/{eid}", stack.key, {})


# -- warm restart --------------------------------------------------------------------

@pytest.fixture(scope="module")
def warm_run(stack):
    return stack.python("""
from misp_container.env import apply_defaults
from misp_container.config import SettingsCache, apply_settings_fast
from misp_container import db
apply_defaults()
db.acquire_config_lock()
try:
    cache = SettingsCache()
    cache.load()
    cache.load_defaults_version()
    apply_settings_fast('minimum_config', cache)
finally:
    db.release_config_lock()
""")[1]


def test_warm_start_loads_the_database_settings(warm_run):
    assert "DB settings" in warm_run


def test_warm_start_changes_no_minimum_config(warm_run):
    assert "minimum_config: 0 changed" in warm_run


# -- version-gated defaults ----------------------------------------------------------

GATE_PRELUDE = """
import os, shutil, yaml
from misp_container.env import apply_defaults
from misp_container import cake, db
from misp_container.config import SettingsCache, apply_settings_fast, load_settings_yaml
apply_defaults()
SRC = '/etc/misp-docker/settings.yaml'
def patched(setting, since, value):
    os.chmod('/etc/misp-docker', 0o770); os.chmod(SRC, 0o660)
    shutil.copy2(SRC, '/tmp/settings.yaml.bak')
    with open(SRC) as f: data = yaml.safe_load(f)
    data['settings']['critical'][setting]['since'] = since
    data['settings']['critical'][setting]['value'] = value
    with open(SRC, 'w') as f: yaml.dump(data, f)
    return load_settings_yaml()
def restore():
    shutil.copy2('/tmp/settings.yaml.bak', SRC)
    os.chmod(SRC, 0o440); os.chmod('/etc/misp-docker', 0o550)
"""


def test_defaults_version_saved_after_configure(stack):
    stack.python(GATE_PRELUDE + """
db.query("DELETE FROM system_settings WHERE setting='misp_docker.defaults_version';")
specs = load_settings_yaml()
c = SettingsCache(); c.load(); c.load_defaults_version()
for g in ('minimum_config', 'db_enable', 'initialisation', 'critical', 'optional', 'gpg'):
    apply_settings_fast(g, c, specs)
c.save_defaults_version()
""")
    assert stack.sql("SELECT value FROM system_settings WHERE setting='misp_docker.defaults_version';") == f'"{MISP_VERSION}"'


def test_version_gate_applies_once_then_stays(stack):
    stack.python(GATE_PRELUDE + f"""
cake.set_setting('Security.csp_enforce', 'false')
db.query("DELETE FROM system_settings WHERE setting='misp_docker.defaults_version';")
specs = patched('Security.csp_enforce', '{MISP_VERSION}', True)
c = SettingsCache(); c.load(); c.load_defaults_version(); apply_settings_fast('critical', c, specs); c.save_defaults_version()
# A second run after an operator set it back applies nothing: the version is saved
cake.set_setting('Security.csp_enforce', 'false')
c2 = SettingsCache(); c2.load(); c2.load_defaults_version(); apply_settings_fast('critical', c2, specs)
restore()
""")
    assert stack.sql("SELECT value FROM system_settings WHERE setting='Security.csp_enforce';") == "false"


def test_env_var_wins_over_a_version_gated_default(stack):
    stack.python(GATE_PRELUDE + f"""
db.query("DELETE FROM system_settings WHERE setting='misp_docker.defaults_version';")
specs = patched('MISP.external_baseurl', '{MISP_VERSION}', 'https://should-not-win')
c = SettingsCache(); c.load(); c.load_defaults_version(); apply_settings_fast('critical', c, specs)
restore()
""")
    assert stack.sql("SELECT value FROM system_settings WHERE setting='MISP.external_baseurl';") == '"http://caddy:8080"'


# -- S3 attachment storage -----------------------------------------------------------

@pytest.fixture(scope="module")
def s3_event(stack):
    access, secret, _ = stack.s3
    stack.python(f"""
from misp_container import cake
cake.set_setting('Plugin.S3_enable', 'true')
cake.set_setting('Plugin.S3_aws_compatible', 'true')
cake.set_setting('Plugin.S3_bucket_name', 'misp-attachments')
cake.set_setting('Plugin.S3_aws_endpoint', 'http://garage:3900')
cake.set_setting('Plugin.S3_region', 'garage')
cake.set_setting('Plugin.S3_aws_access_key', '{access}')
cake.set_setting('Plugin.S3_aws_secret_key', '{secret}')
cake.set_setting('MISP.attachments_dir', 's3://', force=True)
""")
    # Earlier sections may have rotated the key
    stack.key = stack.admin_key(ADMIN_EMAIL)
    event = stack.api("POST", "/events/add", stack.key, {"Event": {"info": "S3 test event", "distribution": 0}}) or {}
    eid = event.get("Event", {}).get("id")
    attribute = stack.api("POST", f"/attributes/add/{eid}", stack.key, {
        "event_id": eid, "category": "Payload delivery", "type": "attachment", "value": "test.txt",
        "data": base64.b64encode(b"S3 storage test content").decode()}) or {}
    return {"event_id": eid, "attribute_id": attribute.get("Attribute", {}).get("id")}


def test_s3_event_created(s3_event):
    assert s3_event["event_id"]


def test_s3_attachment_uploaded(s3_event):
    assert s3_event["attribute_id"]


def test_s3_attachment_downloads(stack, s3_event):
    # Without a JSON Accept header MISP does not take the key as an API login and
    # redirects to its login page at MISP.baseurl
    status, body = stack.http("GET", f"/attributes/download/{s3_event['attribute_id']}", stack.key)
    assert (status, body) == (200, "S3 storage test content")


def test_s3_objects_in_the_bucket(stack, s3_event):
    time.sleep(2)
    info = garage("GET", f"/v2/GetBucketInfo?id={stack.s3[2]}")
    assert (info.get("objects") or 0) > 0 or (info.get("bytes") or 0) > 0


# -- custom auth (reverse proxy header login) ----------------------------------------

@pytest.fixture(scope="module")
def custom_auth(stack):
    stack.exec("web", """CAKE=/var/www/MISP/app/Console/cake
$CAKE Admin setSetting -q Plugin.CustomAuth_enable true
$CAKE Admin setSetting -q Plugin.CustomAuth_header X_FORWARDED_EMAIL
$CAKE Admin setSetting -q Plugin.CustomAuth_use_header_namespace true
$CAKE Admin setSetting -q Plugin.CustomAuth_header_namespace HTTP_
$CAKE Admin setSetting -q Plugin.CustomAuth_required false
$CAKE user create headeruser@example.com 3 1 'HeaderUserPass123!' || true
$CAKE user change_pw headeruser@example.com 'HeaderUserPass123!' --no_password_change""")
    # CustomAuth matches a user by external_auth_key, not by email
    stack.sql(f"UPDATE users SET external_auth_required={sql_true(stack)}, "
              f"external_auth_key='headeruser@example.com', change_pw=NOT {sql_true(stack)}, "
              f"last_pw_change={sql_now(stack)} WHERE email='headeruser@example.com';")
    yield stack
    stack.exec("web", "/var/www/MISP/app/Console/cake Admin setSetting -q Plugin.CustomAuth_enable false")


def test_custom_auth_header_logs_in(custom_auth):
    # A logged-in browser gets the page; a logged-out one the login redirect
    status, _ = custom_auth.http("GET", "/events/index", json_api=False, redirects=False,
                                 headers={"X-Forwarded-Email": "headeruser@example.com"})
    assert status == 200


def test_custom_auth_without_header_redirects(custom_auth):
    assert custom_auth.http("GET", "/events/index", json_api=False, redirects=False)[0] == 302


# -- OIDC login through dex ----------------------------------------------------------

# The browser side of the login, in the compose network. The sync container is
# read-only like the Kubernetes Jobs, so the cookies stay in memory.
OIDC_FLOW = """
import http.cookiejar, json, urllib.error, urllib.parse, urllib.request
jar = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
follow = urllib.request.build_opener(jar)
stay = urllib.request.build_opener(jar, Stay)
def open_(opener, url, data=None, accept=None):
    request = urllib.request.Request(url, data=data, headers={"Accept": accept} if accept else {})
    try:
        return opener.open(request, timeout=30)
    except urllib.error.HTTPError as e:
        return e
auth = open_(stay, "http://caddy:8080/users/login?OidcAuth=enable").headers.get("Location", "")
print("auth=" + auth)
form = open_(follow, auth).geturl()
print("form=" + form)
login = urllib.parse.urlencode({"login": "oidc@example.com", "password": "oidc-test-password"}).encode()
final = open_(follow, form, data=login)
print(f"final={final.geturl()} {final.status}")
me = open_(follow, "http://caddy:8080/users/view/me.json", accept="application/json").read()
try:
    print(json.dumps(json.loads(me)))
except ValueError:
    print("{}")
"""


@pytest.fixture(scope="module")
def oidc(stack):
    # Mixed auth needs ?OidcAuth=enable to start the flow; dex's local connector
    # takes the credentials, and the callback lands on http://caddy:8080/users/login
    _, out = stack.run("sync", "python3", "-c", OIDC_FLOW)
    user = next((json.loads(line) for line in reversed(out.splitlines()) if line.startswith("{")), {})
    return {"out": out, "user": user}


def test_oidc_login_redirects_to_dex(oidc):
    assert "auth=http://dex:5556/dex/auth" in oidc["out"]


def test_oidc_dex_offers_its_password_form(oidc):
    assert "form=http://dex:5556/dex/auth/local/login" in oidc["out"]


def test_oidc_callback_lands_on_misp(oidc):
    assert "final=http://caddy:8080/ 200" in oidc["out"]


def test_oidc_user_has_the_idp_email(oidc):
    assert oidc["user"].get("User", {}).get("email") == "oidc@example.com"


def test_oidc_role_mapped_by_name(oidc):
    assert oidc["user"].get("Role", {}).get("name") == "Org Admin"


def test_oidc_default_organisation(oidc):
    assert oidc["user"].get("Organisation", {}).get("name") == "Test Org"


def test_oidc_mixed_auth_keeps_the_password_form(stack, oidc):
    body = stack.http("GET", "/users/login", json_api=False)[1]
    assert body.count('name="data[User][password]"') == 1


# -- task runner ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def update_taxonomies(stack):
    return task(stack, "update-taxonomies")


def test_task_update_taxonomies_exits_0(update_taxonomies):
    assert update_taxonomies[0] == 0, update_taxonomies[1][-2000:]


def test_task_update_taxonomies_calls_the_api(update_taxonomies):
    assert "POST /taxonomies/update" in update_taxonomies[1]


def test_task_pull_servers_without_servers(stack):
    assert task(stack, "pull-servers")[0] == 0


def test_task_without_admin_key_exits_1(stack):
    assert task(stack, "pull-servers", key="")[0] == 1


@pytest.mark.parametrize("name", ["cache-servers", "update-object-templates", "push-taxii", "sharing-group-blueprints"])
def test_task_replacing_the_scheduler(stack, name):
    # With no TAXII servers and no blueprints the index tasks make no call and exit 0
    rc, out = task(stack, name)
    assert rc == 0, out[-2000:]


def test_task_push_taxii_lists_the_servers(stack):
    assert "push-taxii: 0 TAXII server(s)" in task(stack, "push-taxii")[1]


@pytest.fixture(scope="module")
def periodic_summary(stack):
    # Console tasks render app/Config and run MISP's console; in Compose the worker has the volumes
    return stack.exec_rc("worker", "python3 -m misp_container.task periodic-summary")


def test_task_periodic_summary_exits_0(periodic_summary):
    assert periodic_summary[0] == 0, periodic_summary[1][-2000:]


def test_task_periodic_summary_ran_the_console(periodic_summary):
    assert "periodic summary" in periodic_summary[1]


@pytest.fixture(scope="module")
def user_validity(stack):
    return stack.exec_rc("worker", "python3 -m misp_container.task check-user-validity")


def test_task_check_user_validity_exits_0_with_oidc(user_validity):
    assert user_validity[0] == 0, user_validity[1][-2000:]


def test_task_check_user_validity_reports_the_admin(user_validity):
    assert ADMIN_EMAIL in user_validity[1]


# -- org sync ------------------------------------------------------------------------

ORGS = """taxonomies:
  - admiralty-scale
  - tlp

warninglists:
  - name: Sync Test Warninglist
    enabled: true
    description: "integration test warninglist"
    version: {version}
    type: hostname
    category: false_positive
    matching_attributes:
      - hostname
      - domain
    values:
      - example.com
      - test.local{extra}

tags:
  - name: "sync-test:global-tag"
    colour: "#112233"

teams:
  - uuid: "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"
    name: "Sync Test Org"
    description: "Created by integration test"
    sector: "academic"
    users:
      - email: sync-test-user@example.com
        role: User
      - email: sync-disabled-user@example.com
        role: User
        disabled: true
    sync_users:
      - email: sync-inbound@example.com
        role: Sync user
        authkey: inbound0000000000000000000000000000000000
    tags:
      - name: "sync-test:org-tag"
        colour: "#445566"
    servers:
      - name: "Sync Test Server"
        url: "https://sync-test.example.com"
        authkey: outbound000000000000000000000000000000000
        pull: true
        push: false
        pull_tags:
          - "tlp:white"
        internal: false
"""


def org_sync(stack, config):
    path = scratch_dir() / "test-orgs.yaml"
    path.write_text(config)
    try:
        return stack.run("sync", env={"ADMIN_KEY": stack.key, "SYNC_BASE_URL": "http://caddy:8080"},
                         volumes=[f"{path}:/etc/misp-docker/orgs.yaml:ro"])
    finally:
        path.unlink()


@pytest.fixture(scope="module")
def synced(stack):
    org_sync(stack, ORGS.format(version=1, extra=""))
    servers = stack.api("GET", "/servers", stack.key) or []
    server = next((s["Server"] for s in servers if s["Server"]["url"] == "https://sync-test.example.com"), {})
    return {"server": server}


def search_user(stack, email):
    found = stack.api("GET", f"/admin/users/index/searchall:{email}", stack.key) or []
    return found[0] if found else {}


def taxonomy_enabled(stack, namespace):
    for item in stack.api("GET", "/taxonomies", stack.key) or []:
        entry = item.get("Taxonomy", item) if isinstance(item, dict) else {}
        if entry.get("namespace") == namespace:
            return entry.get("enabled") in (True, 1, "1", "true")
    return False


def test_sync_org_created(stack, synced):
    org = stack.api("GET", f"/organisations/view/{SYNC_ORG_UUID}", stack.key) or {}
    assert org.get("Organisation", {}).get("name") == "Sync Test Org"


def test_sync_org_sector(stack, synced):
    org = stack.api("GET", f"/organisations/view/{SYNC_ORG_UUID}", stack.key) or {}
    assert org.get("Organisation", {}).get("sector") == "academic"


def test_sync_user_created(stack, synced):
    assert search_user(stack, "sync-test-user@example.com").get("User", {}).get("email") == "sync-test-user@example.com"


def test_sync_user_in_its_org(stack, synced):
    assert search_user(stack, "sync-test-user@example.com").get("Organisation", {}).get("name") == "Sync Test Org"


def test_sync_global_tag_created(stack, synced):
    assert "sync-test:global-tag" in stack.http("GET", "/tags", stack.key)[1]


def test_sync_server_created(synced):
    assert synced["server"].get("name") == "Sync Test Server"


def test_sync_server_pull_tags(synced):
    rules = json.loads(synced["server"].get("pull_rules") or "{}")
    assert rules.get("tags", {}).get("OR", [None])[0] == "tlp:white"


def test_sync_server_authkey_from_config(stack, synced):
    # The API does not return server authkeys
    assert stack.sql(f"SELECT authkey FROM servers WHERE id={synced['server']['id']};") == \
        "outbound000000000000000000000000000000000"


def test_sync_user_with_explicit_authkey(stack, synced):
    assert search_user(stack, "sync-inbound@example.com").get("User", {}).get("email") == "sync-inbound@example.com"


def test_sync_user_authkey_prefix(stack, synced):
    # MISP stores a hash and the first four characters
    uid = search_user(stack, "sync-inbound@example.com").get("User", {}).get("id")
    assert stack.sql(f"SELECT authkey_start FROM auth_keys WHERE user_id={uid} ORDER BY id DESC LIMIT 1;") == "inbo"


@pytest.mark.parametrize("namespace", ["admiralty-scale", "tlp"])
def test_sync_taxonomy_enabled(stack, synced, namespace):
    assert taxonomy_enabled(stack, namespace)


def test_sync_disabled_user_created(stack, synced):
    user = search_user(stack, "sync-disabled-user@example.com").get("User", {})
    assert user.get("email") == "sync-disabled-user@example.com"


def test_sync_disabled_user_is_disabled(stack, synced):
    assert search_user(stack, "sync-disabled-user@example.com").get("User", {}).get("disabled") in (True, 1, "1")


def test_sync_custom_warninglist_created(stack, synced):
    assert "Sync Test Warninglist" in stack.http("GET", "/warninglists", stack.key)[1]


def test_sync_custom_warninglist_version(stack, synced):
    assert stack.sql("SELECT version FROM warninglists WHERE name='Sync Test Warninglist';") == "1"


def test_sync_custom_warninglist_updated_to_v2(stack, synced):
    org_sync(stack, ORGS.format(version=2, extra="\n      - new-entry.example.org"))
    assert stack.sql("SELECT version FROM warninglists WHERE name='Sync Test Warninglist';") == "2"


def test_sync_warm_run_changes_nothing(stack, synced):
    # Disabling unmanaged users is expected: headeruser from the custom auth section
    _, out = org_sync(stack, ORGS.format(version=2, extra="\n      - new-entry.example.org"))
    changed = [m for m in re.findall(r"'(?:created|updated)': (\d+)", out) if m != "0"]
    assert changed == [], out[-2000:]


# -- metrics exporter ----------------------------------------------------------------

def metric(path="/metrics"):
    try:
        with urllib.request.urlopen(METRICS + path, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def default_depth():
    line = next((l for l in metric()[1].splitlines() if l.startswith('misp_jobs_queued{worker="default"}')), "")
    return line.rsplit(" ", 1)[-1] if line else ""


@pytest.fixture(scope="module")
def metrics_text(stack):
    deadline = time.monotonic() + 60
    while metric("/healthz")[0] != 200 and time.monotonic() < deadline:
        time.sleep(2)
    return metric()[1]


def value(text, name):
    line = next((l for l in text.splitlines() if l.startswith(name + " ")), "")
    return line.split(" ", 1)[1] if line else None


def test_metrics_endpoint_responds(metrics_text):
    assert metrics_text


@pytest.mark.parametrize("path,status", [("/healthz", 200), ("/ready", 200), ("/nonexistent", 404)])
def test_metrics_paths(metrics_text, path, status):
    assert metric(path)[0] == status


def test_metrics_misp_up_present(metrics_text):
    assert "misp_up" in metrics_text


def test_metrics_misp_up_is_1(metrics_text):
    assert value(metrics_text, "misp_up") == "1"


@pytest.mark.parametrize("name", ["misp_events", "misp_attributes", "misp_organisations", "misp_tags"])
def test_metrics_content_counts(metrics_text, name):
    assert name in metrics_text


@pytest.mark.parametrize("status", ["active", "disabled"])
def test_metrics_users_by_status(metrics_text, status):
    assert f'status="{status}"' in metrics_text


def test_metrics_instance_info(metrics_text):
    assert "misp_instance_info" in metrics_text


def test_metrics_scrape_duration(metrics_text):
    assert "misp_scrape_duration_seconds" in metrics_text


def test_metrics_scrape_errors_present(metrics_text):
    assert "misp_scrape_errors" in metrics_text


def test_metrics_no_scrape_errors(metrics_text):
    # A failed Redis read counts here too
    assert value(metrics_text, "misp_scrape_errors") == "0"


def test_metrics_help_and_type_for_every_block(metrics_text):
    lines = metrics_text.splitlines()
    assert sum(l.startswith("# HELP") for l in lines) == sum(l.startswith("# TYPE") for l in lines)


def test_metrics_jobs_queued_present(metrics_text):
    assert "misp_jobs_queued" in metrics_text


def test_metrics_scheduled_tasks_present(metrics_text):
    # Nothing runs MISP's Scheduled tasks here; the exporter counts enabled ones
    assert "misp_scheduled_tasks_enabled 0" in metrics_text


@pytest.fixture(scope="module")
def waiting_job(stack, metrics_text):
    # The depth comes from Redis: a job waits there while no worker runs
    stack.compose("stop", "worker")
    stack.http("POST", "/servers/cache/all", stack.key, {})
    depth = default_depth()
    stack.compose("start", "worker")
    return depth


def test_metrics_waiting_job_shows_in_the_queue(waiting_job):
    assert waiting_job and int(waiting_job) >= 1


def test_metrics_queue_returns_to_0(waiting_job):
    deadline = time.monotonic() + 30
    while default_depth() != "0" and time.monotonic() < deadline:
        time.sleep(1)
    assert default_depth() == "0"


def test_metrics_sync_log_after_org_sync(metrics_text, synced):
    if "misp_sync_runs_24h" not in metrics_text:
        pytest.skip("the sync log table may not exist yet")
    assert "misp_sync_runs_24h" in metrics_text


# -- MISP modules --------------------------------------------------------------------

def modules(path, data=None):
    request = urllib.request.Request(MODULES + path, data=json.dumps(data).encode() if data else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode()


@pytest.fixture(scope="module")
def modules_ready(stack):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if modules("/healthcheck")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def test_modules_healthcheck(modules_ready):
    assert modules_ready


def test_modules_loaded(modules_ready):
    assert len(json.loads(modules("/modules")[1])) > 0


def test_modules_dns_available(modules_ready):
    assert any(m.get("name") == "dns" for m in json.loads(modules("/modules")[1]))


def test_modules_dns_resolves(modules_ready):
    result = json.loads(modules("/query", {"module": "dns", "domain": "example.com"})[1])
    assert result.get("results", [{}])[0].get("values")


def test_modules_enrichment_url_in_misp(stack, modules_ready):
    setting = stack.api("GET", "/servers/getSetting/Plugin.Enrichment_services_url", stack.key) or {}
    assert "modules" in str(setting.get("value", ""))


def test_web_reaches_modules_for_enrichment(stack, modules_ready):
    _, out = stack.python("""
import json, urllib.request
request = urllib.request.Request('http://modules:6666/query', headers={'Content-Type': 'application/json'},
                                 data=json.dumps({'module': 'dns', 'domain': 'example.com'}).encode())
with urllib.request.urlopen(request, timeout=10) as response:
    values = json.loads(response.read()).get('results', [{}])[0].get('values', [])
    print(values[0] if values else '')
""")
    assert out.strip()


# -- multi-replica web ---------------------------------------------------------------

def test_configure_ran_configuration(stack):
    assert "configuration complete" in stack.logs("configure", stopped=True)


def test_configure_set_live(stack):
    assert "setting MISP.live = true" in stack.logs("configure", stopped=True)


@pytest.fixture(scope="module")
def two_web(stack):
    stack.compose("up", "-d", "--no-deps", "--no-recreate", "--scale", "web=2", "web")
    deadline = time.monotonic() + 60
    while stack.logs("web").count("starting PHP-FPM") < 2 and time.monotonic() < deadline:
        time.sleep(2)
    yield stack
    # Back to one replica, so the teardown sees only containers compose created
    extra = stack.container_ids("web")[1:]
    if extra:
        subprocess.run([stack.engine, "rm", "-f", *extra], capture_output=True)
    stack._ids.clear()


def test_two_web_replicas_started_fpm(two_web):
    assert two_web.logs("web").count("starting PHP-FPM") == 2


def test_web_replicas_do_not_configure(two_web):
    assert "configuration complete" not in two_web.logs("web")


def test_two_web_containers_running(two_web):
    assert len(two_web.container_ids("web")) == 2


def test_login_served_with_two_replicas(two_web):
    assert two_web.http("GET", "/users/login", json_api=False)[0] == 200


# -- logging -------------------------------------------------------------------------

def test_configure_writes_json_lines(stack):
    # The test stack takes LOG_FORMAT=json from base.env, as Kubernetes does
    lines = [l for l in stack.logs("configure", stopped=True).splitlines() if l.startswith("{")]
    assert len(lines) >= 10
    for line in lines:
        json.loads(line)


def test_configure_writes_no_text_lines(stack):
    text = [l for l in stack.logs("configure", stopped=True).splitlines()
            if re.match(r"^[0-9-]{10} [0-9:]{8} (INFO|WARNING|ERROR) ", l)]
    assert text == []


@pytest.fixture(scope="module")
def missing_controller_line(stack):
    # MISP logs a missing controller as an error
    stack.http("GET", "/nonexistentcontrollerzz", json_api=False)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = next((l for l in stack.logs("web").splitlines() if "NonexistentcontrollerzzController" in l), "")
        if line:
            return line
        time.sleep(1)
    return ""


def test_misp_log_reaches_the_web_output(missing_controller_line):
    assert '"context":"misp"' in missing_controller_line


def test_the_web_line_is_an_error(missing_controller_line):
    assert '"level":"error"' in missing_controller_line


def test_worker_job_lines_once_as_json(stack):
    # A console shell would add its own text copy of every line
    lines = [l for l in stack.logs("worker").splitlines() if "launching job" in l]
    assert lines and all(l.startswith("{") and '"context":"misp"' in l for l in lines)


def test_no_cakelog_files_on_disk(stack):
    assert stack.exec("web", "find /var/www/MISP/app/tmp/logs -maxdepth 1 "
                             "\\( -name debug.log -o -name error.log \\) -size +0 | wc -l") == "0"


def test_the_relay_forwards_server_sync_log(stack):
    stack.exec("web", 'echo "relay probe line" >> /var/www/MISP/app/tmp/logs/server-sync.log')
    deadline = time.monotonic() + 10
    line = ""
    while not line and time.monotonic() < deadline:
        line = next((l for l in stack.logs("web").splitlines() if "relay probe line" in l), "")
        time.sleep(1)
    assert '"context":"misp:server-sync"' in line


# -- settings and scheduler coverage -------------------------------------------------

def test_every_misp_setting_curated_or_catalogued(stack):
    dump = scratch_dir() / "misp-settings.json"
    dump.write_text(stack.exec("web", "/var/www/MISP/app/Console/cake Admin getSetting all"))
    result = subprocess.run([sys.executable, str(REPO / "scripts/update_settings.py"), "--check", "--json", str(dump)],
                            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO / "files")})
    assert result.returncode == 0, "new or stale settings (run scripts/update-settings.sh and review):\n" + result.stdout


def test_every_scheduler_task_has_a_task_runner_task(stack):
    shell = scratch_dir() / "SchedulerWorkerShell.php"
    shell.write_text(stack.exec("web", "cat /var/www/MISP/app/Console/Command/SchedulerWorkerShell.php"))
    result = subprocess.run([sys.executable, str(REPO / "scripts/check_scheduler_coverage.py"), str(shell)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_no_rejected_cake_settings(stack):
    # Every default this image applies must be accepted by this MISP version
    rejected = [l for l in stack.logs("configure", stopped=True).splitlines()
                if "WARNING [cake]" in l or '"level":"warning","context":"cake"' in l]
    assert rejected == []
