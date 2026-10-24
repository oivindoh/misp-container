"""Migration suite: loads a MISP 2.5 database into MariaDB, MISP's own
INSTALL/MYSQL.sql of 2.5 with the rows of tests/migrate-source.sql, migrates
it with the migrate Job of this tree's image into PostgreSQL, which converts it
to MISP 3.0 on the way, and checks the copy through the API and the database.
The first copy takes the attachments onto the volume; on it, an org sync
renames the migrated organisation to its Keycloak organisation's alias, and a
migrated account logs in through Keycloak. A forced second copy takes the
attachments into an S3 bucket in garage; a last run copies that bucket into
another.

    pytest tests/e2e/test_migration.py [--skip-build] [--keep]

This tree's images run under MISP_IMAGE_TAG: CI loads them as ci, and a local
run builds them as migration-test. The tests run in file order and share one
stack; module fixtures carry each step's state to the next.
"""

import base64
import http.client
import os
import shutil
import subprocess
import urllib.request
from functools import partial

import pytest

import garage
import keycloak
from misp_container.s3 import Bucket
from stack import DEPLOY, TESTS, Stack, env_file, scratch_dir

SECRETS = env_file(TESTS / "test-compose-secrets.env")
TEST_ENV = env_file(TESTS / "test-compose.env")
ADMIN_EMAIL = TEST_ENV["ADMIN_EMAIL"]
MISP_UUID = TEST_ENV["MISP_UUID"]
# The source: MISP's schema at its newest 2.5 release, v2.5.50, then the suite's rows
SOURCE_SCHEMA = ("https://raw.githubusercontent.com/MISP/MISP/"
                 "fc99c9e3fc85beddc6ef18835359f3c8b7f44608/INSTALL/MYSQL.sql")
SOURCE_ROWS = TESTS / "migrate-source.sql"
TARGET_TAG = os.environ.get("MISP_IMAGE_TAG") or "migration-test"
MISP_SERVICES = ("configure", "web", "worker", "caddy", "metrics")
# The authkeys of the admin and of the sync user that tests/migrate-source.sql seeds
ADMIN_KEY = "migrateadmin0000000000000000000000000000"
INBOUND_KEY = "migrateinbound00000000000000000000000000"
ORG_UUID = "7c2f4a9e-3d5b-4e8a-9f10-6b7c8d9e0f11"
EVENT_ID = 1
TABLES = ("events", "attributes", "users", "organisations", "servers", "tags")
# The source's attachments directory, which the migrate service mounts
FIXTURE_DIR = scratch_dir() / "migrate-source-files"
# Attachments by the key they get: the seeded event's attachment, and one in each
# of the three places MISP keeps them on disk
FIXTURE_FILES = {"1/1": ("1/1", "migration attachment content"),
                 "999/1": ("999/1", "legacy attachment"),
                 "998/1": ("bucket_0/998/1", "bucketed attachment"),
                 "shadow/999/2": ("shadow/999/2", "proposal attachment")}
ATTACHMENTS = "/var/www/MISP/app/attachments"
# Email linking and unblocking for the migrated accounts' first Keycloak login
KEYCLOAK_ENV = TESTS / "migrate-keycloak.env"
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
# The second copy keeps its attachments in this bucket
S3_ENV = TESTS / "migrate-target-s3.env"
S3_TARGET = env_file(S3_ENV)
COPY_BUCKET = "misp-copy"


def email_of(stack, key):
    user = stack.api("GET", "/users/view/me", key) or {}
    return user.get("User", {}).get("email")


