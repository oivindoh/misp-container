"""Integration suite: one release of the chart on k3s, checked feature by feature.

    pytest tests/e2e/test_integration.py [--skip-build] [--keep]

Beside the release run Keycloak for the OIDC login, and PostgreSQL and Redis
outside the chart, as a database operator runs them (tests/k3s/external.yaml).
The tests run in file order on one release. Later sections depend on the state
earlier ones leave: custom auth creates a user that the org sync warm run
disables, the metrics section scales the workers to none and back, and the
multi-replica section scales web to two replicas and back.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request

import pytest

import keycloak
import manifests
from cluster import Cluster, build_images
from misp_container import WORKER_QUEUES
from misp_container.logrelay import FILES as RELAYED
from stack import K3S, REPO, VALUES, Release, gnupg_key, merge, server_certificate, values_file

sys.path.insert(0, str(REPO / "scripts"))
from check_upstream import NOT_RELAYED  # noqa: E402

TEST_ENV = values_file(VALUES)["env"]
ADMIN_EMAIL = TEST_ENV["ADMIN_EMAIL"]
# Host port: node port. web, metrics, modules
PORTS = {28080: 30080, 19191: 30191, 16666: 30666}
METRICS = "http://localhost:19191"
MODULES = "http://localhost:16666"
SYNC_ORG_UUID = "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"
# The external PostgreSQL in the release's namespace, misp, and the keys of its user Secret
PG_HOST = "pg-primary.misp.svc"
# The passwords hold the characters that need quoting in a URL, PHP, SQL or a shell
PG_USER = {"host": PG_HOST, "port": "5432", "dbname": "misp", "user": "misp", "password": "pg'\"$\\@:/?#&%-pw"}
DB_CA = "/etc/misp-docker/db-ca/ca.crt"
# A setting whose value differs from MISP's default, so a job that misses the env layer shows
TITLE = "Integration MISP"
# Header login from a reverse proxy; required false keeps the password form and the API keys
CUSTOM_AUTH = {"env": {"CUSTOM_AUTH_ENABLE": "true", "CUSTOM_AUTH_HEADER": "X_FORWARDED_EMAIL",
                       "CUSTOM_AUTH_USE_HEADER_NAMESPACE": "true", "CUSTOM_AUTH_HEADER_NAMESPACE": "HTTP_",
                       "CUSTOM_AUTH_REQUIRED": "false"}}


@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    build_images(options)
    cluster = Cluster("misp-integration", PORTS)
    cluster.start()
    cluster.load_images_under_test()
    s = Release(cluster, values=merge(merge(merge(values_file(K3S / "oidc.yaml"), values_file(K3S / "external.yaml")),
                                            CUSTOM_AUTH), {"env": {"MISP_TITLE_TEXT": TITLE}}))
    s.apply(gnupg_key(ADMIN_EMAIL, s.values["secrets"]["app"]["GNUPG_PASSWORD"]), *manifests.keycloak(), *manifests.postgres_tls(server_certificate("pg-primary", PG_HOST), PG_USER),
            *manifests.external_redis("ext'\"$\\@:/?#&%-pw"),
            manifests.nodeport("web", "web", {8080: 30080}), manifests.nodeport("metrics", "metrics", {9191: 30191}),
            manifests.nodeport("modules", "modules", {6666: 30666}))
    s.install()
    s.rollout("keycloak")
    s.wait_for_misp()
    s.key = s.admin_key(ADMIN_EMAIL)
    yield s
    cluster.finish("integration", options["keep"], failed_in_module())


def me(stack, key=None):
    return stack.api("GET", "/users/view/me", key or stack.key) or {}


def setting(stack, name):
    return (stack.api("GET", f"/servers/getSetting/{name}", stack.key) or {}).get("value")


# -- non-root operation --------------------------------------------------------------

@pytest.mark.parametrize("service", ["web", "worker"])
def test_runs_as_uid_1000(stack, service):
    assert stack.exec(service, "id -u") == "1000"


def test_caddy_runs_as_uid_1000(stack):
    assert stack.exec("caddy", "id -u") == "1000"


# -- HTTP and caddy ------------------------------------------------------------------

def test_caddy_proxies_fpm_ping(stack):
    assert stack.http("GET", "/fpm-ping", json_api=False)[1].strip() == "pong"


def test_the_ping_pool_is_closed_to_a_client_behind_a_proxy(stack):
    # The probes' pool has one child: a client behind the Gateway, which sends X-Forwarded-For, never reaches it
    status, body = stack.http("GET", "/fpm-ping", json_api=False, headers={"X-Forwarded-For": "203.0.113.9"})
    assert body.strip() != "pong", status


def test_login_page(stack):
    assert stack.http("GET", "/users/login", json_api=False)[0] == 200


def test_caddy_serves_static_css(stack):
    assert stack.http("GET", "/css/main.css", json_api=False)[0] == 200


def test_root_redirects_to_login(stack):
    assert stack.http("GET", "/", json_api=False, redirects=False)[0] == 302


def test_a_forged_forwarded_for_stays_out_of_the_audit_log(stack):
    # A failed API login is logged with the client address MISP reads from X-Forwarded-For
    stack.http("GET", "/users/view/me", "0" * 40, headers={"X-Forwarded-For": "203.0.113.9"})
    logged = stack.sql("SELECT ip FROM logs WHERE action = 'auth_fail' ORDER BY id DESC LIMIT 1")
    assert logged and logged != "203.0.113.9", (
        f"MISP logged {logged!r}: caddy passed on a client's X-Forwarded-For (TRUSTED_PROXY_CIDR)")


# -- admin user configuration --------------------------------------------------------

def test_admin_email_from_env(stack):
    assert me(stack).get("User", {}).get("email") == ADMIN_EMAIL


def test_admin_has_no_forced_password_change(stack):
    assert me(stack).get("User", {}).get("change_pw") is False


def test_admin_org_name(stack):
    assert me(stack).get("Organisation", {}).get("name") == "Test Org"


def test_admin_org_uuid(stack):
    assert me(stack).get("Organisation", {}).get("uuid") == TEST_ENV["ADMIN_ORG_UUID"]


def test_the_admin_org_is_the_host_org(stack):
    assert str(setting(stack, "MISP.host_org_id")) == str(me(stack)["User"]["org_id"])


def test_a_host_org_user_without_site_admin_passes_the_host_org_check(stack):
    # patches/04: the ACL reads the host organisation after MISP's settings layers
    org_id = me(stack)["User"]["org_id"]
    user = stack.api("POST", "/admin/users/add", stack.key, {"User": {
        "email": "host-org-user@example.com", "org_id": org_id, "role_id": 3,
        "password": "Host-0rg!Pass#2026"}}) or {}
    uid = user.get("User", {}).get("id")
    assert uid, user
    key = (stack.api("POST", f"/auth_keys/add/{uid}", stack.key, {}) or {})["AuthKey"]["authkey_raw"]
    status, body = stack.http("GET", "/event_blocklists/index", key)
    stack.http("POST", f"/admin/users/delete/{uid}", stack.key, {})
    assert status == 200, body[:300]


def test_misp_org_is_the_admin_org(stack):
    assert setting(stack, "MISP.org") == TEST_ENV["ADMIN_ORG"]


def test_last_pw_change_is_set(stack):
    assert "NULL" not in stack.sql("SELECT last_pw_change FROM users WHERE id=1;")


# -- settings from the repository ----------------------------------------------------

def test_misp_serves_the_baseurl_from_its_env_layer(stack):
    assert setting(stack, "MISP.baseurl") == TEST_ENV["MISP_BASEURL"]


def test_misp_refuses_a_change_to_a_repository_setting(stack):
    status, body = stack.http("POST", "/servers/serverSettingsEdit/MISP.title_text", stack.key, {"value": "by hand"})
    assert status == 403 and "environment variable" in body, body


def test_the_database_holds_no_row_of_a_repository_setting(stack):
    _, out = stack.python("""
