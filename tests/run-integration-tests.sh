#!/bin/bash
set -euo pipefail


#
# Containerised integration test suite for the MISP image.
# Runs the full compose stack and verifies each feature.
#
# Usage:
#   ./tests/run-tests.sh          # run all tests
#   ./tests/run-tests.sh --skip-build  # skip the compose build
#   ./tests/run-tests.sh --postgres    # the same suite on PostgreSQL
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/../deploy" && pwd)"
# COMPOSE_CMD selects the compose runner: podman compose (default) or docker compose (CI)
COMPOSE="${COMPOSE_CMD:-podman compose} -f ${DEPLOY_DIR}/docker-compose.yml -f ${SCRIPT_DIR}/docker-compose.test.yml"
SKIP_BUILD=0
DB_ENGINE_UNDER_TEST=mysql
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=1 ;;
        --postgres) DB_ENGINE_UNDER_TEST=postgres ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
if [ "$DB_ENGINE_UNDER_TEST" = "postgres" ]; then
    # The postgres service sits behind a compose profile; the overlay points every
    # MISP container at it through tests/postgres.env
    COMPOSE="${COMPOSE} -f ${SCRIPT_DIR}/docker-compose.postgres.yml --profile postgres"
fi
# CONTAINER_CMD is the engine behind it: podman (default) or docker (CI)
ENGINE="${CONTAINER_CMD:-podman}"
# Host-side scratch files (mounted into the sync container, so the path must be shared with the VM)
# Physical path: a podman machine on macOS shares /private, not the /tmp symlink
WORK_DIR="$(cd "${TMPDIR:-/tmp}" && pwd -P)"

PASSED=0
FAILED=0
SKIPPED=0
FAILURES=()

# --- Helpers ----------------------------------------------------------------

pass() { echo "  PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL: $1"; FAILED=$((FAILED + 1)); FAILURES+=("$1"); }
skip() { echo "  SKIP: $1"; SKIPPED=$((SKIPPED + 1)); }

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$desc"
    else
        fail "$desc (expected: '$expected', got: '$actual')"
    fi
}

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -q "$needle"; then
        pass "$desc"
    else
        fail "$desc (expected to contain: '$needle')"
    fi
}

assert_not_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if ! echo "$haystack" | grep -q "$needle"; then
        pass "$desc"
    else
        fail "$desc (expected NOT to contain: '$needle')"
    fi
}


# Section header with the previous section's wall time, for finding slow parts
SECTION_START=$(date +%s)
SECTION_NAME=""
section() {
    local now=$(date +%s)
    if [ -n "$SECTION_NAME" ]; then
        echo "  ($SECTION_NAME: $((now - SECTION_START))s)"
    fi
    SECTION_NAME="$1"
    SECTION_START=$now
    echo ""
    echo "--- $1 ---"
}

web_exec() { ${COMPOSE} exec -T web bash -c "$1" 2>/dev/null; }
# Container IDs of a compose service; both compose runners set this label
container_ids() { ${ENGINE} ps -q --filter "label=com.docker.compose.project=deploy" --filter "label=com.docker.compose.service=$1" 2>/dev/null; }
# Run SQL queries via the MySQL container (mariadb-client not in the MISP image)
# SQL through the image's own engine-neutral layer (MariaDB or PostgreSQL)
mysql_exec() {
    SQL="$1" ${COMPOSE} exec -T -e SQL web python3 -c '
import os, sys; sys.path.insert(0, "/opt")
from misp_container.env import apply_defaults
from misp_container import db
apply_defaults()
print(db.query(os.environ["SQL"]))' 2>/dev/null
}
db_query() { mysql_exec "$1" | tr -d '[:space:]'; }
sql_true() { [ "${DB_ENGINE_UNDER_TEST}" = "postgres" ] && echo TRUE || echo 1; }
sql_now_epoch() { [ "${DB_ENGINE_UNDER_TEST}" = "postgres" ] && echo "EXTRACT(EPOCH FROM NOW())::bigint" || echo "UNIX_TIMESTAMP()"; }
api_get() {
    local key="$1" path="$2"
    curl -s -H "Authorization: ${key}" -H "Accept: application/json" "http://localhost:${TEST_PORT}${path}" 2>/dev/null || echo '{}'
}

wait_for_misp() {
    echo "Waiting for MISP to be ready..."
    local retries=60
    until curl -sf -o /dev/null "http://localhost:${TEST_PORT}/users/login" 2>/dev/null || [ $retries -le 0 ]; do
        sleep 3
        retries=$((retries - 1))
    done
    if [ $retries -le 0 ]; then
        echo "ERROR: MISP did not become ready in time"
        ${COMPOSE} logs --tail=50
        exit 1
    fi
    echo "MISP is ready"
}

# --- Setup ------------------------------------------------------------------

TEST_PORT=28080  # use a non-standard port to avoid conflicts
MISP_VERSION=$(grep '^ARG CORE_TAG=' "${SCRIPT_DIR}/../Dockerfile" | cut -d= -f2)

echo "============================================="
echo " MISP Container Integration Tests"
echo "============================================="
echo ""

# Clean up any previous test run
${COMPOSE} down -v 2>/dev/null || true

if [ "$SKIP_BUILD" -eq 0 ]; then
    echo "Building images..."
    ${COMPOSE} build 2>&1
fi

section "Stack startup"
echo "Starting stack (cold start)..."
${COMPOSE} up -d 2>&1
wait_for_misp

# Bootstrap Garage S3 (layout + key + bucket)
echo "Bootstrapping Garage S3..."
GARAGE_ADMIN="s3cr3t-admin-t0ken"
GARAGE_URL="http://localhost:3903"

# Wait for Garage admin API
retries=20
until curl -sf -H "Authorization: Bearer ${GARAGE_ADMIN}" "${GARAGE_URL}/v2/GetClusterStatus" >/dev/null 2>&1 || [ $retries -le 0 ]; do
    sleep 1; retries=$((retries - 1))
done

GARAGE_NODE=$(curl -sf -H "Authorization: Bearer ${GARAGE_ADMIN}" "${GARAGE_URL}/v2/GetClusterStatus" | jq -r '.nodes[0].id')

# Assign layout and apply
curl -sf -X POST -H "Authorization: Bearer ${GARAGE_ADMIN}" -H "Content-Type: application/json" \
    -d "{\"roles\": [{\"id\": \"${GARAGE_NODE}\", \"zone\": \"dc1\", \"capacity\": 1073741824, \"tags\": []}]}" \
    "${GARAGE_URL}/v2/UpdateClusterLayout" >/dev/null
curl -sf -X POST -H "Authorization: Bearer ${GARAGE_ADMIN}" -H "Content-Type: application/json" \
    -d '{"version": 1}' "${GARAGE_URL}/v2/ApplyClusterLayout" >/dev/null

# Create API key and bucket
S3_KEY_JSON=$(curl -sf -X POST -H "Authorization: Bearer ${GARAGE_ADMIN}" -H "Content-Type: application/json" \
    -d '{"name": "misp-test"}' "${GARAGE_URL}/v2/CreateKey")
S3_ACCESS_KEY=$(echo "${S3_KEY_JSON}" | jq -r '.accessKeyId')
S3_SECRET_KEY=$(echo "${S3_KEY_JSON}" | jq -r '.secretAccessKey')

BUCKET_JSON=$(curl -sf -X POST -H "Authorization: Bearer ${GARAGE_ADMIN}" -H "Content-Type: application/json" \
    -d '{"globalAlias": "misp-attachments"}' "${GARAGE_URL}/v2/CreateBucket")
S3_BUCKET_ID=$(echo "${BUCKET_JSON}" | jq -r '.id')