def mariadb(stack, sql):
    """SQL on the source's MariaDB, fed on stdin: the output, tab-separated."""
    result = subprocess.run([stack.engine, "exec", "-i", "-e", f"MYSQL_PWD={SECRETS['MARIADB_PASSWORD']}",
                             stack.container_id("mysql"), "mariadb", "-N", "-u", SECRETS["MARIADB_USER"], "misp"],
                            input=sql, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"mariadb exited {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def fetch(url, attempts=3):
    """A text file over HTTPS; a connection that breaks off part way gets another try."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode()
        except (OSError, http.client.HTTPException):
            if attempt == attempts - 1:
                raise


def migrate(stack, **env):
    """One run of the migrate Job of this tree's image, from the source's MariaDB."""
    source = {"MIGRATE_SOURCE_HOST": "mysql", "MIGRATE_SOURCE_USER": SECRETS["DB_USER"],
              "MIGRATE_SOURCE_PASSWORD": SECRETS["DB_PASSWORD"], "MIGRATE_SOURCE_FILES": "/mnt/source"}
    return stack.run("migrate", env={**source, **env})


def bucket(name):
    """A bucket of the test stack's garage, reached from this host."""
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


def switch(stack, env=None):
    """Run this tree's MISP on the copy, with an extra env file, and wait for it."""
    if env:
        os.environ["MISP_TEST_ENV"] = str(env)
    else:
        os.environ.pop("MISP_TEST_ENV", None)
    stack.compose("up", "-d", "--force-recreate", *MISP_SERVICES, profiles=())
    stack.wait_for_misp(timeout=600)


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    previous = {k: os.environ.get(k) for k in ("MISP_IMAGE_TAG", "MISP_TEST_ENV", "MIGRATE_FIXTURE_DIR")}
    os.environ["MISP_IMAGE_TAG"] = TARGET_TAG
    os.environ.pop("MISP_TEST_ENV", None)
    os.environ["MIGRATE_FIXTURE_DIR"] = str(FIXTURE_DIR)
    s = Stack([DEPLOY / "docker-compose.yml", TESTS / "docker-compose.test.yml",
               TESTS / "docker-compose.migrate.yml"], profiles=("migrate",))
    s.compose("down", "-v")
    shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
    for where, content in [*FIXTURE_FILES.values(), ("taxonomies/x", "ships in the image")]:
        (FIXTURE_DIR / where).parent.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / where).write_text(content)
    if not options["skip_build"]:
        s.compose("build", check=True, timeout=3600)
    s.compose("up", "-d", "mysql", "postgres", "redis", "garage", "keycloak", profiles=(), check=True)
    s.wait_for("mysql", "healthcheck.sh --connect --innodb_initialized", timeout=180)
    mariadb(s, fetch(SOURCE_SCHEMA) + SOURCE_ROWS.read_text())
    garage.layout()
    access, _ = garage.key("migrate", S3_TARGET["PLUGIN_S3_AWS_ACCESS_KEY"], S3_TARGET["PLUGIN_S3_AWS_SECRET_KEY"])
    for name in (S3_TARGET["PLUGIN_S3_BUCKET_NAME"], COPY_BUCKET):
        garage.bucket(name, access)
    yield s
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    s.finish("migration", options["keep"], failed_in_module())
    shutil.rmtree(FIXTURE_DIR, ignore_errors=True)


@pytest.fixture(scope="module")
def counts(stack):
    """The source's row counts, before the first copy changes it."""
    return {table: mariadb(stack, f"SELECT COUNT(*) FROM {table}") for table in TABLES}


@pytest.fixture(scope="module")
def first_copy(stack, counts):
    return migrate(stack, MIGRATE_SOURCE_UPGRADE="true")


@pytest.fixture(scope="module")
def identity_mismatch(stack, first_copy):
    return migrate(stack, MISP_UUID="00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="module")
def force_alone(stack, identity_mismatch):
    # The target holds the first copy: a MIGRATE_FORCE left behind must not drop it
    return migrate(stack, MIGRATE_FORCE="true")


@pytest.fixture(scope="module")
def forced_copy(stack, force_alone):
    return migrate(stack, MIGRATE_FORCE="true", MIGRATE_REPLACE_COPY="true")


@pytest.fixture(scope="module")
def volume_copy(stack, forced_copy):
    switch(stack, KEYCLOAK_ENV)
    return "volume"


@pytest.fixture(scope="module")
def keycloak_team(stack, volume_copy):
    """An org sync that renames the migrated organisation to its Keycloak organisation's alias."""
    before = (stack.api("GET", f"/organisations/view/{ORG_UUID}", ADMIN_KEY) or {}).get("Organisation", {})
    path = scratch_dir() / "migrate-keycloak-orgs.yaml"
    path.write_text(KEYCLOAK_ORGS)
    rc, out = stack.run("sync", env={"ADMIN_KEY": ADMIN_KEY, "SYNC_BASE_URL": "http://caddy:8080"},
                        volumes=[f"{path}:/etc/misp-docker/orgs.yaml:ro"])
    path.unlink()
    after = (stack.api("GET", f"/organisations/view/{ORG_UUID}", ADMIN_KEY) or {}).get("Organisation", {})
    user = (stack.api("GET", f"/admin/users/view/{MIGRATED_USER_ID}", ADMIN_KEY) or {}).get("User", {})
    return {"rc": rc, "out": out, "before": before, "after": after, "user": user}


@pytest.fixture(scope="module")
def keycloak_login(stack, keycloak_team):
    return keycloak.login(stack, "migrate-user", "migrate-user-password")


@pytest.fixture(scope="module")
def s3_run(stack, volume_copy):
    # The attachments volume holds the files of the first copy: the source of the upload into S3
    stack.compose("stop", "web", "worker", "metrics", profiles=())
    return migrate(stack, MIGRATE_FORCE="true", MIGRATE_REPLACE_COPY="true", MIGRATE_SOURCE_FILES=ATTACHMENTS,
                   **S3_TARGET)


@pytest.fixture(scope="module")
def s3_copy(stack, s3_run):
    switch(stack, S3_ENV)
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
    assert f"attachments copied from /mnt/source to {ATTACHMENTS}: 4 files" in first_copy[1]


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
