"""Migration suite: loads a MISP 2.5 database into MariaDB, MISP's own
INSTALL/MYSQL.sql of 2.5 with the rows of tests/migrate-source.sql, migrates
it into PostgreSQL with the chart's migrate component, which converts it to
MISP 3.0 on the way, and checks the copy through the API and the database.
The first copy, in the configure Job, takes the attachments from a claim onto
the attachments claim; on it, an org sync renames the migrated organisation to
its Keycloak organisation's alias, and a migrated account logs in through
Keycloak. A forced second copy takes the attachments into an S3 bucket in
garage; a last run copies that bucket into another. Every run after the first
runs migrate in the toolbox, with the env of the run.

    pytest tests/e2e/test_migration.py [--skip-build] [--keep]

The tests run in file order on one release; module fixtures carry each step's
state to the next.
"""

import base64
import http.client
import json
import shlex
import urllib.request
from functools import partial

import pytest

import garage
import keycloak
import manifests
from cluster import IMAGES, Cluster, build_images, image_tag
from misp_container.s3 import Bucket
from stack import K3S, VALUES, Release, scratch_dir, values_file

TEST_ENV = values_file(VALUES)["env"]
ADMIN_EMAIL = TEST_ENV["ADMIN_EMAIL"]
MISP_UUID = TEST_ENV["MISP_UUID"]
# Host port: node port. web, then garage's S3 and admin API
PORTS = {28080: 30080, 3900: 30900, 3903: 30903}
# The source: MISP's schema at its newest 2.5 release, v2.5.50, then the suite's rows
SOURCE_SCHEMA = ("https://raw.githubusercontent.com/MISP/MISP/"
                 "fc99c9e3fc85beddc6ef18835359f3c8b7f44608/INSTALL/MYSQL.sql")
SOURCE_ROWS = __import__("pathlib").Path(__file__).resolve().parents[1] / "migrate-source.sql"
SOURCE = {"MIGRATE_SOURCE_HOST": "mariadb", "MIGRATE_SOURCE_USER": "misp", "MIGRATE_SOURCE_PASSWORD": "source-test-pw"}
# The workloads that use the database, which a forced copy stops
MISP_WORKLOADS = ("web", "worker", "metrics")
# The authkeys of the admin and of the sync user that tests/migrate-source.sql seeds
ADMIN_KEY = "migrateadmin0000000000000000000000000000"
INBOUND_KEY = "migrateinbound00000000000000000000000000"
ORG_UUID = "7c2f4a9e-3d5b-4e8a-9f10-6b7c8d9e0f11"
EVENT_ID = 1
TABLES = ("events", "attributes", "users", "organisations", "servers", "tags")
# The source's attachments directory: a claim the migrate component mounts at /mnt/source
SOURCE_FILES = "/mnt/source"
# Attachments by the key they get: the seeded event's attachment, and one in each
# of the three places MISP keeps them on disk
FIXTURE_FILES = {"1/1": ("1/1", "migration attachment content"),
                 "999/1": ("999/1", "legacy attachment"),
                 "998/1": ("bucket_0/998/1", "bucketed attachment"),
                 "shadow/999/2": ("shadow/999/2", "proposal attachment")}
ATTACHMENTS = "/var/www/MISP/app/attachments"
# The first copy: the migrate component, with the source's attachments on the claim migrate-source
MIGRATE_VALUES = {"migrate": {
    "enabled": True,
    "secret": {**SOURCE, "MIGRATE_SOURCE_FILES": SOURCE_FILES, "MIGRATE_SOURCE_UPGRADE": "true"},
    "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": "migrate-source", "readOnly": True}}],
    "volumeMounts": [{"name": "source", "mountPath": SOURCE_FILES, "readOnly": True}]}}
# After the copy the migrate component goes off. Email linking and unblocking for the
# migrated accounts' first Keycloak login
KEYCLOAK_VALUES = {"migrate": {"enabled": False},
                   "env": {"OIDCAUTH_ALLOW_EMAIL_LINKING": "true", "OIDCAUTH_UNBLOCK": "true"}}