from misp_container import db
from misp_container.config import repository_settings
carried = sorted(repository_settings())
print(len(carried) > 100, [r['setting'] for r in db.dict_query(
    'SELECT setting FROM system_settings WHERE setting = ANY(%s)', (carried,))])
""")
    assert out == "True []"


# -- workers -------------------------------------------------------------------------

def worker_processes(stack):
    """{queue: [pid]} of the PHP workers in the worker container."""
    found = {}
    for line in stack.exec("worker", "ps -eo pid=,args=").splitlines():
        pid, _, args = line.strip().partition(" ")
        queue = re.search(r"start_worker (\w+)$", args)
        if queue:
            found.setdefault(queue.group(1), []).append(int(pid))
    return found


@pytest.fixture(scope="module")
def workers(stack):
    """{queue: [pid]} once every queue runs its NUM_WORKERS_<QUEUE> processes."""
    expected = {queue: int(stack.exec("worker", f"printenv NUM_WORKERS_{queue.upper()}")) for queue in WORKER_QUEUES}
    deadline = time.monotonic() + 120
    while True:
        found = worker_processes(stack)
        if all(len(found.get(queue, [])) == n for queue, n in expected.items()) or time.monotonic() > deadline:
            return found
        time.sleep(2)


@pytest.mark.parametrize("queue", WORKER_QUEUES)
def test_worker_queue_running(stack, workers, queue):
    expected = int(stack.exec("worker", f"printenv NUM_WORKERS_{queue.upper()}"))
    assert len(workers.get(queue, [])) == expected


def test_no_scheduler_worker(workers):
    # Periodic work belongs to the task runner; MISP's own scheduler never runs
    assert "scheduler" not in workers


def test_a_killed_worker_is_started_again(stack, workers):
    pid = workers["default"][0]
    stack.exec("worker", f"kill -9 {pid}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        now = worker_processes(stack).get("default", [])
        if pid not in now and len(now) == len(workers["default"]):
            return
        time.sleep(1)
    raise AssertionError(f"worker {pid} of the default queue was not replaced in 30 s")


# -- background jobs -----------------------------------------------------------------

def test_a_published_event_runs_a_job(stack):
    event = stack.api("POST", "/events/add", stack.key, {"Event": {"info": "Background job test", "distribution": 0}}) or {}
    eid = event.get("Event", {}).get("id")
    assert eid, event
    stack.http("POST", f"/events/publish/{eid}", stack.key, {})
    query = f"SELECT status FROM jobs WHERE job_type = 'publish_event' AND job_input = 'Event ID: {int(eid)}'"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and stack.sql(query) != "4":
        time.sleep(2)
    status = stack.sql(query)
    stack.http("POST", f"/events/delete/{eid}", stack.key, {})
    assert status == "4", f"the publish job of event {eid} has status {status or 'none'}, not 4 (completed), after 60 s"


def test_a_background_job_reads_the_env_layer(stack):
    # patches/02: MISP starts a job with an environment of its own; the job keeps the worker's
    rc, out = stack.python("""