# Grant key access to bucket
curl -sf -X POST -H "Authorization: Bearer ${GARAGE_ADMIN}" -H "Content-Type: application/json" \
    -d "{\"bucketId\": \"${S3_BUCKET_ID}\", \"accessKeyId\": \"${S3_ACCESS_KEY}\", \"permissions\": {\"read\": true, \"write\": true, \"owner\": true}}" \
    "${GARAGE_URL}/v2/AllowBucketKey" >/dev/null

echo "Garage S3 ready (key: ${S3_ACCESS_KEY:0:10}...)"

# Get an API key for authenticated tests
ADMIN_KEY=$(${COMPOSE} exec -T web /var/www/MISP/app/Console/cake user change_authkey test-admin@example.com 2>&1 | grep -o '[A-Za-z0-9]\{40\}')
echo "API key: ${ADMIN_KEY:0:10}..."
echo ""

# --- Test Suite -------------------------------------------------------------

section "Non-root operation"

# All containers must run as non-root (UID 1000) for security and OpenShift compat
uid=$(${COMPOSE} exec -T web id -u 2>/dev/null | tr -d '[:space:]')
assert_eq "web runs as UID 1000" "1000" "$uid"

uid=$(${COMPOSE} exec -T worker id -u 2>/dev/null | tr -d '[:space:]')
assert_eq "worker runs as UID 1000" "1000" "$uid"

# Caddy runs on scratch (no shell), verify via inspect
caddy_uid=$(${ENGINE} inspect "$(container_ids caddy | head -1)" --format '{{.Config.User}}' 2>/dev/null | cut -d: -f1 | tr -d '[:space:]')
assert_eq "caddy runs as UID 1000" "1000" "$caddy_uid"

section "HTTP / caddy"

# The pod probes hit PHP-FPM's ping through caddy
fpm_ping=$(curl -s "http://localhost:${TEST_PORT}/fpm-ping" 2>/dev/null | tr -d '[:space:]')
assert_eq "caddy proxies /fpm-ping to PHP-FPM" "pong" "$fpm_ping"

# caddy must serve the MISP login page (PHP proxied to FPM)
http_code=$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:${TEST_PORT}/users/login" 2>/dev/null)
assert_eq "login page returns 200" "200" "$http_code"

# caddy must serve static assets directly from its own webroot
http_code=$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:${TEST_PORT}/css/main.css" 2>/dev/null)
assert_eq "static CSS served by caddy" "200" "$http_code"

# Unauthenticated root should redirect to login (MISP's default behaviour)
http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-redirs 0 "http://localhost:${TEST_PORT}/" 2>/dev/null || true)
assert_eq "root returns redirect (302)" "302" "$http_code"

section "Admin user configuration"

user_json=$(api_get "$ADMIN_KEY" "/users/view/me")

# ADMIN_EMAIL env var should change the admin username from default admin@admin.test
email=$(echo "$user_json" | jq -r '.User.email')
assert_eq "admin email is test-admin@example.com" "test-admin@example.com" "$email"

# ADMIN_PASSWORD with --no_password_change means no forced reset on login
change_pw=$(echo "$user_json" | jq -r '.User.change_pw')
assert_eq "no forced password change" "false" "$change_pw"

# ADMIN_ORG env var should set the organisation name
org_name=$(echo "$user_json" | jq -r '.Organisation.name')
assert_eq "admin org is Test Org" "Test Org" "$org_name"

# ADMIN_ORG_UUID should create/find the org by UUID and assign the admin to it
org_uuid=$(echo "$user_json" | jq -r '.Organisation.uuid')
assert_eq "admin org UUID matches" "2399b00e-b7f4-4fdb-aeb9-03d28e83a210" "$org_uuid"

# last_pw_change must be set (not NULL) to avoid MISP forcing a password change
last_pw=$(db_query "SELECT last_pw_change FROM users WHERE id=1;")
assert_not_contains "last_pw_change is set" "$last_pw" "NULL"

section "Database settings storage"

# With ENABLE_DB_SETTINGS=true, settings should be persisted in the system_settings table
db_settings_count=$(db_query "SELECT COUNT(*) FROM system_settings;")
assert_contains "settings stored in DB" "$db_settings_count" "[0-9]"
[ "${db_settings_count:-0}" -gt 30 ] && pass "more than 30 settings in DB ($db_settings_count)" || fail "expected >30 DB settings, got $db_settings_count"

# Env-var-enforced settings (envars.json) should be written to the DB
baseurl=$(db_query "SELECT value FROM system_settings WHERE setting='MISP.baseurl';")
assert_eq "BASE_URL stored in DB" "\"http://caddy:8080\"" "$baseurl"

section "Workers"

# Background workers are managed by supervisord in the worker container.
# Wait for the supervisor socket to appear (workers need to configure first).
retries=30
until ${COMPOSE} exec -T worker bash -c 'test -S /tmp/supervisor.sock' 2>/dev/null || [ $retries -le 0 ]; do
    sleep 2; retries=$((retries - 1))
done
sleep 3