# The team of the migrated organisation: its UUID, and the alias of its Keycloak organisation
# (tests/keycloak-realm.json) as the name. The sync user stays named, so the run keeps it
KEYCLOAK_ORGS = f"""teams:
  - uuid: "{ORG_UUID}"
    name: "migrate-test-org"
    description: "Created by the migration suite"
    sector: "government"
    users:
      - email: migrate-inbound@example.com
        role: Sync user
        authkey: "{INBOUND_KEY}"
"""
# The migrated person's account in tests/migrate-source.sql
MIGRATED_USER_ID = "2"
# The second copy keeps its attachments in this bucket of garage, under a key the suite imports
S3_TARGET = {"PLUGIN_S3_BUCKET_NAME": "misp-migrated", "PLUGIN_S3_AWS_ENDPOINT": "http://garage:3900",
             "PLUGIN_S3_REGION": "garage", "PLUGIN_S3_AWS_ACCESS_KEY": "GK0123456789abcdef01234567",
             "PLUGIN_S3_AWS_SECRET_KEY": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
COPY_BUCKET = "misp-copy"


def email_of(stack, key):
    user = stack.api("GET", "/users/view/me", key) or {}
    return user.get("User", {}).get("email")


def mariadb(stack, sql):
    """SQL on the source's MariaDB, fed on stdin: the output, tab-separated."""
    rc, out = stack.exec_rc("mariadb", 'mariadb -N -u "$MARIADB_USER" misp', input=sql, timeout=300,
                            env={"MYSQL_PWD": SOURCE["MIGRATE_SOURCE_PASSWORD"]})
    if rc != 0:
        raise RuntimeError(f"mariadb exited {rc}: {out}")
    return out


def fetch(url, attempts=3):
    """A text file over HTTPS; a connection that breaks off part way gets another try."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode()
        except (OSError, http.client.HTTPException):
            if attempt == attempts - 1:
                raise


def source_files(stack):
    """The claim migrate-source with the fixture attachments, written by a pod that mounts it."""
    stack.apply({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "migrate-source"},
                 "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "100Mi"}}}},
                {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "source-files"},
                 "spec": {"securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                          "terminationGracePeriodSeconds": 1,
                          "containers": [{"name": "files", "image": f"{IMAGES[0]}:{image_tag()}",
                                          "args": ["sleep", "infinity"],
                                          "volumeMounts": [{"name": "source", "mountPath": SOURCE_FILES}]}],
                          "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": "migrate-source"}}]}})
    stack.kubectl("wait", "--for=condition=Ready", "pod/source-files", "--timeout=180s", timeout=200)
    for where, content in [*FIXTURE_FILES.values(), ("taxonomies/x", "ships in the image")]:
        path = shlex.quote(f"{SOURCE_FILES}/{where}")
        result = stack.kubectl("exec", "-i", "pod/source-files", "--", "sh", "-c",
                               f"mkdir -p $(dirname {path}) && cat > {path}", input=content)
        assert result.returncode == 0, result.stderr
    stack.kubectl("delete", "pod", "source-files", "--wait=true", timeout=120)


def migrate(stack, **env):
    """One run of migrate in the toolbox, from the source's MariaDB.

    The toolbox comes from the migrate container while the component is on, with
    its Secret and the source's claim, and from the configure container after.
    """
    container = "migrate" if stack.revision == 1 else "configure"
    return stack.run("python3", "-m", "misp_container.migrate", container=container,
                     env={**SOURCE, "MIGRATE_SOURCE_FILES": SOURCE_FILES, "MIGRATE_SOURCE_UPGRADE": "false",
                          "MIGRATE_FORCE": "false", "MIGRATE_REPLACE_COPY": "false", **env})


def bucket(name):
    """A bucket of the suite's garage, reached from this host."""
    return Bucket(name, S3_TARGET["PLUGIN_S3_AWS_ACCESS_KEY"], S3_TARGET["PLUGIN_S3_AWS_SECRET_KEY"],
                  region=garage.REGION, endpoint=garage.S3_HOST_ENDPOINT)


def objects(name):
    """{key: content} of a bucket."""
    path = scratch_dir() / "migration-object"
    found = {}
    for key in bucket(name).keys():
        bucket(name).download(key, path)
        found[key] = path.read_bytes()
    path.unlink(missing_ok=True)
    return found


def stop(stack):
    """Scale the workloads that use the database to none, before a copy replaces it."""
    for workload in MISP_WORKLOADS:
        stack.scale(workload, 0)


def switch(stack, values):
    """Upgrade the release with values, start the stopped workloads, and wait for MISP.

    A helm upgrade keeps the replicas a scale set, unless the chart's value changed.
    """
    stack.install(values, wait=False)
    for workload in MISP_WORKLOADS:
        stack.kubectl("scale", f"deploy/{workload}", "--replicas=1")
    stack.wait_for_revision()
    stack.wait_for_misp(timeout=600)


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    build_images(options)
    cluster = Cluster("misp-migration", PORTS)
    cluster.start()
    cluster.load_images_under_test()
    s = Release(cluster, values=values_file(K3S / "oidc.yaml"))
    s.apply(*manifests.mariadb(SOURCE["MIGRATE_SOURCE_USER"], SOURCE["MIGRATE_SOURCE_PASSWORD"]),
            *manifests.garage(), *manifests.keycloak(),
            manifests.nodeport("web", "web", {8080: 30080}),
            manifests.nodeport("garage", "garage", {3900: 30900, 3903: 30903}))
    source_files(s)
    for service in ("mariadb", "garage", "keycloak"):
        s.rollout(service)
    mariadb(s, fetch(SOURCE_SCHEMA) + SOURCE_ROWS.read_text())
    garage.layout()
    access, _ = garage.key("migrate", S3_TARGET["PLUGIN_S3_AWS_ACCESS_KEY"], S3_TARGET["PLUGIN_S3_AWS_SECRET_KEY"])
    for name in (S3_TARGET["PLUGIN_S3_BUCKET_NAME"], COPY_BUCKET):
        garage.bucket(name, access)
    yield s
    cluster.finish("migration", options["keep"], failed_in_module())


@pytest.fixture(scope="module")
def counts(stack):
    """The source's row counts, before the first copy changes it."""
    return {table: mariadb(stack, f"SELECT COUNT(*) FROM {table}") for table in TABLES}


@pytest.fixture(scope="module")
def first_copy(stack, counts):
    """The install with the migrate component: the copy runs in the init container of the configure Job."""
    stack.install(MIGRATE_VALUES, wait=False)
    try:
        stack.wait_for_revision()
    except RuntimeError as e:
        print(e)
    pods = json.loads(stack.kubectl("get", "pods", "-l", "job-name=configure-1", "-o", "json").stdout)["items"]
    state = pods[0]["status"].get("initContainerStatuses", [{}])[0].get("state", {}) if pods else {}
    out = stack.kubectl("logs", "job/configure-1", "-c", "migrate").stdout
    return state.get("terminated", {}).get("exitCode", -1), out


@pytest.fixture(scope="module")
def identity_mismatch(stack, first_copy):
    return migrate(stack, MISP_UUID="00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="module")
def force_alone(stack, identity_mismatch):
    # The target holds the first copy: a MIGRATE_FORCE left behind must not drop it
    return migrate(stack, MIGRATE_FORCE="true")


@pytest.fixture(scope="module")
def forced_copy(stack, force_alone):
    stop(stack)
    return migrate(stack, MIGRATE_FORCE="true", MIGRATE_REPLACE_COPY="true")


@pytest.fixture(scope="module")
def volume_copy(stack, forced_copy):
    switch(stack, KEYCLOAK_VALUES)
    return "volume"


@pytest.fixture(scope="module")
def keycloak_team(stack, volume_copy):
    """An org sync that renames the migrated organisation to its Keycloak organisation's alias."""
    before = (stack.api("GET", f"/organisations/view/{ORG_UUID}", ADMIN_KEY) or {}).get("Organisation", {})
    rc, out = stack.run("python3", "-m", "misp_container.sync", "/tmp/orgs.yaml", files={"/tmp/orgs.yaml": KEYCLOAK_ORGS},
                        env={"ADMIN_KEY": ADMIN_KEY, "SYNC_BASE_URL": "http://web:8080"})
    after = (stack.api("GET", f"/organisations/view/{ORG_UUID}", ADMIN_KEY) or {}).get("Organisation", {})
    user = (stack.api("GET", f"/admin/users/view/{MIGRATED_USER_ID}", ADMIN_KEY) or {}).get("User", {})
    return {"rc": rc, "out": out, "before": before, "after": after, "user": user}


@pytest.fixture(scope="module")
def keycloak_login(stack, keycloak_team):
    return keycloak.login(stack, "migrate-user", "migrate-user-password")


@pytest.fixture(scope="module")
def s3_run(stack, volume_copy):
    # The attachments claim holds the files of the first copy: the source of the upload into S3
    stop(stack)
    return migrate(stack, MIGRATE_FORCE="true", MIGRATE_REPLACE_COPY="true", MIGRATE_SOURCE_FILES=ATTACHMENTS,
                   **S3_TARGET)


@pytest.fixture(scope="module")
def s3_copy(stack, s3_run):
    switch(stack, {"env": S3_TARGET})
    return "s3"


@pytest.fixture(scope="module")
def bucket_copy(stack, s3_copy):
    """The last run: the attachments from the S3 target into a second bucket."""
    source = {"MIGRATE_SOURCE_FILES": "", "MIGRATE_SOURCE_S3_BUCKET": S3_TARGET["PLUGIN_S3_BUCKET_NAME"],
              "MIGRATE_SOURCE_S3_ENDPOINT": S3_TARGET["PLUGIN_S3_AWS_ENDPOINT"],
              "MIGRATE_SOURCE_S3_REGION": S3_TARGET["PLUGIN_S3_REGION"],
              "MIGRATE_SOURCE_S3_ACCESS_KEY": S3_TARGET["PLUGIN_S3_AWS_ACCESS_KEY"],
              "MIGRATE_SOURCE_S3_SECRET_KEY": S3_TARGET["PLUGIN_S3_AWS_SECRET_KEY"]}
    return migrate(stack, MIGRATE_FORCE="true", MIGRATE_REPLACE_COPY="true",
                   **source, **{**S3_TARGET, "PLUGIN_S3_BUCKET_NAME": COPY_BUCKET})


# -- the checks every migrated copy must pass --------------------------------------

def live_set_by_configure(stack, counts, where):
    assert stack.sql("SELECT value FROM system_settings WHERE setting='MISP.live'") == "true"


def migration_recorded(stack, counts, where):
    assert '"success":1' in stack.sql("SELECT value FROM system_settings WHERE setting='misp_docker.runs.migrate'")


def row_count(table, stack, counts, where):
    assert stack.sql(f"SELECT COUNT(*) FROM {table}") == counts[table]


def uuid_carried_over(stack, counts, where):
    assert MISP_UUID in stack.sql("SELECT value FROM system_settings WHERE setting='MISP.uuid'")


def admin_key_logs_in(stack, counts, where):
    assert email_of(stack, ADMIN_KEY) == ADMIN_EMAIL


def sync_key_logs_in(stack, counts, where):
    assert email_of(stack, INBOUND_KEY) == "migrate-inbound@example.com"


def seeded_org_present(stack, counts, where):
    org = stack.api("GET", f"/organisations/view/{ORG_UUID}", ADMIN_KEY) or {}
    assert org.get("Organisation", {}).get("name") == "Migrate Test Org"


def sync_server_present(stack, counts, where):
    _, body = stack.http("GET", "/servers/index", ADMIN_KEY)
    assert "Migrate Test Server" in body


def event_intact(stack, counts, where):
    event = stack.api("GET", f"/events/view/{EVENT_ID}", ADMIN_KEY) or {}
    assert event.get("Event", {}).get("info") == "Migration suite event"


def attachment_readable(stack, counts, where):
    # The download endpoint serves an encrypted zip; restSearch returns the bytes base64-encoded
    found = stack.api("POST", "/attributes/restSearch", ADMIN_KEY, {
        "returnFormat": "json", "eventid": EVENT_ID, "type": "attachment", "withAttachments": 1}) or {}
    data = (found.get("response", {}).get("Attribute") or [{}])[0].get("data", "")
    assert base64.b64decode(data) == b"migration attachment content"


def fixture_attachments_copied(stack, counts, where):
    if where == "s3":
        found = {key: content.decode() for key, content in objects(S3_TARGET["PLUGIN_S3_BUCKET_NAME"]).items()}
    else:
        found = {key: stack.exec("web", f"cat {ATTACHMENTS}/{key}") for key in FIXTURE_FILES}
    assert {key: found.get(key) for key in FIXTURE_FILES} == {key: c for key, (_, c) in FIXTURE_FILES.items()}


COPY_CHECKS = [live_set_by_configure, migration_recorded,
               *[partial(row_count, table) for table in TABLES],
               uuid_carried_over, admin_key_logs_in, sync_key_logs_in, seeded_org_present,
               sync_server_present, event_intact, attachment_readable, fixture_attachments_copied]
CHECK_IDS = [c.func.__name__ + "_" + c.args[0] if isinstance(c, partial) else c.__name__ for c in COPY_CHECKS]


# -- the source ----------------------------------------------------------------------

def test_source_holds_misp_2_attribute_values(stack, counts):
    # MISP 2.x keeps an attribute's value in value1 and value2; runUpgrade converts them
    assert mariadb(stack, "SELECT value1 FROM attributes WHERE id = 1") == "migrate.txt"


# -- MariaDB to PostgreSQL, attachments onto the volume --------------------------------

def test_migrate_exits_0(first_copy):
    rc, out = first_copy
    assert rc == 0, out[-3000:]


def test_migrate_upgraded_the_source_to_30(first_copy):
    assert "upgrading the source" in first_copy[1] and "cake Admin runUpgrade" in first_copy[1]


def test_rows_copied(first_copy):
    assert "database copied" in first_copy[1]


def test_attachments_copied(first_copy):
    # The seeded attachment and the three fixture attachments; taxonomies/x is no attachment
    assert f"attachments copied from {SOURCE_FILES} to {ATTACHMENTS}: 4 files" in first_copy[1]


def test_rerun_keeps_the_jobs_copy(stack, first_copy):
    # A rerun or a configure retry after the copy succeeds and changes nothing
    rc, out = migrate(stack)
    assert rc == 0 and "holds the copy this Job made" in out and "dropped" not in out, out[-3000:]


def test_identity_mismatch_refused(identity_mismatch):
    assert identity_mismatch[0] == 4, identity_mismatch[1][-3000:]


def test_identity_mismatch_names_the_setting(identity_mismatch):
    assert "MISP.uuid on the source differs from MISP_UUID" in identity_mismatch[1]


def test_force_alone_keeps_the_jobs_copy(force_alone):
    rc, out = force_alone
    assert rc == 0 and "holds the copy this Job made" in out and "dropped" not in out, out[-3000:]


def test_force_replaces_the_target(forced_copy):
    assert forced_copy[0] == 0, forced_copy[1][-3000:]


def test_force_dropped_the_tables(forced_copy):
    assert "dropped" in forced_copy[1]


@pytest.mark.parametrize("check", COPY_CHECKS, ids=CHECK_IDS)
def test_volume_copy(stack, counts, volume_copy, check):
    check(stack, counts, volume_copy)


def test_new_rows_get_ids_past_the_copied_ones(stack, volume_copy):
    assert stack.sql("SELECT COUNT(*) FROM tags WHERE id = (SELECT last_value FROM tags_id_seq)") == "1"


# -- Keycloak on the copy --------------------------------------------------------------

def test_keycloak_team_sync_exits_0(keycloak_team):
    assert keycloak_team["rc"] == 0, keycloak_team["out"][-3000:]


def test_org_sync_renames_the_migrated_org_by_its_uuid(keycloak_team):
    names = (keycloak_team["before"].get("name"), keycloak_team["after"].get("name"))
    assert names == ("Migrate Test Org", "migrate-test-org")


def test_org_sync_keeps_the_migrated_org_id(keycloak_team):
    assert keycloak_team["after"].get("id") == keycloak_team["before"].get("id")


def test_org_sync_disables_the_unlinked_migrated_account(keycloak_team):
    # No OIDC subject yet and not named in orgs.yaml: a local account to the org sync
    assert keycloak_team["user"].get("disabled") in (True, 1, "1")


def test_migrated_account_logs_in_through_keycloak(keycloak_login):
    user = keycloak_login["user"].get("User", {})
    assert (user.get("id"), user.get("email")) == (MIGRATED_USER_ID, "migrate-user@example.com"), \
        keycloak_login["out"][-2000:]


def test_migrated_account_lands_in_the_renamed_org(keycloak_login):
    assert keycloak_login["user"].get("Organisation", {}).get("name") == "migrate-test-org"


def test_migrated_account_is_linked_and_enabled(stack, keycloak_login):
    assert stack.sql(f"SELECT sub IS NOT NULL FROM users WHERE id = {MIGRATED_USER_ID}") == "1"
    assert stack.sql(f"SELECT disabled FROM users WHERE id = {MIGRATED_USER_ID}") == "0"


# -- attachments into S3 -------------------------------------------------------------

def test_s3_migrate_exits_0(s3_run):
    rc, out = s3_run
    assert rc == 0, out[-3000:]


def test_s3_attachments_uploaded(s3_run):
    assert f"attachments copied from {ATTACHMENTS} to s3://misp-migrated at http://garage:3900: 4 files" \
        in s3_run[1]


@pytest.mark.parametrize("check", COPY_CHECKS, ids=CHECK_IDS)
def test_s3_copy(stack, counts, s3_copy, check):
    check(stack, counts, s3_copy)


def test_s3_new_attachment_goes_to_the_bucket(stack, s3_copy):
    # The volume still holds a copy of every migrated attachment, so a read through
    # the API passes from disk as well; only a new upload shows where MISP writes
    event = stack.api("POST", "/events/add", ADMIN_KEY, {
        "info": "S3 target event", "distribution": "0", "threat_level_id": "4", "analysis": "0"}) or {}
    event_id = event.get("Event", {}).get("id")
    attribute = stack.api("POST", f"/attributes/add/{event_id}", ADMIN_KEY, {
        "event_id": event_id, "category": "Payload delivery", "type": "attachment", "value": "s3.txt",
        "data": base64.b64encode(b"S3 target attachment").decode()}) or {}
    key = f"{event_id}/{attribute.get('Attribute', {}).get('id')}"
    assert objects(S3_TARGET["PLUGIN_S3_BUCKET_NAME"]).get(key) == b"S3 target attachment"


# -- bucket to bucket ----------------------------------------------------------------

def test_bucket_copy_exits_0(bucket_copy):
    rc, out = bucket_copy
    assert rc == 0, out[-3000:]


def test_bucket_copy_keeps_every_key_and_byte(bucket_copy):
    source = objects(S3_TARGET["PLUGIN_S3_BUCKET_NAME"])
    assert source and objects(COPY_BUCKET) == source
