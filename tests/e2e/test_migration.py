"""Migration suite: seeds MISP 2.5.50 from the published images on MariaDB,
migrates it with the migrate Job of this tree's image into PostgreSQL, which
converts it to MISP 3.0 on the way, and checks the copy through the API and
the database. The first copy takes the attachments onto the volume, a forced
second copy into an S3 bucket in garage; a last run copies that bucket into
another.

    pytest tests/e2e/test_migration.py [--skip-build] [--keep]

This tree's images run under MISP_IMAGE_TAG: CI loads them as ci, and a local
run builds them as migration-test. The tests run in file order and share one
stack; module fixtures carry each step's state to the next.
"""

import base64
import os
import shutil
import subprocess
from functools import partial

import pytest

import garage
from misp_container.s3 import Bucket
from stack import DEPLOY, TESTS, Stack, env_file, scratch_dir

SECRETS = env_file(TESTS / "test-compose-secrets.env")
TEST_ENV = env_file(TESTS / "test-compose.env")
ADMIN_EMAIL = TEST_ENV["ADMIN_EMAIL"]
MISP_UUID = TEST_ENV["MISP_UUID"]
# The source: the newest 2.5 release, on MariaDB
SOURCE_TAG = "2.5.50"
SOURCE_ENV = TESTS / "migrate-source.env"
TARGET_TAG = os.environ.get("MISP_IMAGE_TAG") or "migration-test"
IMAGES = ("ghcr.io/oivindoh/misp-container", "ghcr.io/oivindoh/misp-container-caddy",
          "ghcr.io/oivindoh/misp-container-modules")
MISP_SERVICES = ("configure", "web", "worker", "caddy", "metrics")
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
# The second copy keeps its attachments in this bucket
S3_ENV = TESTS / "migrate-target-s3.env"
S3_TARGET = env_file(S3_ENV)
COPY_BUCKET = "misp-copy"


def use(tag, env=None):
    """Compose reads the image tag and the extra env file of every MISP service from these on each call."""
    os.environ["MISP_IMAGE_TAG"] = tag
    if env:
        os.environ["MISP_TEST_ENV"] = str(env)
    else:
        os.environ.pop("MISP_TEST_ENV", None)


def email_of(stack, key):
    user = stack.api("GET", "/users/view/me", key) or {}
    return user.get("User", {}).get("email")


def migrate(stack, **env):
    """One run of the migrate Job of this tree's image, from the source's MariaDB."""
    use(TARGET_TAG)
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
    use(TARGET_TAG, env)
    stack.compose("up", "-d", "--force-recreate", *MISP_SERVICES, profiles=())
    stack.wait_for_misp(timeout=600)


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack(options, failed_in_module):
    previous = {k: os.environ.get(k) for k in ("MISP_IMAGE_TAG", "MISP_TEST_ENV", "MIGRATE_FIXTURE_DIR")}
    os.environ["MIGRATE_FIXTURE_DIR"] = str(FIXTURE_DIR)
    s = Stack([DEPLOY / "docker-compose.yml", TESTS / "docker-compose.test.yml",
               TESTS / "docker-compose.migrate.yml"], profiles=("migrate",))
    s.compose("down", "-v")
    shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
    for where, content in [*FIXTURE_FILES.values(), ("taxonomies/x", "ships in the image")]:
        (FIXTURE_DIR / where).parent.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / where).write_text(content)
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
def first_copy(stack, seed):
    # Nothing of the source may write while the Job copies it
    stack.compose("stop", "web", "worker", "metrics", profiles=())
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
    switch(stack)
    return "volume"


@pytest.fixture(scope="module")
def s3_run(stack, volume_copy):
    # The attachments volume holds the seeded attachment and, from the first
    # copy, the fixture files: the source of the upload into S3
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

def live_set_by_configure(stack, seed, where):
    assert stack.sql("SELECT value FROM system_settings WHERE setting='MISP.live'") == "true"


def migration_recorded(stack, seed, where):
    assert '"success":1' in stack.sql("SELECT value FROM system_settings WHERE setting='misp_docker.runs.migrate'")


