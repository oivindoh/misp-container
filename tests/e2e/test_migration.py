"""Migration suite: seeds a MISP on MariaDB, migrates it with the migrate Job into
a second MariaDB (same engine) and into PostgreSQL (cross engine), and checks
each copy through the API and the database. The same-engine copy takes the
attachments onto the volume, the cross-engine copy into an S3 bucket in
garage; a last run copies that bucket into another.

    pytest tests/e2e/test_migration.py [--skip-build] [--keep]

The tests run in file order and share one stack; module fixtures carry each
step's state to the next.
"""

import base64
import os
import shutil
from functools import partial

import pytest

import garage
from misp_container.s3 import Bucket
from stack import DEPLOY, TESTS, Stack, env_file, scratch_dir

SECRETS = env_file(TESTS / "test-compose-secrets.env")
TEST_ENV = env_file(TESTS / "test-compose.env")
ADMIN_EMAIL = TEST_ENV["ADMIN_EMAIL"]
MISP_UUID = TEST_ENV["MISP_UUID"]
# The sync user tests/migrate-orgs.yaml creates, with this authkey
INBOUND_KEY = "migrateinbound00000000000000000000000000"
ORG_UUID = "7c2f4a9e-3d5b-4e8a-9f10-6b7c8d9e0f11"
TABLES = ("events", "attributes", "users", "organisations", "servers", "tags")
# The source's attachments directory, which the migrate service mounts
FIXTURE_DIR = scratch_dir() / "migrate-source-files"
# Attachments in the three places MISP keeps them on disk, by the key they get
FIXTURE_FILES = {"999/1": ("999/1", "legacy attachment"),
                 "998/1": ("bucket_0/998/1", "bucketed attachment"),
                 "shadow/999/2": ("shadow/999/2", "proposal attachment")}
ATTACHMENTS = "/var/www/MISP/app/attachments"
# The cross-engine copy keeps its attachments in this bucket
S3_TARGET = env_file(TESTS / "migrate-target-s3.env")
S3_COPY_HOST = "postgres"
COPY_BUCKET = "misp-copy"


def email_of(stack, key):
    user = stack.api("GET", "/users/view/me", key) or {}
    return user.get("User", {}).get("email")


def migrate(stack, **env):
    """One run of the migrate Job against the source stack's own database."""
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


def switch(stack, overlay):
    """Point configure, web, worker and metrics at a target and restart them."""
    stack.compose("up", "-d", "--force-recreate", "configure", "web", "worker", "metrics",
                  extra_files=[overlay])
    stack.wait_for_misp()


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    os.environ["MIGRATE_FIXTURE_DIR"] = str(FIXTURE_DIR)
    s = Stack([DEPLOY / "docker-compose.yml", TESTS / "docker-compose.test.yml",
               TESTS / "docker-compose.migrate.yml"],
              profiles=("migrate", "migrate-target", "postgres"))
    s.compose("down", "-v")
    shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
    for where, content in [*FIXTURE_FILES.values(), ("taxonomies/x", "ships in the image")]:
        (FIXTURE_DIR / where).parent.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / where).write_text(content)
    if not options["skip_build"]:
        s.compose("build", check=True, timeout=3600)
    # The source stack runs without the migrate, target and postgres profiles
    s.compose("up", "-d", profiles=())
    s.wait_for_misp()
    s.key = s.admin_key(ADMIN_EMAIL)
    garage.layout()
    access, _ = garage.key("migrate", S3_TARGET["PLUGIN_S3_AWS_ACCESS_KEY"], S3_TARGET["PLUGIN_S3_AWS_SECRET_KEY"])
    for name in (S3_TARGET["PLUGIN_S3_BUCKET_NAME"], COPY_BUCKET):
        garage.bucket(name, access)
    yield s
    s.finish("migration", options["keep"], failed_in_module())
    shutil.rmtree(FIXTURE_DIR, ignore_errors=True)


@pytest.fixture(scope="module")
def seed(stack):
    stack.run("sync", env={"ADMIN_KEY": stack.key, "SYNC_BASE_URL": "http://caddy:8080"},
              volumes=[f"{TESTS / 'migrate-orgs.yaml'}:/etc/misp-docker/orgs.yaml:ro"])
    event = stack.api("POST", "/events/add", stack.key, {
        "info": "Migration suite event", "distribution": "0", "threat_level_id": "4", "analysis": "0"}) or {}
    event_id = event.get("Event", {}).get("id")
    attribute = stack.api("POST", f"/attributes/add/{event_id}", stack.key, {
        "event_id": event_id, "category": "Payload delivery", "type": "attachment", "value": "migrate.txt",
        "data": base64.b64encode(b"migration attachment content").decode()}) or {}
    return {"event_id": event_id,
            "attribute_id": attribute.get("Attribute", {}).get("id"),
            "counts": {table: stack.sql(f"SELECT COUNT(*) FROM {table}") for table in TABLES}}