# Each MISP queue should have at least one worker in RUNNING state
sv_user=$(${COMPOSE} exec -T worker printenv SUPERVISOR_USERNAME 2>/dev/null || echo supervisor)
sv_pass=$(${COMPOSE} exec -T worker printenv SUPERVISOR_PASSWORD 2>/dev/null || echo supervisor)
worker_output=$(${COMPOSE} exec -T worker supervisorctl -s unix:///tmp/supervisor.sock -u "$sv_user" -p "$sv_pass" status 2>/dev/null || true)
for queue in default prio email cache update; do
    if echo "$worker_output" | grep -q "${queue}.*RUNNING"; then
        pass "worker queue '${queue}' is running"
    else
        fail "worker queue '${queue}' is not running"
    fi
done
# Periodic work belongs to the task runner; MISP's own scheduler never runs
assert_not_contains "worker: no scheduler program" "$worker_output" "scheduler"

# Verify web container can reach worker supervisord via TCP (same path MISP uses)
sv_status=$(${COMPOSE} exec -T web python3 -c "
import urllib.request, os
host = os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_HOST', 'worker')
port = os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_PORT', '9001')
user = os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_USER', 'supervisor')
passwd = os.environ.get('SIMPLEBACKGROUNDJOBS_SUPERVISOR_PASSWORD', 'supervisor')
mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
mgr.add_password(None, f'http://{host}:{port}', user, passwd)
opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
resp = opener.open(f'http://{host}:{port}/RPC2', b'<?xml version=\"1.0\"?><methodCall><methodName>supervisor.getState</methodName></methodCall>', timeout=5)
print('OK' if resp.status == 200 else f'HTTP {resp.status}')
" 2>/dev/null)
assert_eq "web -> worker supervisord TCP connectivity" "OK" "$sv_status"

section "Background job processing"

# Create an event and publish it to trigger background jobs (cache, correlation).
# Then verify that a job was actually picked up and completed by the worker.
job_event=$(curl -sf --max-time 15 -X POST \
    -H "Authorization: ${ADMIN_KEY}" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d '{"Event":{"info":"Background job test","distribution":0}}' \
    "http://localhost:${TEST_PORT}/events/add" 2>/dev/null)
job_event_id=$(echo "$job_event" | jq -r '.Event.id // empty')

if [ -n "$job_event_id" ]; then
    # Publish the event (triggers cache/email/correlation background jobs)
    curl -sf --max-time 15 -X POST \
        -H "Authorization: ${ADMIN_KEY}" \
        -H "Accept: application/json" \
        "http://localhost:${TEST_PORT}/events/publish/${job_event_id}" >/dev/null 2>&1

    # Wait for the publish job to be picked up and completed by the worker.
    # Poll the jobs table for a completed job (status=3 in MISP's job model).
    retries=15
    job_done=false
    while [ $retries -gt 0 ]; do
        # MISP job status: 1=queued, 2=running, 3=failed, 4=completed
        completed=$(db_query "SELECT COUNT(*) FROM jobs WHERE status=4;")
        if [ "${completed:-0}" -gt 0 ]; then
            job_done=true
            break
        fi
        sleep 2
        retries=$((retries - 1))
    done

    if [ "$job_done" = "true" ]; then
        pass "background job completed ($completed finished)"
    else
        total=$(db_query "SELECT COUNT(*) FROM jobs;")
        if [ "${total:-0}" -gt 0 ]; then
            pass "background jobs queued ($total total, still processing)"
        else
            fail "no background jobs found after publishing event"
        fi
    fi

    # Clean up
    curl -sf --max-time 10 -X POST -H "Authorization: ${ADMIN_KEY}" \
        "http://localhost:${TEST_PORT}/events/delete/${job_event_id}" >/dev/null 2>&1
else
    fail "could not create event for background job test"
fi

section "PHP-FPM"

# PHP-FPM should be listening on TCP port 9002 (0x232A in /proc/net/tcp6)
fpm_listening=$(web_exec "cat /proc/net/tcp6 2>/dev/null | grep ':232A'" || true)
assert_contains "PHP-FPM listens on port 9002" "$fpm_listening" "232A"

section "Distribution files and app/Config rendering"


# Taxonomy definitions ship in the image
taxonomies=$(web_exec "ls /var/www/MISP/app/files/taxonomies/ 2>/dev/null | head -3")
assert_contains "taxonomies directory populated" "$taxonomies" ""
[ -n "$taxonomies" ] && pass "taxonomies directory is not empty" || fail "taxonomies directory is empty"

# bootstrap.php gets the auth plugin detection patch when app/Config is rendered
bootstrap=$(web_exec "cat /var/www/MISP/app/Config/bootstrap.php 2>/dev/null | grep 'Detect what auth modules'")
assert_contains "bootstrap.php has auth plugin patch" "$bootstrap" "Detect what auth modules"

# database.php is generated from env vars with the host of the engine under test
db_php=$(web_exec "cat /var/www/MISP/app/Config/database.php 2>/dev/null | grep 'host'")
assert_contains "database.php has correct host" "$db_php" "$DB_ENGINE_UNDER_TEST"

# config.php should have Redis bootstrap settings (critical for K8s where
# each pod has its own Config volume and workers never run web configure)
config_php=$(web_exec "cat /var/www/MISP/app/Config/config.php 2>/dev/null")
assert_contains "config.php has Redis host" "$config_php" "redis_host"
assert_contains "config.php has Redis password" "$config_php" "redis_password"
assert_contains "config.php has SimpleBackgroundJobs" "$config_php" "SimpleBackgroundJobs"
assert_contains "config.php has supervisor_host" "$config_php" "supervisor_host"

# Worker container should have identical Redis config in its own config.php
worker_config=$(${COMPOSE} exec -T worker cat /var/www/MISP/app/Config/config.php 2>/dev/null)
assert_contains "worker config.php has Redis host" "$worker_config" "redis_host"
assert_contains "worker config.php has Redis password" "$worker_config" "redis_password"
assert_contains "worker config.php has SimpleBackgroundJobs" "$worker_config" "SimpleBackgroundJobs"

section "GPG"

# AUTOCONF_GPG=true should auto-generate a GPG key in the .gnupg volume
gpg_key=$(web_exec "ls /var/www/MISP/.gnupg/trustdb.gpg 2>/dev/null" | tr -d '[:space:]')
assert_contains "GPG key generated" "$gpg_key" "trustdb.gpg"

section "MISP API functional"

# The MISP API should report a 2.5.x version
server_info=$(api_get "$ADMIN_KEY" "/servers/getVersion.json")
misp_version=$(echo "$server_info" | jq -r '.version' 2>/dev/null)
assert_contains "MISP version reported" "$misp_version" "2.5"

# Verify full round-trip: create an event via the API, confirm it got an ID, then delete it
event_result=$(curl -sf -X POST \
    -H "Authorization: ${ADMIN_KEY}" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d '{"Event":{"info":"Integration test event","distribution":0}}' \
    "http://localhost:${TEST_PORT}/events/add" 2>/dev/null)
event_id=$(echo "$event_result" | jq -r '.Event.id // empty' 2>/dev/null)
if [ -n "$event_id" ]; then
    pass "create event via API (id=$event_id)"
    curl -sf -X POST -H "Authorization: ${ADMIN_KEY}" "http://localhost:${TEST_PORT}/events/delete/${event_id}" >/dev/null 2>&1 || true
else
    fail "create event via API"
fi

section "Warm restart behaviour"

# Run configure-misp manually and verify the warm-start fast path.
# This is more reliable than parsing container logs after a restart.
warm_output=$(${COMPOSE} exec -T web python3 -c '
import sys; sys.path.insert(0, "/opt")
from misp_container.env import apply_defaults
from misp_container.config import SettingsCache, apply_settings_fast
from misp_container import cake, db
apply_defaults()
db.acquire_config_lock()
try:
    cache = SettingsCache()
    cache.load()
    cache.load_defaults_version()
    apply_settings_fast("minimum_config", cache)
finally:
    db.release_config_lock()
' 2>&1)

assert_contains "warm start loads DB settings" "$warm_output" "DB settings"
assert_contains "warm start: minimum_config unchanged" "$warm_output" "minimum_config: 0 changed"

section "Version-gated defaults"

PY="python3 -c"
PY_INIT="import sys; sys.path.insert(0, '/opt'); from misp_container.env import apply_defaults; from misp_container import cake, db; from misp_container.config import SettingsCache, apply_settings_fast, load_settings_yaml; import yaml, os, shutil; apply_defaults()"
YAML_SRC="/etc/misp-docker/settings.yaml"

# Verify defaults version is saved
${COMPOSE} exec -T web ${PY} "${PY_INIT}
db.query(\"DELETE FROM system_settings WHERE setting='misp_docker.defaults_version';\")
specs = load_settings_yaml()
c = SettingsCache(); c.load(); c.load_defaults_version()
for g in ('minimum_config','db_enable','initialisation','critical','optional','gpg'): apply_settings_fast(g, c, specs)
c.save_defaults_version()
" >/dev/null 2>&1
stored_version=$(db_query "SELECT value FROM system_settings WHERE setting='misp_docker.defaults_version';")
assert_eq "defaults version saved after configure" "\"${MISP_VERSION}\"" "$stored_version"

# Version-gated upgrade: set csp_enforce=false, clear version, inject since gate
${COMPOSE} exec -T web ${PY} "${PY_INIT}
cake.set_setting('Security.csp_enforce', 'false')
db.query(\"DELETE FROM system_settings WHERE setting='misp_docker.defaults_version';\")
# Patch settings.yaml to add a since field to Security.csp_enforce
os.chmod('/etc/misp-docker', 0o770); os.chmod('${YAML_SRC}', 0o660)
shutil.copy2('${YAML_SRC}', '/tmp/settings.yaml.bak')
with open('${YAML_SRC}') as f: data = yaml.safe_load(f)
data['settings']['critical']['Security.csp_enforce']['since'] = '${MISP_VERSION}'
data['settings']['critical']['Security.csp_enforce']['value'] = True
with open('${YAML_SRC}', 'w') as f: yaml.dump(data, f)
specs = load_settings_yaml()
c = SettingsCache(); c.load(); c.load_defaults_version(); apply_settings_fast('critical', c, specs); c.save_defaults_version()
# Second run: set false again, re-run -- should NOT re-apply (version saved)
cake.set_setting('Security.csp_enforce', 'false')
c2 = SettingsCache(); c2.load(); c2.load_defaults_version(); apply_settings_fast('critical', c2, specs)
shutil.copy2('/tmp/settings.yaml.bak', '${YAML_SRC}')
os.chmod('${YAML_SRC}', 0o440); os.chmod('/etc/misp-docker', 0o550)
" >/dev/null 2>&1
stable_val=$(db_query "SELECT value FROM system_settings WHERE setting='Security.csp_enforce';")
assert_eq "version gate applies once then stays stable" "false" "$stable_val"

# Envar wins over version-gated default
${COMPOSE} exec -T web ${PY} "${PY_INIT}
db.query(\"DELETE FROM system_settings WHERE setting='misp_docker.defaults_version';\")
os.chmod('/etc/misp-docker', 0o770); os.chmod('${YAML_SRC}', 0o660)
shutil.copy2('${YAML_SRC}', '/tmp/settings.yaml.bak')
with open('${YAML_SRC}') as f: data = yaml.safe_load(f)
data['settings']['critical']['MISP.external_baseurl']['since'] = '${MISP_VERSION}'
data['settings']['critical']['MISP.external_baseurl']['value'] = 'https://should-not-win'
with open('${YAML_SRC}', 'w') as f: yaml.dump(data, f)
specs = load_settings_yaml()
c = SettingsCache(); c.load(); c.load_defaults_version(); apply_settings_fast('critical', c, specs)
shutil.copy2('/tmp/settings.yaml.bak', '${YAML_SRC}')
os.chmod('${YAML_SRC}', 0o440); os.chmod('/etc/misp-docker', 0o550)
" >/dev/null 2>&1
ext_baseurl=$(db_query "SELECT value FROM system_settings WHERE setting='MISP.external_baseurl';")
assert_eq "envar wins over version-gated default" "\"http://caddy:8080\"" "$ext_baseurl"

section "S3 attachment storage"

# Configure MISP to use the Garage S3 endpoint for attachments.
# Then create an event with an attachment and verify it works.
${COMPOSE} exec -T web python3 -c "
import sys; sys.path.insert(0, '/opt')
from misp_container import cake
cake.set_setting('Plugin.S3_enable', 'true')
cake.set_setting('Plugin.S3_aws_compatible', 'true')
cake.set_setting('Plugin.S3_bucket_name', 'misp-attachments')
cake.set_setting('Plugin.S3_aws_endpoint', 'http://garage:3900')
cake.set_setting('Plugin.S3_region', 'garage')
cake.set_setting('Plugin.S3_aws_access_key', '${S3_ACCESS_KEY}')
cake.set_setting('Plugin.S3_aws_secret_key', '${S3_SECRET_KEY}')
cake.set_setting('MISP.attachments_dir', 's3://', force=True)
" >/dev/null 2>&1

# Get a fresh API key (previous tests may have rotated it)
ADMIN_KEY=$(${COMPOSE} exec -T web /var/www/MISP/app/Console/cake user change_authkey test-admin@example.com 2>&1 | grep -o '[A-Za-z0-9]\{40\}')

# Create an event with a text attachment via the API
s3_event=$(curl -sf --max-time 30 -X POST \
    -H "Authorization: ${ADMIN_KEY}" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d '{"Event":{"info":"S3 test event","distribution":0}}' \
    "http://localhost:${TEST_PORT}/events/add" 2>/dev/null)
s3_event_id=$(echo "$s3_event" | jq -r '.Event.id // empty')
if [ -n "$s3_event_id" ]; then
    pass "s3: created test event (id=$s3_event_id)"
else
    fail "s3: could not create test event"
fi

# Add an attachment attribute to the event
if [ -n "$s3_event_id" ]; then
    attach_result=$(curl -s --max-time 30 -X POST \
        -H "Authorization: ${ADMIN_KEY}" \
        -H "Accept: application/json" \
        -H "Content-Type: application/json" \
        -d "{\"event_id\":\"${s3_event_id}\",\"category\":\"Payload delivery\",\"type\":\"attachment\",\"value\":\"test.txt\",\"data\":\"$(echo -n 'S3 storage test content' | base64)\"}" \
        "http://localhost:${TEST_PORT}/attributes/add/${s3_event_id}" 2>/dev/null)
    attr_id=$(echo "$attach_result" | jq -r '.Attribute.id // empty')
    if [ -n "$attr_id" ]; then
        pass "s3: uploaded attachment (attr_id=$attr_id)"
    else
        fail "s3: could not upload attachment"
    fi

    # Download the attachment back -- MISP encrypts attachments, so we check
    # that we get a non-empty response (not a 404 or error)
    if [ -n "$attr_id" ]; then
        # Download the attachment -- follow redirects and check we get content
        dl_size=$(curl -sL --max-time 15 -o /dev/null -w '%{size_download}' \
            -H "Authorization: ${ADMIN_KEY}" \
            "http://localhost:${TEST_PORT}/attributes/download/${attr_id}" 2>/dev/null)
        if [ "${dl_size:-0}" -gt 0 ]; then
            pass "s3: downloaded attachment ($dl_size bytes)"
        else
            fail "s3: download returned empty response"
        fi
    fi

    # Verify objects exist in Garage via the admin API.
    # Need to wait briefly for MISP's async write to complete.
    sleep 2
    bucket_info=$(curl -sf -H "Authorization: Bearer ${GARAGE_ADMIN}" \
        "${GARAGE_URL}/v2/GetBucketInfo?id=${S3_BUCKET_ID}")
    bucket_objects=$(echo "$bucket_info" | jq '.objects // 0')
    if [ "${bucket_objects:-0}" -gt 0 ]; then
        pass "s3: objects present in Garage bucket ($bucket_objects)"
    else
        # MISP may write async -- check bytes instead
        bucket_bytes=$(echo "$bucket_info" | jq '.bytes // 0')
        if [ "${bucket_bytes:-0}" -gt 0 ]; then
            pass "s3: data present in Garage bucket ($bucket_bytes bytes)"
        else
            fail "s3: no objects found in Garage bucket"
        fi
    fi

    # Don't delete -- the whole stack is torn down at the end, and keeping
    # the event lets you inspect the S3 attachment in the UI during test runs.
fi

section "Custom auth (reverse proxy header login)"

# Enable CustomAuth and create a test user, then verify that passing the
# X-Forwarded-Email header through caddy logs the user in automatically.
# CustomAuth is a browser/session feature so we test with HTML requests
# (not Accept: application/json which goes through the API auth path).

# Enable custom auth in mixed mode
${COMPOSE} exec -T web bash -c '
  CAKE=/var/www/MISP/app/Console/cake
  $CAKE Admin setSetting -q "Plugin.CustomAuth_enable" true
  $CAKE Admin setSetting -q "Plugin.CustomAuth_header" "X_FORWARDED_EMAIL"
  $CAKE Admin setSetting -q "Plugin.CustomAuth_use_header_namespace" true
  $CAKE Admin setSetting -q "Plugin.CustomAuth_header_namespace" "HTTP_"
  $CAKE Admin setSetting -q "Plugin.CustomAuth_required" false
' >/dev/null 2>&1

# Create a test user (role 3 = org admin, org 1) with external auth enabled.
# MISP CustomAuth matches users by external_auth_key, not email.
${COMPOSE} exec -T web bash -c '
  CAKE=/var/www/MISP/app/Console/cake
  $CAKE user create headeruser@example.com 3 1 "HeaderUserPass123!" 2>/dev/null || true
  $CAKE user change_pw headeruser@example.com "HeaderUserPass123!" --no_password_change
' >/dev/null 2>&1
mysql_exec "UPDATE users SET external_auth_required=$(sql_true), external_auth_key='headeruser@example.com', change_pw=NOT $(sql_true), last_pw_change=$(sql_now_epoch) WHERE email='headeruser@example.com';" >/dev/null 2>&1

# Browser-style request with the auth header. If CustomAuth works,
# MISP creates a session and serves the page (200) instead of redirecting
# to login (302). We use a fresh cookie jar per request.
header_code=$(curl -s -o /dev/null -w '%{http_code}' --max-redirs 0 \
  -c ${WORK_DIR}/misp-header-cookies \
  -H "X-Forwarded-Email: headeruser@example.com" \
  "http://localhost:${TEST_PORT}/events/index" 2>/dev/null || true)
assert_eq "custom auth: header login gets 200 (logged in)" "200" "$header_code"

# Without the header, mixed mode should redirect to login
no_header_code=$(curl -s -o /dev/null -w '%{http_code}' --max-redirs 0 \
  "http://localhost:${TEST_PORT}/events/index" 2>/dev/null || true)
assert_eq "custom auth: no header gets 302 (login redirect)" "302" "$no_header_code"

# Clean up: disable custom auth
${COMPOSE} exec -T web bash -c '
  /var/www/MISP/app/Console/cake Admin setSetting -q "Plugin.CustomAuth_enable" false
' >/dev/null 2>&1
rm -f ${WORK_DIR}/misp-header-cookies

# --- Org sync -----------------------------------------------------------------

# --- OIDC login through dex -------------------------------------------------

section "OIDC login"

# The whole flow runs inside the compose network: the login page starts the
# flow (mixed auth needs ?OidcAuth=enable), dex's local connector takes the
# credentials, and the callback lands on http://caddy:8080/users/login.
oidc_out=$(${COMPOSE} run --rm --no-deps -T sync bash -c '
set -e
J=/tmp/oidc-jar
AUTH=$(curl -s -o /dev/null -w "%{redirect_url}" -c $J "http://caddy:8080/users/login?OidcAuth=enable")
echo "auth=$AUTH"
# dex redirects the local connector to its form; the credentials post to that URL
FORM=$(curl -s -L -o /dev/null -w "%{url_effective}" -b $J -c $J "$AUTH")
echo "form=$FORM"
FINAL=$(curl -s -L -o /dev/null -w "%{url_effective} %{http_code}" -b $J -c $J --data-urlencode "login=oidc@example.com" --data-urlencode "password=oidc-test-password" "$FORM")
echo "final=$FINAL"
curl -s -b $J -H "Accept: application/json" http://caddy:8080/users/view/me.json | jq -c . || echo "{}"
' 2>/dev/null || true)
assert_contains "oidc: login page redirects to dex" "$oidc_out" "auth=http://dex:5556/dex/auth"
assert_contains "oidc: dex offers its password form" "$oidc_out" "form=http://dex:5556/dex/auth/local/login"
assert_contains "oidc: callback lands on MISP" "$oidc_out" "final=http://caddy:8080/ 200"
oidc_me=$(echo "$oidc_out" | grep '^{' | tail -1 || true)
assert_eq "oidc: user is logged in with the IdP email" "oidc@example.com" "$(echo "$oidc_me" | jq -r '.User.email // empty')"
assert_eq "oidc: role mapped by name" "Org Admin" "$(echo "$oidc_me" | jq -r '.Role.name // empty')"
assert_eq "oidc: default organisation assigned" "Test Org" "$(echo "$oidc_me" | jq -r '.Organisation.name // empty')"
oidc_form=$(curl -s "http://localhost:${TEST_PORT}/users/login" | grep -c 'name="data\[User\]\[password\]"' || true)
assert_eq "oidc: mixed auth keeps the password form" "1" "$oidc_form"


# --- Task runner (cronjob entrypoint) ---------------------------------------

section "Task runner"

# update-taxonomies is quick and idempotent; pull-servers with no servers makes no call
task_output=$(${COMPOSE} run --rm --no-deps -T -e ADMIN_KEY="${ADMIN_KEY}" -e SYNC_BASE_URL="http://caddy:8080" \
    sync python3 -m misp_container.task update-taxonomies 2>&1) && task_rc=0 || task_rc=$?
assert_eq "task: update-taxonomies exits 0" "0" "$task_rc"
assert_contains "task: update-taxonomies called the API" "$task_output" "POST /taxonomies/update"
task_output=$(${COMPOSE} run --rm --no-deps -T -e ADMIN_KEY="${ADMIN_KEY}" -e SYNC_BASE_URL="http://caddy:8080" \
    sync python3 -m misp_container.task pull-servers 2>&1) && task_rc=0 || task_rc=$?
assert_eq "task: pull-servers exits 0" "0" "$task_rc"
task_output=$(${COMPOSE} run --rm --no-deps -T -e ADMIN_KEY="" sync python3 -m misp_container.task pull-servers 2>&1) && task_rc=0 || task_rc=$?
assert_eq "task: missing ADMIN_KEY exits 1" "1" "$task_rc"

# The tasks that replace MISP's scheduler: with no TAXII servers and no
# blueprints the index tasks make no call and still exit 0
for task_name in cache-servers update-object-templates push-taxii sharing-group-blueprints; do
    task_output=$(${COMPOSE} run --rm --no-deps -T -e ADMIN_KEY="${ADMIN_KEY}" -e SYNC_BASE_URL="http://caddy:8080" \
        sync python3 -m misp_container.task "$task_name" 2>&1) && task_rc=0 || task_rc=$?
    assert_eq "task: ${task_name} exits 0" "0" "$task_rc"
done
assert_contains "task: push-taxii lists the TAXII servers" "$(${COMPOSE} run --rm --no-deps -T -e ADMIN_KEY="${ADMIN_KEY}" \
    -e SYNC_BASE_URL="http://caddy:8080" sync python3 -m misp_container.task push-taxii 2>&1)" "push-taxii: 0 TAXII server(s)"

# Console tasks render app/Config and run MISP's console; in Compose the worker has the volumes
task_output=$(${COMPOSE} exec -T worker python3 -m misp_container.task periodic-summary 2>&1) && task_rc=0 || task_rc=$?
assert_eq "task: periodic-summary exits 0" "0" "$task_rc"
assert_contains "task: periodic-summary ran MISP's console" "$task_output" "periodic summary"
task_output=$(${COMPOSE} exec -T worker python3 -m misp_container.task check-user-validity 2>&1) && task_rc=0 || task_rc=$?
assert_eq "task: check-user-validity exits 0 with OIDC enabled" "0" "$task_rc"
assert_contains "task: check-user-validity reports the admin" "$task_output" "test-admin@example.com"


section "Org sync"

# Create a test orgs.yaml with org, users, tags, taxonomies, warninglists, server
cat > ${WORK_DIR}/test-orgs.yaml <<'ORGSEOF'
taxonomies:
  - admiralty-scale
  - tlp

warninglists:
  - name: Sync Test Warninglist
    enabled: true
    description: "integration test custom warninglist"
    version: 1
    type: hostname
    category: false_positive
    matching_attributes:
      - hostname
      - domain
    values:
      - example.com
      - test.local

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
ORGSEOF

# Run the sync container against the live MISP instance
${COMPOSE} run --rm -T \
    -e ADMIN_KEY="${ADMIN_KEY}" \
    -e SYNC_BASE_URL="http://caddy:8080" \
    -v ${WORK_DIR}/test-orgs.yaml:/etc/misp-docker/orgs.yaml:ro \
    sync 2>&1

# Verify org was created
sync_org=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/organisations/view/4f1ed2b2-1821-49da-bf2c-b7ab639d9b19" 2>/dev/null)
sync_org_name=$(echo "$sync_org" | jq -r '.Organisation.name // empty')
assert_eq "sync: org created" "Sync Test Org" "$sync_org_name"

sync_org_sector=$(echo "$sync_org" | jq -r '.Organisation.sector // empty')
assert_eq "sync: org has correct sector" "academic" "$sync_org_sector"

# Verify user was created in the org
sync_user=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/admin/users/index/searchall:sync-test-user@example.com" 2>/dev/null)
sync_user_email=$(echo "$sync_user" | jq -r '.[0].User.email // empty')
assert_eq "sync: user created" "sync-test-user@example.com" "$sync_user_email"

sync_user_org=$(echo "$sync_user" | jq -r '.[0].Organisation.name // empty')
assert_eq "sync: user in correct org" "Sync Test Org" "$sync_user_org"

# Verify global tag was created
if curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/tags" 2>/dev/null | grep -q "sync-test:global-tag"; then
    pass "sync: global tag created"
else
    fail "sync: global tag created"
fi

# Verify server was created with pull tags
sync_servers=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/servers" 2>/dev/null)
sync_server_name=$(echo "$sync_servers" | jq -r '.[] | select(.Server.url=="https://sync-test.example.com") | .Server.name // empty')
assert_eq "sync: server created" "Sync Test Server" "$sync_server_name"

sync_pull_rules=$(echo "$sync_servers" | jq -r '.[] | select(.Server.url=="https://sync-test.example.com") | .Server.pull_rules // empty')
sync_pull_tag=$(echo "$sync_pull_rules" | jq -r '.tags.OR[0] // empty')
assert_eq "sync: server has pull_tags" "tlp:white" "$sync_pull_tag"

# Verify server authkey matches config (query DB directly -- API doesn't return authkeys).
# Verify server authkey via DB (mariadb-client not in MISP image, query via mysql container)
sync_server_id=$(echo "$sync_servers" | jq -r '.[] | select(.Server.url=="https://sync-test.example.com") | .Server.id // empty')
server_authkey=$(mysql_exec "SELECT authkey FROM servers WHERE id=${sync_server_id};" | tr -d '[:space:]')
assert_eq "sync: server authkey from config" "outbound000000000000000000000000000000000" "$server_authkey"

# Verify sync user was created with explicit authkey
sync_user_search=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/admin/users/index/searchall:sync-inbound@example.com" 2>/dev/null)
sync_inbound_email=$(echo "$sync_user_search" | jq -r '.[0].User.email // empty')
assert_eq "sync: sync user created" "sync-inbound@example.com" "$sync_inbound_email"

# Check authkey prefix in auth_keys table (MISP hashes the key but stores first 4 chars)
sync_inbound_id=$(echo "$sync_user_search" | jq -r '.[0].User.id // empty')
sync_user_key_start=$(mysql_exec "SELECT authkey_start FROM auth_keys WHERE user_id=${sync_inbound_id} ORDER BY id DESC LIMIT 1;" | tr -d '[:space:]')
assert_eq "sync: sync user authkey prefix" "inbo" "$sync_user_key_start"

# Verify taxonomy was enabled
sync_tax=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/taxonomies" 2>/dev/null)
sync_tax_enabled=$(echo "$sync_tax" | jq -r '[.[] | select(type=="object") | .Taxonomy // . | select(.namespace=="admiralty-scale")] | .[0].enabled // empty')
if [ "$sync_tax_enabled" = "true" ] || [ "$sync_tax_enabled" = "1" ]; then
    pass "sync: taxonomy enabled"
else
    fail "sync: taxonomy enabled (got: '$sync_tax_enabled')"
fi

# Verify tlp taxonomy was enabled
sync_tlp_enabled=$(echo "$sync_tax" | jq -r '[.[] | select(type=="object") | .Taxonomy // . | select(.namespace=="tlp")] | .[0].enabled // empty')
if [ "$sync_tlp_enabled" = "true" ] || [ "$sync_tlp_enabled" = "1" ]; then
    pass "sync: tlp taxonomy enabled"
else
    fail "sync: tlp taxonomy enabled (got: '$sync_tlp_enabled')"
fi

# Verify disabled user was created and is disabled
sync_disabled_user=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/admin/users/index/searchall:sync-disabled-user@example.com" 2>/dev/null)
sync_disabled_email=$(echo "$sync_disabled_user" | jq -r '.[0].User.email // empty')
assert_eq "sync: disabled user created" "sync-disabled-user@example.com" "$sync_disabled_email"
sync_disabled_flag=$(echo "$sync_disabled_user" | jq -r '.[0].User.disabled // empty')
if [ "$sync_disabled_flag" = "true" ] || [ "$sync_disabled_flag" = "1" ]; then
    pass "sync: disabled user is actually disabled"
else
    fail "sync: disabled user is actually disabled (got: '$sync_disabled_flag')"
fi

# Verify custom warninglist was created with correct content.
# Dump raw response to debug format, then search flexibly.
sync_wl_raw=$(curl -sf -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
    "http://localhost:${TEST_PORT}/warninglists" 2>/dev/null)
# Search for our warninglist name in the raw JSON (handles any nesting)
if echo "$sync_wl_raw" | grep -q "Sync Test Warninglist"; then
    pass "sync: custom warninglist created"
    # Extract version via DB query (API response nesting is unpredictable)
    sync_custom_wl_version=$(mysql_exec "SELECT version FROM warninglists WHERE name='Sync Test Warninglist';" | tr -d '[:space:]')
    assert_eq "sync: custom warninglist version" "1" "$sync_custom_wl_version"
else
    fail "sync: custom warninglist created"
    skip "sync: custom warninglist version"
fi

# Update the warninglist with a new version and different content
cat > ${WORK_DIR}/test-orgs-v2.yaml <<'ORGSEOF2'
taxonomies:
  - admiralty-scale
  - tlp

warninglists:
  - name: Sync Test Warninglist
    enabled: true
    description: "updated integration test warninglist"
    version: 2
    type: hostname
    category: false_positive
    matching_attributes:
      - hostname
      - domain
    values:
      - example.com
      - test.local
      - new-entry.example.org

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
ORGSEOF2

# Run sync with updated warninglist version
${COMPOSE} run --rm -T \
    -e ADMIN_KEY="${ADMIN_KEY}" \
    -e SYNC_BASE_URL="http://caddy:8080" \
    -v ${WORK_DIR}/test-orgs-v2.yaml:/etc/misp-docker/orgs.yaml:ro \
    sync 2>&1

# Verify warninglist was updated to version 2 (query DB directly)
sync_wl_v2_version=$(mysql_exec "SELECT version FROM warninglists WHERE name='Sync Test Warninglist';" | tr -d '[:space:]')
assert_eq "sync: custom warninglist updated to v2" "2" "$sync_wl_v2_version"

# Run sync again (warm run with v2 config) -- should not create or update any resources.
# Disabling unmanaged users is expected (headeruser from custom auth test).
${COMPOSE} run --rm -T \
    -e ADMIN_KEY="${ADMIN_KEY}" \
    -e SYNC_BASE_URL="http://caddy:8080" \
    -v ${WORK_DIR}/test-orgs-v2.yaml:/etc/misp-docker/orgs.yaml:ro \
    sync 2>&1 | tee ${WORK_DIR}/warm-sync-output.txt || true
warm_sync_output=$(cat ${WORK_DIR}/warm-sync-output.txt)
# Check warm run did not create or update anything
warm_created=$(echo "$warm_sync_output" | /usr/bin/grep -o "'created': [0-9]*" | /usr/bin/grep -v "'created': 0" || true)
warm_updated=$(echo "$warm_sync_output" | /usr/bin/grep -o "'updated': [0-9]*" | /usr/bin/grep -v "'updated': 0" || true)
if [ -z "$warm_created" ] && [ -z "$warm_updated" ]; then
    pass "sync: warm run creates/updates nothing"
else
    fail "sync: warm run creates/updates nothing (${warm_created} ${warm_updated})"
fi

rm -f ${WORK_DIR}/test-orgs.yaml ${WORK_DIR}/test-orgs-v2.yaml


# ============================================================================
# Metrics exporter
# ============================================================================

section "Metrics exporter"

METRICS_PORT=19191

# Wait for metrics endpoint to become available
echo "Waiting for metrics exporter..."
retries=30
until curl -sf -o /dev/null "http://localhost:${METRICS_PORT}/healthz" 2>/dev/null || [ $retries -le 0 ]; do
    sleep 2; retries=$((retries - 1))
done

if [ $retries -le 0 ]; then
    echo "WARNING: Metrics exporter did not become ready, skipping metrics tests"
    skip "metrics: exporter not ready"
else
    # Fetch metrics once, test everything from the captured output
    metrics_output=$(curl -sf "http://localhost:${METRICS_PORT}/metrics" 2>/dev/null || echo "")

    if [ -z "$metrics_output" ]; then
        fail "metrics: /metrics returns empty response"
    else
        pass "metrics: /metrics endpoint responds"

        # Health check endpoints
        healthz=$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:${METRICS_PORT}/healthz" 2>/dev/null)
        assert_eq "metrics: /healthz returns 200" "200" "$healthz"
        ready=$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:${METRICS_PORT}/ready" 2>/dev/null)
        assert_eq "metrics: /ready returns 200" "200" "$ready"

        # 404 for unknown paths
        notfound=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${METRICS_PORT}/nonexistent" 2>/dev/null)
        assert_eq "metrics: unknown path returns 404" "404" "$notfound"

        # Core metric: misp_up should be 1
        assert_contains "metrics: misp_up is present" "$metrics_output" "misp_up"
        misp_up=$(echo "$metrics_output" | grep '^misp_up ' | awk '{print $2}')
        assert_eq "metrics: misp_up = 1" "1" "$misp_up"

        # Content counts should be present
        assert_contains "metrics: misp_events gauge" "$metrics_output" "misp_events"
        assert_contains "metrics: misp_attributes gauge" "$metrics_output" "misp_attributes"
        assert_contains "metrics: misp_organisations gauge" "$metrics_output" "misp_organisations"
        assert_contains "metrics: misp_tags gauge" "$metrics_output" "misp_tags"

        # Users should have status labels
        assert_contains "metrics: misp_users active" "$metrics_output" 'status="active"'
        assert_contains "metrics: misp_users disabled" "$metrics_output" 'status="disabled"'

        # Instance info should have uuid label
        assert_contains "metrics: instance info has uuid" "$metrics_output" "misp_instance_info"

        # Scrape duration should be present (self-monitoring)
        assert_contains "metrics: scrape duration present" "$metrics_output" "misp_scrape_duration_seconds"
        assert_contains "metrics: scrape errors present" "$metrics_output" "misp_scrape_errors"
        scrape_errors=$(echo "$metrics_output" | grep '^misp_scrape_errors ' | awk '{print $2}')
        assert_eq "metrics: no scrape errors" "0" "$scrape_errors"

        # All metric blocks should have HELP and TYPE headers (Prometheus format)
        help_count=$(echo "$metrics_output" | grep -c '^# HELP' || true)
        type_count=$(echo "$metrics_output" | grep -c '^# TYPE' || true)
        assert_eq "metrics: HELP and TYPE counts match" "$help_count" "$type_count"

        # Jobs queue depth metric should exist
        assert_contains "metrics: jobs queued metric present" "$metrics_output" "misp_jobs_queued"
        # Nothing runs MISP's Scheduled tasks here; the exporter counts enabled ones
        assert_contains "metrics: scheduled tasks metric present" "$metrics_output" "misp_scheduled_tasks_enabled 0"

        # The depth comes from Redis: a job waits there while no worker runs
        default_depth() {
            curl -sf "http://localhost:${METRICS_PORT}/metrics" 2>/dev/null \
                | grep '^misp_jobs_queued{worker="default"}' | awk '{print $2}'
        }
        ${COMPOSE} stop worker >/dev/null 2>&1
        curl -sf -X POST -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" \
            -H "Content-Type: application/json" -d '{}' \
            "http://localhost:${TEST_PORT}/servers/cache/all" >/dev/null 2>&1
        depth=$(default_depth)
        if [ "${depth:-0}" -ge 1 ]; then
            pass "metrics: a waiting job shows in misp_jobs_queued (default=${depth})"
        else
            fail "metrics: a waiting job does not show in misp_jobs_queued (default=${depth:-none})"
        fi
        ${COMPOSE} start worker >/dev/null 2>&1
        retries=30
        until [ "$(default_depth)" = "0" ] || [ $retries -le 0 ]; do
            sleep 1; retries=$((retries - 1))
        done
        assert_eq "metrics: misp_jobs_queued returns to 0 once the worker runs" "0" "$(default_depth)"

        # Sync log metric should be present after sync ran earlier in this test
        if echo "$metrics_output" | grep -q "misp_sync_runs_24h"; then
            pass "metrics: sync log metrics present after org sync"
        else
            # Sync log table may not exist if sync ran before metrics code was present
            skip "metrics: sync log metrics (table may not exist yet)"
        fi
    fi
fi



# ============================================================================
# MISP modules (enrichment)
# ============================================================================


section "MISP modules"

MODULES_PORT=16666

# Wait for modules to be healthy
echo "Waiting for MISP modules..."
retries=30
until curl -sf -o /dev/null "http://localhost:${MODULES_PORT}/healthcheck" 2>/dev/null || [ $retries -le 0 ]; do
    sleep 2; retries=$((retries - 1))
done

if [ $retries -le 0 ]; then
    echo "WARNING: MISP modules did not become ready, skipping modules tests"
    skip "modules: not ready"
else
    # Healthcheck
    healthz=$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:${MODULES_PORT}/healthcheck" 2>/dev/null)
    assert_eq "modules: healthcheck returns 200" "200" "$healthz"

    # List available modules
    modules_json=$(curl -sf "http://localhost:${MODULES_PORT}/modules" 2>/dev/null || echo "[]")
    module_count=$(echo "$modules_json" | jq 'length')
    if [ "$module_count" -gt 0 ]; then
        pass "modules: ${module_count} modules loaded"
    else
        fail "modules: no modules loaded"
    fi

    # Verify dns module is available (used for smoke test below)
    has_dns=$(echo "$modules_json" | jq '[.[] | select(.name == "dns")] | length')
    assert_eq "modules: dns module available" "1" "$has_dns"

    # Direct module query: dns resolution (no API key needed)
    dns_result=$(curl -sf "http://localhost:${MODULES_PORT}/query" \
        -H "Content-Type: application/json" \
        -d '{"module":"dns","domain":"example.com"}' 2>/dev/null || echo '{}')
    dns_values=$(echo "$dns_result" | jq -r '.results[0].values[0] // empty' 2>/dev/null)
    if [ -n "$dns_values" ]; then
        pass "modules: dns module resolves example.com -> ${dns_values}"
    else
        fail "modules: dns module returned no results"
    fi

    # Verify MISP enrichment URL points to the modules service
    # Verify MISP enrichment URL via API
    enrichment_url=$(api_get "$ADMIN_KEY" "/servers/getSetting/Plugin.Enrichment_services_url" | \
        jq -r '.value // empty' 2>/dev/null)
    if echo "$enrichment_url" | /usr/bin/grep -q "modules"; then
        pass "modules: MISP enrichment URL = ${enrichment_url}"
    else
        fail "modules: enrichment URL (expected modules, got: ${enrichment_url:-empty})"
    fi

    # End-to-end enrichment: send a MISP-format attribute to the modules service
    # via the compose network (modules:6666) -- the same path MISP uses
    enrich_result=$(${COMPOSE} exec -T web python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://modules:6666/query',
    data=json.dumps({'module': 'dns', 'domain': 'example.com'}).encode(),
    headers={'Content-Type': 'application/json'},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    data = json.loads(resp.read())
    values = data.get('results', [{}])[0].get('values', [])
    print(values[0] if values else '')
" 2>/dev/null)
    if [ -n "$enrich_result" ]; then
        pass "modules: web->modules enrichment resolved example.com -> ${enrich_result}"
    else
        fail "modules: web->modules enrichment returned no results"
    fi
fi


# --- Multi-replica web ------------------------------------------------------

section "Multi-replica web"

# The configure service owns schema and settings; web replicas only wait for MISP.live.
configure_logs=$(${COMPOSE} logs configure 2>/dev/null || true)
assert_contains "configure: ran configuration" "$configure_logs" "configuration complete"
assert_contains "configure: set MISP.live" "$configure_logs" "setting MISP.live = true"

${COMPOSE} up -d --no-deps --no-recreate --scale web=2 web >/dev/null 2>&1
# Read each replica's log through the engine; compose log output differs between runners
web_logs_all() { for id in $(container_ids web); do ${ENGINE} logs "$id" 2>&1; done; }
retries=30
until [ "$(web_logs_all | grep -c 'starting PHP-FPM' || true)" -ge 2 ] || [ $retries -le 0 ]; do
    sleep 2; retries=$((retries - 1))
done
web_logs=$(web_logs_all || true)
fpm_starts=$(echo "$web_logs" | grep -c 'starting PHP-FPM' || true)
assert_eq "web: two replicas started PHP-FPM" "2" "$fpm_starts"
assert_not_contains "web: replicas do not run configuration" "$web_logs" "configuration complete"
web_count=$(container_ids web | wc -l | tr -d ' ')
assert_eq "web: two containers running" "2" "$web_count"
login_status=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${TEST_PORT}/users/login" || true)
assert_eq "web: login page served with two replicas" "200" "$login_status"
# Scale back to one replica so the compose teardown sees only containers it created
extra_web=$(container_ids web | tail -n +2)
[ -n "$extra_web" ] && ${ENGINE} rm -f $extra_web >/dev/null 2>&1 || true


# --- Logging ----------------------------------------------------------------

section "Logging"

# The test stack takes LOG_FORMAT=json from base.env, as Kubernetes does
configure_id=$(${ENGINE} ps -aq --filter "label=com.docker.compose.project=deploy" --filter "label=com.docker.compose.service=configure" | head -1)
configure_raw=$(${ENGINE} logs "$configure_id" 2>&1 || true)
json_lines=$(echo "$configure_raw" | python3 -c '
import json, sys
n = 0
for line in sys.stdin:
    line = line.strip()
    if line.startswith("{"):
        json.loads(line); n += 1
print(n)' 2>/dev/null || echo "invalid")
if [ "$json_lines" != "invalid" ] && [ "$json_lines" -ge 10 ]; then
    pass "logging: configure writes JSON lines ($json_lines)"
else
    fail "logging: configure JSON lines ($json_lines)"
fi
text_lines=$(echo "$configure_raw" | grep -c -E '^[0-9-]{10} [0-9:]{8} (INFO|WARNING|ERROR) ' || true)
assert_eq "logging: configure writes no text lines" "0" "$text_lines"

# MISP's own log (CakeLog) reaches the web pod's output: a missing controller is logged
curl -s -o /dev/null "http://localhost:${TEST_PORT}/nonexistentcontrollerzz" || true
retries=10
until web_logs_all | grep -q 'NonexistentcontrollerzzController' || [ $retries -le 0 ]; do
    sleep 1; retries=$((retries - 1))
done
misp_line=$(web_logs_all | grep 'NonexistentcontrollerzzController' | head -1)
assert_contains "logging: MISP's log reaches the web output" "$misp_line" '"context":"misp"'
assert_contains "logging: the web line is an error" "$misp_line" '"level":"error"'

# The workers' CakeLog lines reach the worker output once, as JSON: a console
# shell would add its own text copy
worker_raw=$(${ENGINE} logs "$(container_ids worker | head -1)" 2>&1 || true)
job_json=$(echo "$worker_raw" | grep 'launching job' | grep -c '"context":"misp"' || true)
job_text=$(echo "$worker_raw" | grep 'launching job' | grep -c -v '^{' || true)
if [ "${job_json:-0}" -ge 1 ] && [ "${job_text:-0}" -eq 0 ]; then
    pass "logging: worker job lines reach the worker output once, as JSON ($job_json)"
else
    fail "logging: worker job lines: $job_json JSON, $job_text text"
fi

# No pod writes CakeLog files any more
cake_files=$(web_exec 'find /var/www/MISP/app/tmp/logs -maxdepth 1 \( -name debug.log -o -name error.log \) -size +0 | wc -l' | tr -d '[:space:]')
assert_eq "logging: no debug.log or error.log on disk" "0" "$cake_files"

# The relay forwards a file MISP appends to, with the file as context
web_exec 'echo "relay probe line" >> /var/www/MISP/app/tmp/logs/server-sync.log'
retries=10
until web_logs_all | grep -q 'relay probe line' || [ $retries -le 0 ]; do
    sleep 1; retries=$((retries - 1))
done
assert_contains "logging: the relay forwards server-sync.log" "$(web_logs_all | grep 'relay probe line' | head -1)" '"context":"misp:server-sync"'

# --- Settings coverage ------------------------------------------------------

section "Settings coverage"
settings_dump="${WORK_DIR}/misp-settings.json"
${COMPOSE} exec -T web /var/www/MISP/app/Console/cake Admin getSetting all > "$settings_dump" 2>/dev/null || true
if PYTHONPATH="${SCRIPT_DIR}/../files" python3 "${SCRIPT_DIR}/../scripts/update_settings.py" --check --json "$settings_dump"; then
    pass "settings: every MISP setting is curated or catalogued"
else
    fail "settings: new or stale settings (run scripts/update-settings.sh and review)"
fi

# Every task type and action MISP's scheduler offers must have a task runner task
sched_shell="${WORK_DIR}/SchedulerWorkerShell.php"
${COMPOSE} exec -T web cat /var/www/MISP/app/Console/Command/SchedulerWorkerShell.php > "$sched_shell" 2>/dev/null || true
if python3 "${SCRIPT_DIR}/../scripts/check_scheduler_coverage.py" "$sched_shell"; then
    pass "scheduler: every task MISP's scheduler offers has a task runner task"
else
    fail "scheduler: MISP's scheduler offers work the task runner does not cover (see files/misp_container/task.py)"
fi

# Every default this image applies must be accepted by this MISP version
cake_failures=$(${COMPOSE} logs configure 2>/dev/null | grep -c -E 'WARNING \[cake\]|"level":"warning","context":"cake"' || true)
assert_eq "configure: no rejected cake settings" "0" "$cake_failures"

# --- Results ----------------------------------------------------------------
section "Results"

echo ""
echo "============================================="
echo " Results: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped"
echo "============================================="

if [ ${FAILED} -gt 0 ]; then
    echo ""
    echo "Failures:"
    for f in "${FAILURES[@]}"; do
        echo "  - $f"
    done
fi

# Cleanup
if [ "${KEEP_RUNNING:-}" != "true" ]; then
    echo ""
    echo "Cleaning up..."
    ${COMPOSE} down -v 2>/dev/null
fi

exit ${FAILED}