import json, time, uuid
from misp_container import metrics
client, ns = metrics.job_redis()
job = {"id": str(uuid.uuid4()), "command": "admin", "args": ["getSetting", "MISP.title_text"], "metadata": {}}
client.rpush(f"{ns}:default", json.dumps(job))
for _ in range(90):
    found = json.loads(client.get(f"{ns}:job_status:{job['id']}") or "{}")
    if found.get("status") in (3, 4):
        print(found.get("output", ""))
        break
    time.sleep(1)
""")
    assert rc == 0 and TITLE in out, out[-2000:]


# A feed that answers its manifest after FEED_DELAY seconds, from the toolbox: a job of known length
FEED_DELAY = 15
SLOW_FEED = f"""
import http.server, time
class Feed(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        open("/tmp/slow-feed-hit", "w").close()
        time.sleep({FEED_DELAY})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{{}}")
http.server.HTTPServer(("", 8765), Feed).serve_forever()
"""


def redis_job_status(stack, process_id):
    """The status the worker recorded for a background job in Redis: 3 failed, 4 completed; "" while none."""
    rc, out = stack.python("import json, os\n"
                           "from misp_container import metrics\n"
                           "client, ns = metrics.job_redis()\n"
                           "job = client.get(f\"{ns}:job_status:{os.environ['JOB']}\")\n"
                           "print(json.loads(job)['status'] if job else '')", env={"JOB": process_id})
    assert rc == 0, out
    return out.strip()


def test_a_stopping_worker_finishes_its_running_job(stack):
    # patches/03: on SIGTERM the worker takes no new job and waits for the running one
    stack.exec_rc("toolbox", 'rm -f /tmp/slow-feed-hit; nohup python3 -c "$CODE" > /tmp/slow-feed.log 2>&1 &',
                  env={"CODE": SLOW_FEED})
    address = stack.kubectl("get", "pod", "toolbox", "-o", "jsonpath={.status.podIP}").stdout.strip()
    feed = stack.api("POST", "/feeds/add", stack.key, {"Feed": {
        "name": "slow feed", "provider": "the integration suite", "url": f"http://{address}:8765",
        "source_format": "misp", "input_source": "network", "enabled": True, "distribution": 0}}) or {}
    fid = feed.get("Feed", {}).get("id")
    assert fid, feed
    stack.http("POST", f"/feeds/fetchFromFeed/{fid}", stack.key, {})
    stack.wait_for("toolbox", "test -e /tmp/slow-feed-hit", timeout=60)
    stack.kubectl("delete", "pod", "-l", "app.kubernetes.io/name=worker", "--wait=false")
    process_id = stack.sql(f"SELECT process_id FROM jobs WHERE job_type = 'fetch_feeds' AND job_input = 'Feed: {int(fid)}'")
    deadline = time.monotonic() + FEED_DELAY + 60
    status = ""
    while time.monotonic() < deadline and status not in ("3", "4"):
        time.sleep(2)
        status = redis_job_status(stack, process_id)
    stack.rollout("worker")
    stack.http("POST", f"/feeds/delete/{fid}", stack.key, {})
    assert status == "4", f"the job of the stopped worker ended with status {status or 'none'}, not 4 (completed)"


# -- MISP's own Python scripts -------------------------------------------------------

@pytest.fixture(scope="module")
def stix_event(stack):
    event = stack.api("POST", "/events/add", stack.key, {"Event": {"info": "STIX export test", "distribution": 0}}) or {}
    eid = event.get("Event", {}).get("id")
    assert eid, event
    stack.api("POST", f"/attributes/add/{eid}", stack.key, {
        "event_id": eid, "category": "Network activity", "type": "ip-dst", "value": "198.51.100.7"})
    yield eid
    stack.http("POST", f"/events/delete/{eid}", stack.key, {})


def test_stix2_export_runs_misp_stix(stack, stix_event):
    # MISP runs app/files/scripts/stix2/misp2stix2.py, which imports misp-stix
    status, body = stack.http("POST", "/events/restSearch", stack.key, {"returnFormat": "stix2", "eventid": stix_event})
    assert status == 200 and '"bundle"' in body and "198.51.100.7" in body, body[-2000:]


def test_stix1_export_runs_python_stix(stack, stix_event):
    # MISP runs app/files/scripts/misp2stix.py, which imports python-stix and cybox; a JSON
    # request gets the package as STIX 1 JSON
    status, body = stack.http("POST", "/events/restSearch", stack.key, {"returnFormat": "stix", "eventid": stix_event})
    assert status == 200 and ":STIXPackage-" in body and "198.51.100.7" in body, body[-2000:]


# -- PHP-FPM -------------------------------------------------------------------------

def test_fpm_listens_on_the_loopback_only(stack):
    # 127.0.0.1:9002 is 0100007F:232A in /proc/net/tcp; no address of the pod's
    listening = [line.split()[1] for line in stack.exec("web", "cat /proc/net/tcp /proc/net/tcp6").splitlines()
                 if line.split()[3:4] == ["0A"]]
    assert "0100007F:232A" in listening and not [a for a in listening if a.endswith(":232A") and a != "0100007F:232A"]


def test_fpm_loads_debians_production_ini_and_ours(stack):
    # PHP-FPM reads Debian's production php.ini, then php.d through PHP_INI_SCAN_DIR (web.py);
    # the CLI loads the same files here, and its startup notices go to stderr
    rc, out = stack.exec_rc("web", "PHP_INI_SCAN_DIR=:/etc/misp-docker/php.d php -c /etc/php/8.4/fpm/php.ini "
                                   "-r 'var_dump(ini_get(\"session.upload_progress.enabled\"), "
                                   "ini_get(\"zend.exception_ignore_args\"));' 2>/dev/null")
    assert (rc, out.split()) == (0, ['string(1)', '"0"', 'string(1)', '"1"']), out


@pytest.mark.parametrize("extension", ["rdkafka", "zstd", "brotli", "simdjson", "ssdeep"])
def test_php_loads_the_extension(stack, extension):
    # PHP skips an extension it cannot load with a warning and runs on without it
    assert extension in stack.exec("web", "php -m 2>/dev/null").split()


@pytest.mark.parametrize("extension", ["brotli", "zstd"])
def test_compression_extensions_link_debians_library(stack, extension):
    # A bundled copy of the library escapes Debian's security fixes and Trivy, and MISP
    # decodes compressed request bodies before authentication
    assert "bundled" not in stack.exec("web", f"php --ri {extension} 2>/dev/null").lower()


def test_simdjson_has_the_encoder_misp_calls(stack):
    # MISP's JsonTool encodes with simdjson_encode when it exists, else with json_encode
    assert stack.exec("web", "php -r 'var_dump(function_exists(\"simdjson_encode\"));' 2>/dev/null") == "bool(true)"


# -- distribution files and app/Config rendering -------------------------------------

def test_tlp_taxonomy_ships_in_the_image(stack):
    assert "tlp" in stack.exec("web", "ls /var/www/MISP/app/files/taxonomies/").split()


def test_bootstrap_is_the_upstream_default_and_the_log_block(stack):
    _, out = stack.python("""
from pathlib import Path
from misp_container import CONFIG_DEFAULTS
from misp_container.init import LOG_BLOCK_START, LOG_BLOCK_END
rendered = Path('/var/www/MISP/app/Config/bootstrap.php').read_text()
start, end = rendered.index(LOG_BLOCK_START), rendered.index(LOG_BLOCK_END) + len(LOG_BLOCK_END)
default = (Path(CONFIG_DEFAULTS) / 'bootstrap.default.php').read_text()
print((rendered[:start] + rendered[end:]).strip() == default.strip())
""")
    assert out == "True", f"bootstrap.php differs from MISP's bootstrap.default.php beyond the logging block: {out}"


def test_database_php_points_at_the_external_postgresql(stack):
    config = stack.exec("web", "cat /var/www/MISP/app/Config/database.php")
    assert "Database/PostgresObserverExtended" in config and f"'host' => '{PG_HOST}'," in config
    assert f"'sslmode' => 'verify-full;sslrootcert={DB_CA}'," in config


def test_misp_reads_the_external_redis(stack):
    assert setting(stack, "MISP.redis_host") == "external-redis"


# -- external PostgreSQL ---------------------------------------------------------------

def test_the_database_user_is_no_superuser(stack):
    assert stack.sql("SELECT CASE WHEN rolsuper THEN 'yes' ELSE 'no' END FROM pg_roles "
                     "WHERE rolname = current_user") == "no"


def test_the_database_refuses_plain_text(stack):
    rc, out = stack.python("from misp_container import db\ndb.dict_query('SELECT 1')",
                           env={"DB_SSLMODE": "disable"})
    assert rc != 0 and "no encryption" in out, out


def test_the_connection_checks_the_server_certificate(stack):
    # The system's CAs never signed the test CA's certificate
    rc, out = stack.python("from misp_container import db\ndb.dict_query('SELECT 1')",
                           env={"DB_SSLROOTCERT": ""})
    assert rc != 0 and "CERTIFICATE_VERIFY_FAILED" in out, out


# -- GPG -----------------------------------------------------------------------------

def test_gpg_key_imported(stack):
    assert "trustdb.gpg" in stack.exec("web", "ls /var/www/MISP/.gnupg/")


# -- MISP API ------------------------------------------------------------------------

def test_version_reported(stack):
    shipped = json.loads(stack.exec("web", "cat /var/www/MISP/VERSION.json"))
    expected = f"{shipped['major']}.{shipped['minor']}.{shipped['hotfix']}"
    assert (stack.api("GET", "/servers/getVersion.json", stack.key) or {}).get("version", "").startswith(expected)


def test_event_created_through_the_api(stack):
    event = stack.api("POST", "/events/add", stack.key, {"Event": {"info": "Integration test event", "distribution": 0}}) or {}
    eid = event.get("Event", {}).get("id")
    assert eid, event
    stack.http("POST", f"/events/delete/{eid}", stack.key, {})


# -- custom auth (reverse proxy header login) ----------------------------------------

@pytest.fixture(scope="module")
def custom_auth(stack):
    # The release turns custom auth on (CUSTOM_AUTH)
    stack.exec("web", """CAKE=/var/www/MISP/app/Console/cake
$CAKE user create headeruser@example.com 3 1 'HeaderUserPass123!' || true
$CAKE user change_pw headeruser@example.com 'HeaderUserPass123!' --no_password_change""")
    # CustomAuth matches a user by external_auth_key, not by email
    stack.sql("UPDATE users SET external_auth_required=TRUE, external_auth_key='headeruser@example.com', "
              "change_pw=FALSE, last_pw_change=EXTRACT(EPOCH FROM NOW())::bigint "
              "WHERE email='headeruser@example.com';")
    return stack


def test_custom_auth_header_logs_in(custom_auth):
    # A logged-in browser gets the page; a logged-out one the login redirect
    status, _ = custom_auth.http("GET", "/events/index", json_api=False, redirects=False,
                                 headers={"X-Forwarded-Email": "headeruser@example.com"})
    assert status == 200


def test_custom_auth_without_header_redirects(custom_auth):
    assert custom_auth.http("GET", "/events/index", json_api=False, redirects=False)[0] == 302


# -- OIDC login through Keycloak -----------------------------------------------------

@pytest.fixture(scope="module")
def oidc(stack):
    # The team of the user's Keycloak organisation, named after its alias
    stack.api("POST", "/admin/organisations/add", stack.key, {"Organisation": {"name": "oidc-team", "local": True}})
    return keycloak.login(stack, "oidc", "oidc-test-password")


def test_oidc_login_redirects_to_keycloak(oidc):
    assert "auth=http://keycloak:8080/realms/misp/protocol/openid-connect/auth" in oidc["out"]


def test_oidc_keycloak_offers_its_login_form(oidc):
    assert "form=http://keycloak:8080/realms/misp/login-actions/authenticate" in oidc["out"]


def test_oidc_callback_lands_on_misp(oidc):
    assert "final=http://web:8080/ 200" in oidc["out"]


def test_oidc_user_has_the_idp_email(oidc):
    assert oidc["user"].get("User", {}).get("email") == "oidc@example.com"


def test_oidc_highest_client_role_wins(oidc):
    # The user holds the client roles user and org-admin
    assert oidc["user"].get("Role", {}).get("name") == "Org Admin"


def test_oidc_organisation_from_the_keycloak_organisation(oidc):
    # The organization claim lists the alias of the user's Keycloak organisation; no default
    # organisation is set
    assert oidc["user"].get("Organisation", {}).get("name") == "oidc-team"


def test_oidc_mixed_auth_keeps_the_password_form(stack, oidc):
    body = stack.http("GET", "/users/login", json_api=False)[1]
    assert body.count('name="data[User][password]"') == 1


# -- task runner ---------------------------------------------------------------------

def task(stack, *args):
    """A task through MISP's console, in the toolbox, as its CronJob runs it."""
    return stack.run("python3", "-m", "misp_container.task", *args)


@pytest.fixture(scope="module")
def update_taxonomies(stack):
    return task(stack, "update-taxonomies")


def test_task_update_taxonomies_exits_0(update_taxonomies):
    assert update_taxonomies[0] == 0, update_taxonomies[1][-2000:]


def test_task_update_taxonomies_ran_the_console(update_taxonomies):
    assert "cake Admin updateTaxonomies" in update_taxonomies[1]


@pytest.mark.parametrize("name", ["pull-servers", "push-servers", "cache-servers", "cache-feeds", "fetch-feeds",
                                  "update-object-templates", "push-taxii"])
def test_task_replacing_the_scheduler(stack, name):
    # No server, feed or TAXII server is set up: each console command has nothing to do
    rc, out = task(stack, name)
    assert rc == 0, out[-2000:]


@pytest.fixture(scope="module")
def periodic_summary(stack):
    return task(stack, "periodic-summary")


def test_task_periodic_summary_exits_0(periodic_summary):
    assert periodic_summary[0] == 0, periodic_summary[1][-2000:]


def test_task_periodic_summary_ran_the_console(periodic_summary):
    assert "periodic summary" in periodic_summary[1]


@pytest.fixture(scope="module")
def user_validity(stack):
    return task(stack, "check-user-validity")


def test_task_check_user_validity_exits_0_with_oidc(user_validity):
    assert user_validity[0] == 0, user_validity[1][-2000:]


def test_task_check_user_validity_reports_the_admin(user_validity):
    assert ADMIN_EMAIL in user_validity[1]


@pytest.fixture(scope="module")
def blocked(stack, oidc, user_validity):
    return task(stack, "block-invalid-users")


def test_task_block_invalid_users_keeps_the_users_keycloak_backs(stack, blocked):
    # The task disables what the provider no longer backs: a wrong answer would lock people out
    assert blocked[0] == 0, blocked[1][-2000:]
    states = stack.sql("SELECT string_agg(disabled::text, ',') FROM users WHERE sub IS NOT NULL AND sub <> ''")
    assert states and "true" not in states, blocked[1][-2000:]


def test_php_console_sessions_go_to_tmp(stack):
    # Some console commands start a session; the root is read-only, /tmp is a volume
    rc, out = stack.exec_rc("worker", "php -r 'session_start(); echo session_save_path();'")
    assert (rc, out) == (0, "/tmp"), out


def test_task_check_user_validity_lines_are_not_nested(user_validity):
    # MISP's own log lines pass through as they are, not inside another entry
    nested = [line for line in user_validity[1].splitlines() if '\\"context\\":\\"misp\\"' in line]
    assert not nested, nested[:3]


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

# No UUID: the runs find the group by its name
sharing_groups:
  - name: "Sync Test Sharing Group"
    releasability: "Sync test partners"
    description: "Created by integration test"
    organisations:
      - "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"

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
      - email: sync-inbound@example.com
        role: Sync user
        authkey: inbound0000000000000000000000000000000000
      - email: break-glass@example.com
        role: Org Admin
        password: "Br3ak-Glass!Pass#2026"
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


# A password login through the form, as a break-glass account makes it while
# Keycloak is down; the CakePHP form wants its hidden token fields back
LOGIN_FLOW = """
import http.cookiejar, json, re, sys, urllib.parse, urllib.request
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
page = opener.open("http://web:8080/users/login", timeout=30).read().decode()
form = next(f for f in re.findall(r"<form.*?</form>", page, re.S) if "data[User][password]" in f)
fields = {}
for tag in re.findall(r"<input[^>]*>", form):
    attrs = dict(re.findall(r'(\\w+)="([^"]*)"', tag))
    if attrs.get("type") == "hidden" and "name" in attrs:
        fields[attrs["name"]] = attrs.get("value", "")
fields.update({"data[User][email]": sys.argv[1], "data[User][password]": sys.argv[2]})
opener.open("http://web:8080/users/login", urllib.parse.urlencode(fields).encode(), timeout=30)
me = opener.open(urllib.request.Request("http://web:8080/users/view/me.json",
                                        headers={"Accept": "application/json"}), timeout=30).read()
try:
    print(json.dumps(json.loads(me)))
except ValueError:
    print("{}")
"""


@pytest.fixture(scope="module")
def synced(stack, oidc):
    # A local user the file does not name, beside the OIDC user of the login above
    stack.api("POST", "/admin/users/add", stack.key, {"User": {
        "email": "unnamed-local@example.com", "org_id": 1, "role_id": 3, "password": "Unnamed-L0cal#2026"}})
    rc, out = stack.org_sync(ORGS.format(version=1, extra=""), stack.key)
    servers = stack.api("GET", "/servers", stack.key) or []
    server = next((s["Server"] for s in servers if s["Server"]["url"] == "https://sync-test.example.com"), {})
    return {"rc": rc, "out": out, "server": server}


def search_user(stack, email):
    found = stack.api("GET", f"/admin/users/index/searchall:{email}", stack.key) or []
    return found[0] if found else {}


def taxonomy_enabled(stack, namespace):
    for item in stack.api("GET", "/taxonomies", stack.key) or []:
        entry = item.get("Taxonomy", item) if isinstance(item, dict) else {}
        if entry.get("namespace") == namespace:
            return entry.get("enabled") in (True, 1, "1", "true")
    return False


def sharing_groups(stack, name):
    found = stack.api("GET", "/sharing_groups", stack.key) or {}
    return [sg for sg in found.get("response", []) if sg.get("SharingGroup", {}).get("name") == name]


def test_sync_exits_0(synced):
    assert synced["rc"] == 0, synced["out"][-3000:]


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


def test_sync_user_authkey_prefix(stack, synced):
    # MISP stores a hash and the first four characters
    uid = search_user(stack, "sync-inbound@example.com").get("User", {}).get("id")
    assert stack.sql(f"SELECT authkey_start FROM auth_keys WHERE user_id={uid} ORDER BY id DESC LIMIT 1;") == "inbo"


@pytest.mark.parametrize("namespace", ["admiralty-scale", "tlp"])
def test_sync_taxonomy_enabled(stack, synced, namespace):
    assert taxonomy_enabled(stack, namespace)


def test_sync_disabled_user_is_disabled(stack, synced):
    assert search_user(stack, "sync-disabled-user@example.com").get("User", {}).get("disabled") in (True, 1, "1")


def test_sync_disables_an_unnamed_local_user(stack, synced):
    assert search_user(stack, "unnamed-local@example.com").get("User", {}).get("disabled") in (True, 1, "1")


def test_sync_leaves_the_unnamed_oidc_user_enabled(stack, synced):
    assert search_user(stack, "oidc@example.com").get("User", {}).get("disabled") in (False, 0, "0")


def test_sync_break_glass_password_logs_in(stack, synced):
    _, out = stack.run("python3", "-c", LOGIN_FLOW, "break-glass@example.com", "Br3ak-Glass!Pass#2026")
    me = next((json.loads(line) for line in reversed(out.splitlines()) if line.startswith("{")), {})
    assert me.get("User", {}).get("email") == "break-glass@example.com", out[-2000:]


def test_sync_custom_warninglist_version(stack, synced):
    assert stack.sql("SELECT version FROM warninglists WHERE name='Sync Test Warninglist';") == "1"


def test_sync_custom_warninglist_matches_its_types_only(stack, synced):
    types = stack.sql("SELECT string_agg(type, ',' ORDER BY type) FROM warninglist_types WHERE warninglist_id = "
                      "(SELECT id FROM warninglists WHERE name = 'Sync Test Warninglist')")
    assert types == "domain,hostname"


def test_sync_sharing_group_created_with_its_org(stack, synced):
    found = sharing_groups(stack, "Sync Test Sharing Group")
    assert len(found) == 1
    assert found[0]["SharingGroup"]["releasability"] == "Sync test partners"
    members = {o.get("Organisation", {}).get("uuid") for o in found[0].get("SharingGroupOrg", [])}
    assert members == {SYNC_ORG_UUID}


def test_sync_custom_warninglist_updated_to_v2(stack, synced):
    rc, out = stack.org_sync(ORGS.format(version=2, extra="\n      - new-entry.example.org"), stack.key)
    assert rc == 0, out[-3000:]
    assert stack.sql("SELECT version FROM warninglists WHERE name='Sync Test Warninglist';") == "2"


@pytest.fixture(scope="module")
def warm_sync(stack, synced):
    return stack.org_sync(ORGS.format(version=2, extra="\n      - new-entry.example.org"), stack.key)


def test_sync_warm_run_exits_0(warm_sync):
    assert warm_sync[0] == 0, warm_sync[1][-3000:]


def test_sync_warm_run_changes_nothing(warm_sync):
    changed = [m for m in re.findall(r"'(?:created|updated)': (\d+)", warm_sync[1]) if m != "0"]
    assert changed == [], warm_sync[1][-2000:]


def test_sync_warm_run_keeps_one_sharing_group(stack, warm_sync):
    assert len(sharing_groups(stack, "Sync Test Sharing Group")) == 1


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


@pytest.mark.parametrize("path,status", [("/healthz", 200), ("/ready", 200), ("/nonexistent", 404)])
def test_metrics_paths(metrics_text, path, status):
    assert metric(path)[0] == status


def test_metrics_misp_up_is_1(metrics_text):
    assert value(metrics_text, "misp_up") == "1"


@pytest.mark.parametrize("name", ["misp_events", "misp_attributes", "misp_organisations", "misp_tags"])
def test_metrics_content_counts(metrics_text, name):
    assert name in metrics_text


@pytest.mark.parametrize("status", ["active", "disabled"])
def test_metrics_users_by_status(metrics_text, status):
    assert f'status="{status}"' in metrics_text


def test_metrics_instance_info(metrics_text):
    line = next((l for l in metrics_text.splitlines() if l.startswith("misp_instance_info{")), "")
    assert f'uuid="{TEST_ENV["MISP_UUID"]}"' in line and f'base_url="{TEST_ENV["MISP_BASEURL"]}"' in line, line


def test_metrics_scrape_duration(metrics_text):
    assert "misp_scrape_duration_seconds" in metrics_text


def test_metrics_no_scrape_errors(metrics_text):
    # A failed Redis read counts here too
    assert value(metrics_text, "misp_scrape_errors") == "0"


def test_metrics_help_and_type_for_every_block(metrics_text):
    lines = metrics_text.splitlines()
    assert sum(l.startswith("# HELP") for l in lines) == sum(l.startswith("# TYPE") for l in lines)


def test_metrics_scheduled_tasks_present(metrics_text):
    # Nothing runs MISP's Scheduled tasks here; the exporter counts enabled ones
    assert "misp_scheduled_tasks_enabled 0" in metrics_text


def test_metrics_count_no_manual_setting(metrics_text):
    assert value(metrics_text, "misp_settings_manual") == "0"


@pytest.fixture(scope="module")
def manual_setting(stack, metrics_text):
    # A setting the repository does not carry, changed as an admin changes it in the UI
    name = "MISP.footermidleft"
    status, body = stack.http("POST", f"/servers/serverSettingsEdit/{name}", stack.key, {"value": "by hand"})
    assert status == 200, body
    yield name
    stack.python(f"from misp_container import db\ndb.execute(\"DELETE FROM system_settings WHERE setting = '{name}'\")")


def test_metrics_report_a_manual_setting(manual_setting):
    text = metric()[1]
    assert f'misp_setting_manual{{setting="{manual_setting}"}} 1' in text
    assert value(text, "misp_settings_manual") == "1"


@pytest.fixture(scope="module")
def waiting_job(stack, metrics_text):
    # The depth comes from Redis: a job waits there while no worker runs
    stack.scale("worker", 0)
    stack.http("POST", "/servers/cache/all", stack.key, {})
    depth = default_depth()
    stack.scale("worker", 1)
    return depth


def test_metrics_waiting_job_shows_in_the_queue(waiting_job):
    assert waiting_job and int(waiting_job) >= 1, (
        f"a job queued with the worker stopped reads {waiting_job!r} in misp_jobs_queued: metrics.py reads "
        "Redis keys MISP no longer writes (see the job-keys check in tests/e2e/test_upstream.py)")


def test_metrics_count_the_org_sync_run(metrics_text, synced):
    assert 'misp_container_job_runs_total{operation="org-sync",status="success"}' in metrics_text


# -- MISP modules --------------------------------------------------------------------

def modules(path, data=None):
    request = urllib.request.Request(MODULES + path, data=json.dumps(data).encode() if data else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode()


@pytest.fixture(scope="module")
def modules_ready(stack):
    deadline = time.monotonic() + 90
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body = modules("/healthcheck")
            if status == 200:
                return True
            last = f"{status} {body[:200]}"
        except Exception as e:
            last = str(e)
        time.sleep(3)
    pytest.fail(f"misp-modules answered no /healthcheck within 90 s: {last}\n{stack.logs('modules')[-3000:]}")


def test_modules_loaded(modules_ready):
    body = modules("/modules")[1]
    assert len(json.loads(body)) > 0, body[:500]


def test_modules_dns_available(modules_ready):
    names = [m.get("name") for m in json.loads(modules("/modules")[1])]
    assert "dns" in names, names


# The dns module takes MISP's standard format: an attribute in, a dns-record object out
DNS_QUERY = {"module": "dns", "attribute": {"type": "domain", "value": "example.com",
                                            "uuid": "5d2c8e4a-7b1f-4c3e-9a6d-0f8b2e1c4a7d"}}


def resolved(body):
    """The A and AAAA values of the dns-record object in a dns module answer; none for any other body."""
    try:
        objects = json.loads(body).get("results", {}).get("Object", [])
    except (ValueError, AttributeError):
        return []
    return [a.get("value") for o in objects if o.get("name") == "dns-record"
            for a in o.get("Attribute", []) if a.get("object_relation") in ("a-record", "aaaa-record")]


def test_modules_dns_resolves(modules_ready):
    # Needs public DNS from the cluster
    body = modules("/query", DNS_QUERY)[1]
    assert resolved(body), body[:500]


def test_modules_enrichment_url_in_misp(stack, modules_ready):
    setting = stack.api("GET", "/servers/getSetting/Plugin.Enrichment_services_url", stack.key) or {}
    assert "modules" in str(setting.get("value", ""))


def test_web_reaches_modules_for_enrichment(stack, modules_ready):
    _, out = stack.python("""
import os, urllib.request
request = urllib.request.Request('http://modules:6666/query', headers={'Content-Type': 'application/json'},
                                 data=os.environ['QUERY'].encode())
with urllib.request.urlopen(request, timeout=10) as response:
    print(response.read().decode())
""", env={"QUERY": json.dumps(DNS_QUERY)})
    assert resolved(out), out[:500]


# -- multi-replica web ---------------------------------------------------------------

def test_configure_ran_configuration(stack):
    assert "configuration complete" in stack.logs("configure")


def test_configure_set_live(stack):
    assert "setting MISP.live = true" in stack.logs("configure")


@pytest.fixture(scope="module")
def two_web(stack):
    stack.scale("web", 2)
    deadline = time.monotonic() + 60
    while stack.logs("web").count("starting PHP-FPM") < 2 and time.monotonic() < deadline:
        time.sleep(2)
    yield stack
    stack.scale("web", 1)


def test_two_web_replicas_started_fpm(two_web):
    assert two_web.logs("web").count("starting PHP-FPM") == 2


def test_web_replicas_do_not_configure(two_web):
    assert "configuration complete" not in two_web.logs("web")


def test_two_web_pods_running(two_web):
    assert len(two_web.pods("web")) == 2


def test_login_served_with_two_replicas(two_web):
    assert two_web.http("GET", "/users/login", json_api=False)[0] == 200


# -- logging -------------------------------------------------------------------------

def test_configure_writes_no_text_lines(stack):
    text = [l for l in stack.logs("configure").splitlines()
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


LOG_BLOCK_HINT = ("MISP's log does not reach the output as JSON: check LOG_FORMAT, and the cakelog-streams and "
                  "shell-streams checks in tests/e2e/test_upstream.py (the logging block in files/misp_container/init.py)")


def test_misp_log_reaches_the_web_output(missing_controller_line):
    assert '"context":"misp"' in missing_controller_line, f"{LOG_BLOCK_HINT}: {missing_controller_line!r}"


def test_the_web_line_is_an_error(missing_controller_line):
    assert '"level":"error"' in missing_controller_line, f"{LOG_BLOCK_HINT}: {missing_controller_line!r}"


def test_worker_job_lines_once_as_json(stack):
    # A console shell would add its own text copy of every line
    lines = [l for l in stack.logs("worker").splitlines() if "launching job" in l]
    assert lines and all(l.startswith("{") and '"context":"misp"' in l for l in lines), f"{LOG_BLOCK_HINT}: {lines[:4]}"


def test_no_cakelog_files_on_disk(stack):
    assert stack.exec("web", "find /var/www/MISP/app/tmp/logs -maxdepth 1 "
                             "\\( -name debug.log -o -name error.log \\) -size +0 | wc -l") == "0", (
        "MISP wrote debug.log or error.log: the logging block no longer drops a FileLog stream MISP configures "
        "(see the cakelog-streams check in tests/e2e/test_upstream.py)")


def test_the_relay_forwards_server_sync_log(stack):
    stack.exec("web", 'echo "relay probe line" >> /var/www/MISP/app/tmp/logs/server-sync.log')
    deadline = time.monotonic() + 10
    line = ""
    while not line and time.monotonic() < deadline:
        line = next((l for l in stack.logs("web").splitlines() if "relay probe line" in l), "")
        time.sleep(1)
    assert '"context":"misp:server-sync"' in line


@pytest.mark.parametrize("service", ["web", "worker"])
def test_every_log_file_misp_wrote_is_relayed(stack, service):
    # Earlier sections exercise most of MISP; a file the relay does not follow grows unseen
    found = stack.exec(service, "cd /var/www/MISP/app/tmp/logs && find . -maxdepth 1 -type f -size +0").split()
    unrelayed = sorted({f.removeprefix("./") for f in found} - set(RELAYED) - set(NOT_RELAYED))
    assert not unrelayed, (f"MISP wrote {unrelayed} in app/tmp/logs of {service}: add each to FILES in "
                           "files/misp_container/logrelay.py, or to NOT_RELAYED in scripts/check_upstream.py")