def row_count(table, stack, seed, where):
    assert stack.sql(f"SELECT COUNT(*) FROM {table}") == seed["counts"][table]


def uuid_carried_over(stack, seed, where):
    assert MISP_UUID in stack.sql("SELECT value FROM system_settings WHERE setting='MISP.uuid'")


def admin_key_logs_in(stack, seed, where):
    assert email_of(stack, stack.key) == ADMIN_EMAIL


def sync_key_logs_in(stack, seed, where):
    assert email_of(stack, INBOUND_KEY) == "migrate-inbound@example.com"


def seeded_org_present(stack, seed, where):
    org = stack.api("GET", f"/organisations/view/{ORG_UUID}", stack.key) or {}
    assert org.get("Organisation", {}).get("name") == "Migrate Test Org"


def sync_server_present(stack, seed, where):
    _, body = stack.http("GET", "/servers/index", stack.key)
    assert "Migrate Test Server" in body


def event_intact(stack, seed, where):
    event = stack.api("GET", f"/events/view/{seed['event_id']}", stack.key) or {}
    assert event.get("Event", {}).get("info") == "Migration suite event"


def attachment_readable(stack, seed, where):
    # The download endpoint serves an encrypted zip; restSearch returns the bytes base64-encoded
    found = stack.api("POST", "/attributes/restSearch", stack.key, {
        "returnFormat": "json", "eventid": seed["event_id"], "type": "attachment", "withAttachments": 1}) or {}
    data = (found.get("response", {}).get("Attribute") or [{}])[0].get("data", "")
    assert base64.b64decode(data) == b"migration attachment content"


def fixture_attachments_copied(stack, seed, where):
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


# -- seed ----------------------------------------------------------------------------

def test_seed_sync_user_logs_in(stack, seed):
    assert email_of(stack, INBOUND_KEY) == "migrate-inbound@example.com"


def test_seed_event_created(seed):
    assert seed["event_id"]


def test_seed_attachment_uploaded(seed):
    assert seed["attribute_id"]


# -- MariaDB to PostgreSQL, attachments onto the volume --------------------------------

def test_migrate_exits_0(first_copy):
    rc, out = first_copy
    assert rc == 0, out[-3000:]


def test_migrate_upgraded_the_source_to_30(first_copy):
    assert "upgrading the source" in first_copy[1] and "cake Admin runUpgrade" in first_copy[1]


def test_rows_copied(first_copy):
    assert "database copied" in first_copy[1]


def test_attachments_copied(first_copy):
    # The three fixture attachments; taxonomies/x is no attachment
    assert f"attachments copied from /mnt/source to {ATTACHMENTS}: 3 files" in first_copy[1]


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
def test_volume_copy(stack, seed, volume_copy, check):
    check(stack, seed, volume_copy)


def test_new_rows_get_ids_past_the_copied_ones(stack, volume_copy):
    assert stack.sql("SELECT COUNT(*) FROM tags WHERE id = (SELECT last_value FROM tags_id_seq)") == "1"


# -- attachments into S3 -------------------------------------------------------------

def test_s3_migrate_exits_0(s3_run):
    rc, out = s3_run
    assert rc == 0, out[-3000:]


def test_s3_attachments_uploaded(s3_run):
    # The fixture attachments and the seeded one
    assert f"attachments copied from {ATTACHMENTS} to s3://misp-migrated at http://garage:3900: 4 files" \
        in s3_run[1]


@pytest.mark.parametrize("check", COPY_CHECKS, ids=CHECK_IDS)
def test_s3_copy(stack, seed, s3_copy, check):
    check(stack, seed, s3_copy)


def test_s3_new_attachment_goes_to_the_bucket(stack, s3_copy):
    # The volume still holds a copy of every migrated attachment, so a read through
    # the API passes from disk as well; only a new upload shows where MISP writes
    event = stack.api("POST", "/events/add", stack.key, {
        "info": "S3 target event", "distribution": "0", "threat_level_id": "4", "analysis": "0"}) or {}
    event_id = event.get("Event", {}).get("id")
    attribute = stack.api("POST", f"/attributes/add/{event_id}", stack.key, {
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