@pytest.fixture(scope="module")
def mysql_target(stack, seed):
    stack.compose("up", "-d", "mysql-target")
    stack.wait_for("mysql-target", "healthcheck.sh --connect --innodb_initialized")
    return stack


@pytest.fixture(scope="module")
def first_copy(mysql_target):
    return migrate(mysql_target, DB_HOST="mysql-target")


@pytest.fixture(scope="module")
def identity_mismatch(first_copy, mysql_target):
    return migrate(mysql_target, DB_HOST="mysql-target", MISP_UUID="00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="module")
def force_refused(identity_mismatch, mysql_target):
    # The target holds the first copy: a MIGRATE_FORCE left behind must not drop it
    return migrate(mysql_target, DB_HOST="mysql-target", MIGRATE_FORCE="true")


@pytest.fixture(scope="module")
def forced_copy(force_refused, mysql_target):
    return migrate(mysql_target, DB_HOST="mysql-target", MIGRATE_FORCE="true", MIGRATE_REPLACE_COPY="true")


@pytest.fixture(scope="module")
def mariadb_copy(stack, forced_copy):
    switch(stack, TESTS / "docker-compose.migrate-mysql.yml")
    return "mysql-target"


@pytest.fixture(scope="module")
def postgres_run(stack, seed, first_copy):
    # The attachments volume holds the seeded attachment and, from the first
    # copy, the fixture files: the source of the upload into S3
    stack.compose("stop", "mysql-target")
    stack.compose("up", "-d", "postgres")
    stack.wait_for("postgres", 'pg_isready -U "$POSTGRES_USER" -d misp')
    return migrate(stack, DB_ENGINE="postgres", DB_HOST="postgres", DB_PORT="5432",
                   MIGRATE_SOURCE_FILES=ATTACHMENTS, **S3_TARGET)


@pytest.fixture(scope="module")
def postgres_copy(stack, postgres_run):
    switch(stack, TESTS / "docker-compose.migrate-s3.yml")
    return S3_COPY_HOST


@pytest.fixture(scope="module")
def bucket_copy(stack, postgres_copy):
    """The last run: the attachments from the S3 target into a second bucket."""
    source = {"MIGRATE_SOURCE_FILES": "", "MIGRATE_SOURCE_S3_BUCKET": S3_TARGET["PLUGIN_S3_BUCKET_NAME"],
              "MIGRATE_SOURCE_S3_ENDPOINT": S3_TARGET["PLUGIN_S3_AWS_ENDPOINT"],
              "MIGRATE_SOURCE_S3_REGION": S3_TARGET["PLUGIN_S3_REGION"],
              "MIGRATE_SOURCE_S3_ACCESS_KEY": S3_TARGET["PLUGIN_S3_AWS_ACCESS_KEY"],
              "MIGRATE_SOURCE_S3_SECRET_KEY": S3_TARGET["PLUGIN_S3_AWS_SECRET_KEY"]}
    return migrate(stack, DB_ENGINE="postgres", DB_HOST="postgres", DB_PORT="5432", MIGRATE_FORCE="true",
                   MIGRATE_REPLACE_COPY="true", **source, **{**S3_TARGET, "PLUGIN_S3_BUCKET_NAME": COPY_BUCKET})


# -- the checks every migrated copy must pass --------------------------------------

def web_uses_target(stack, seed, host):
    assert stack.exec("web", "echo $DB_HOST") == host


def live_set_by_configure(stack, seed, host):
    assert stack.sql("SELECT value FROM system_settings WHERE setting='MISP.live'") == "true"


def migration_recorded(stack, seed, host):
    assert stack.sql("SELECT status FROM misp_container_sync_log WHERE operation='migrate' "
                     "ORDER BY id DESC LIMIT 1") == "success"


def row_count(table, stack, seed, host):
    assert stack.sql(f"SELECT COUNT(*) FROM {table}") == seed["counts"][table]


def uuid_carried_over(stack, seed, host):
    assert MISP_UUID in stack.sql("SELECT value FROM system_settings WHERE setting='MISP.uuid'")


def admin_key_logs_in(stack, seed, host):
    assert email_of(stack, stack.key) == ADMIN_EMAIL


def sync_key_logs_in(stack, seed, host):
    assert email_of(stack, INBOUND_KEY) == "migrate-inbound@example.com"


def seeded_org_present(stack, seed, host):
    org = stack.api("GET", f"/organisations/view/{ORG_UUID}", stack.key) or {}
    assert org.get("Organisation", {}).get("name") == "Migrate Test Org"


def sync_server_present(stack, seed, host):
    _, body = stack.http("GET", "/servers/index", stack.key)
    assert "Migrate Test Server" in body


def event_intact(stack, seed, host):
    event = stack.api("GET", f"/events/view/{seed['event_id']}", stack.key) or {}
    assert event.get("Event", {}).get("info") == "Migration suite event"


def attachment_readable(stack, seed, host):
    # The download endpoint serves an encrypted zip; restSearch returns the bytes base64-encoded
    found = stack.api("POST", "/attributes/restSearch", stack.key, {
        "returnFormat": "json", "eventid": seed["event_id"], "type": "attachment", "withAttachments": 1}) or {}
    data = (found.get("response", {}).get("Attribute") or [{}])[0].get("data", "")
    assert base64.b64decode(data) == b"migration attachment content"


def fixture_attachments_copied(stack, seed, host):
    if host == S3_COPY_HOST:
        found = {key: content.decode() for key, content in objects(S3_TARGET["PLUGIN_S3_BUCKET_NAME"]).items()}
    else:
        found = {key: stack.exec("web", f"cat {ATTACHMENTS}/{key}") for key in FIXTURE_FILES}
    assert {key: found.get(key) for key in FIXTURE_FILES} == {key: c for key, (_, c) in FIXTURE_FILES.items()}


COPY_CHECKS = [web_uses_target, live_set_by_configure, migration_recorded,
               *[partial(row_count, table) for table in TABLES],
               uuid_carried_over, admin_key_logs_in, sync_key_logs_in, seeded_org_present,
               sync_server_present, event_intact, attachment_readable, fixture_attachments_copied]
CHECK_IDS = [c.func.__name__ + "_" + c.args[0] if isinstance(c, partial) else c.__name__ for c in COPY_CHECKS]


# -- seed ----------------------------------------------------------------------------

def test_seed_sync_user_logs_in(stack, seed):
    assert email_of(stack, INBOUND_KEY) == "migrate-inbound@example.com"


def test_seed_event_created(seed):
    assert seed["event_id"]


def test_seed_attachment_uploaded(seed):
    assert seed["attribute_id"]


# -- same engine: MariaDB to MariaDB -------------------------------------------------

def test_mariadb_migrate_exits_0(first_copy):
    rc, out = first_copy
    assert rc == 0, out[-3000:]


def test_mariadb_rows_copied(first_copy):
    assert "database copied" in first_copy[1]


def test_mariadb_attachments_copied(first_copy):
    # The three fixture attachments; taxonomies/x is no attachment
    assert f"attachments copied from /mnt/source to {ATTACHMENTS}: 3 files" in first_copy[1]


def test_mariadb_non_empty_target_refused(first_copy, mysql_target):
    rc, out = migrate(mysql_target, DB_HOST="mysql-target")
    assert rc == 3, out[-3000:]


def test_mariadb_identity_mismatch_refused(identity_mismatch):
    assert identity_mismatch[0] == 4, identity_mismatch[1][-3000:]


def test_mariadb_identity_mismatch_names_the_setting(identity_mismatch):
    assert "MISP.uuid on the source differs from MISP_UUID" in identity_mismatch[1]


def test_mariadb_force_alone_keeps_the_jobs_copy(force_refused):
    rc, out = force_refused
    assert rc == 3 and "holds a copy this Job made" in out, out[-3000:]


def test_mariadb_force_replaces_the_target(forced_copy):
    assert forced_copy[0] == 0, forced_copy[1][-3000:]


def test_mariadb_force_dropped_the_tables(forced_copy):
    assert "dropped" in forced_copy[1]


@pytest.mark.parametrize("check", COPY_CHECKS, ids=CHECK_IDS)
def test_mariadb_copy(stack, seed, mariadb_copy, check):
    check(stack, seed, mariadb_copy)


# -- cross engine: MariaDB to PostgreSQL ---------------------------------------------

def test_postgres_migrate_exits_0(postgres_run):
    rc, out = postgres_run
    assert rc == 0, out[-3000:]


def test_postgres_rows_copied(postgres_run):
    assert "database copied" in postgres_run[1]


def test_postgres_attachments_uploaded(postgres_run):
    # The fixture attachments and the seeded one
    assert f"attachments copied from {ATTACHMENTS} to s3://misp-migrated at http://garage:3900: 4 files" \
        in postgres_run[1]


@pytest.mark.parametrize("check", COPY_CHECKS, ids=CHECK_IDS)
def test_postgres_copy(stack, seed, postgres_copy, check):
    check(stack, seed, postgres_copy)


def test_postgres_new_rows_get_ids_past_the_copied_ones(stack, postgres_copy):
    assert stack.sql("SELECT COUNT(*) FROM tags WHERE id = (SELECT last_value FROM tags_id_seq)") == "1"


# -- bucket to bucket ----------------------------------------------------------------

def test_bucket_copy_exits_0(bucket_copy):
    rc, out = bucket_copy
    assert rc == 0, out[-3000:]


def test_bucket_copy_keeps_every_key_and_byte(bucket_copy):
    source = objects(S3_TARGET["PLUGIN_S3_BUCKET_NAME"])
    assert source and objects(COPY_BUCKET) == source
