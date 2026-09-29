#!/bin/bash
set -euo pipefail

#
# Migration suite: seeds a MISP on MariaDB, migrates it with the migrate Job
# into a second MariaDB (same engine) and into PostgreSQL (cross engine), and
# checks each copy through the API and the database.
#
# Usage:
#   ./tests/run-migration-tests.sh               # build, then run
#   ./tests/run-migration-tests.sh --skip-build
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/../deploy" && pwd)"
CC="${COMPOSE_CMD:-podman compose}"
FILES="-f ${DEPLOY_DIR}/docker-compose.yml -f ${SCRIPT_DIR}/docker-compose.test.yml -f ${SCRIPT_DIR}/docker-compose.migrate.yml"
PROFILES="--profile migrate --profile migrate-target --profile postgres"
COMPOSE="${CC} ${FILES} ${PROFILES}"
ENGINE="${CONTAINER_CMD:-podman}"
WORK_DIR="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
TEST_PORT=28080
SKIP_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

# The fixture attachments directory the migrate service mounts as the source's files
export MIGRATE_FIXTURE_DIR="${WORK_DIR}/migrate-source-files"

PASSED=0
FAILED=0
FAILURES=()

# --- Helpers ----------------------------------------------------------------

pass() { echo "  PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL: $1"; FAILED=$((FAILED + 1)); FAILURES+=("$1"); }

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then pass "$desc"; else fail "$desc (expected: '$expected', got: '$actual')"; fi
}

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -q "$needle"; then pass "$desc"; else fail "$desc (expected to contain: '$needle')"; fi
}

SECTION_START=$(date +%s)
SECTION_NAME=""
section() {
    local now=$(date +%s)
    if [ -n "$SECTION_NAME" ]; then echo "  ($SECTION_NAME: $((now - SECTION_START))s)"; fi
    SECTION_NAME="$1"
    SECTION_START=$now
    echo ""
    echo "--- $1 ---"
}

web_exec() { ${COMPOSE} exec -T web bash -c "$1" 2>/dev/null; }
# SQL through the web container's db layer, against the database web is pointed at
db_query() {
    SQL="$1" ${COMPOSE} exec -T -e SQL web python3 -c '
import os, sys; sys.path.insert(0, "/opt")
from misp_container.env import apply_defaults
from misp_container import db
apply_defaults()
print(db.query(os.environ["SQL"]))' 2>/dev/null | tr -d '[:space:]'
}
api_get() {
    local key="$1" path="$2"
    curl -s -H "Authorization: ${key}" -H "Accept: application/json" "http://localhost:${TEST_PORT}${path}" 2>/dev/null || echo '{}'
}

wait_for_misp() {
    echo "Waiting for MISP to be ready..."
    local retries=80
    until curl -sf -o /dev/null "http://localhost:${TEST_PORT}/users/login" 2>/dev/null || [ $retries -le 0 ]; do
        sleep 3
        retries=$((retries - 1))
    done
    if [ $retries -le 0 ]; then
        echo "ERROR: MISP did not become ready in time"
        ${COMPOSE} logs --tail=50 configure web
        exit 1
    fi
    echo "MISP is ready"
}

wait_for_service() {
    local service="$1" retries=30
    until ${COMPOSE} exec -T "$service" sh -c "$2" >/dev/null 2>&1 || [ $retries -le 0 ]; do
        sleep 3
        retries=$((retries - 1))
    done
    [ $retries -gt 0 ] || { echo "ERROR: $service did not become ready"; exit 1; }
}

# The source credentials the migrate Job uses: the test stack's own database user
env_value() { grep "^$1=" "$2" | tail -1 | cut -d= -f2-; }
DB_USER="$(env_value DB_USER "${SCRIPT_DIR}/test-compose-secrets.env")"
DB_PASSWORD="$(env_value DB_PASSWORD "${SCRIPT_DIR}/test-compose-secrets.env")"
ADMIN_EMAIL="$(env_value ADMIN_EMAIL "${SCRIPT_DIR}/test-compose.env")"
MISP_UUID="$(env_value MISP_UUID "${SCRIPT_DIR}/test-compose.env")"
INBOUND_KEY=migrateinbound00000000000000000000000000

# Run the migrate Job with extra -e overrides; prints the log, returns its exit code
migrate_run() {
    ${COMPOSE} run --rm -T --no-deps \
        -e MIGRATE_SOURCE_HOST=mysql -e MIGRATE_SOURCE_USER="${DB_USER}" \
        -e MIGRATE_SOURCE_PASSWORD="${DB_PASSWORD}" -e MIGRATE_SOURCE_FILES=/mnt/source \
        "$@" migrate 2>&1 | grep -v '^[0-9a-f]\{64\}$'
    return "${PIPESTATUS[0]}"
}

# Point configure, web, worker and metrics at a target through an overlay and restart them
switch_stack() {
    local overlay="$1"
    ${CC} ${FILES} -f "${overlay}" ${PROFILES} up -d --force-recreate configure web worker metrics 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
    wait_for_misp
}

# The checks every migrated copy must pass
verify_copy() {
    local label="$1" host="$2"
    assert_eq "$label: web uses the target database" "$host" "$(web_exec 'echo $DB_HOST')"
    assert_eq "$label: MISP.live set by the configure step" "true" "$(db_query "SELECT value FROM system_settings WHERE setting='MISP.live'")"
    assert_eq "$label: migration recorded for the metrics exporter" "success" \
        "$(db_query "SELECT status FROM misp_container_sync_log WHERE operation='migrate' ORDER BY id DESC LIMIT 1")"
    for table in events attributes users organisations servers tags; do
        local var="SOURCE_COUNT_$table"
        assert_eq "$label: $table row count" "${!var}" "$(db_query "SELECT COUNT(*) FROM $table")"
    done
    assert_contains "$label: MISP.uuid carried over" "$(db_query "SELECT value FROM system_settings WHERE setting='MISP.uuid'")" "$MISP_UUID"
    assert_eq "$label: admin authkey still logs in" "$ADMIN_EMAIL" "$(api_get "$ADMIN_KEY" /users/view/me | jq -r '.User.email // empty')"
    assert_eq "$label: sync user authkey still logs in" "migrate-inbound@example.com" "$(api_get "$INBOUND_KEY" /users/view/me | jq -r '.User.email // empty')"
    assert_eq "$label: seeded org present" "Migrate Test Org" \
        "$(api_get "$ADMIN_KEY" /organisations/view/7c2f4a9e-3d5b-4e8a-9f10-6b7c8d9e0f11 | jq -r '.Organisation.name // empty')"
    assert_contains "$label: sync server present" "$(api_get "$ADMIN_KEY" /servers/index)" "Migrate Test Server"
    assert_eq "$label: event info intact" "Migration suite event" "$(api_get "$ADMIN_KEY" "/events/view/${EVENT_ID}" | jq -r '.Event.info // empty')"
    # The download endpoint serves an encrypted zip; restSearch returns the bytes base64-encoded
    local content
    content=$(curl -s -X POST -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" -H "Content-Type: application/json" \
        -d "{\"returnFormat\":\"json\",\"eventid\":\"${EVENT_ID}\",\"type\":\"attachment\",\"withAttachments\":1}" \
        "http://localhost:${TEST_PORT}/attributes/restSearch" 2>/dev/null | jq -r '.response.Attribute[0].data // empty' | base64 -d 2>/dev/null || true)
    assert_eq "$label: attachment content readable" "migration attachment content" "$content"
    assert_eq "$label: fixture attachment copied from the source files" "legacy attachment" "$(web_exec 'cat /var/www/MISP/app/attachments/999/1')"
}

# --- Setup ------------------------------------------------------------------

echo "============================================="
echo " MISP Container Migration Tests"
echo "============================================="

${COMPOSE} down -v 2>/dev/null || true
rm -rf "${MIGRATE_FIXTURE_DIR}"
mkdir -p "${MIGRATE_FIXTURE_DIR}/999" "${MIGRATE_FIXTURE_DIR}/taxonomies"
printf 'legacy attachment' > "${MIGRATE_FIXTURE_DIR}/999/1"
printf 'ships in the image' > "${MIGRATE_FIXTURE_DIR}/taxonomies/x"

if [ "$SKIP_BUILD" -eq 0 ]; then
    echo "Building images..."
    ${COMPOSE} build 2>&1
fi

section "Source stack"
${CC} ${FILES} up -d 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
wait_for_misp
ADMIN_KEY=$(${COMPOSE} exec -T web /var/www/MISP/app/Console/cake user change_authkey "${ADMIN_EMAIL}" 2>&1 | grep -o '[A-Za-z0-9]\{40\}')
[ -n "$ADMIN_KEY" ] || { echo "ERROR: no admin authkey"; exit 1; }

section "Seed content"
${COMPOSE} run --rm -T --no-deps -e ADMIN_KEY="${ADMIN_KEY}" -e SYNC_BASE_URL="http://caddy:8080" \
    -v "${SCRIPT_DIR}/migrate-orgs.yaml:/etc/misp-docker/orgs.yaml:ro" sync 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
assert_eq "seed: sync user logs in with its authkey" "migrate-inbound@example.com" "$(api_get "$INBOUND_KEY" /users/view/me | jq -r '.User.email // empty')"
event=$(curl -s -X POST -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" -H "Content-Type: application/json" \
    -d '{"info":"Migration suite event","distribution":"0","threat_level_id":"4","analysis":"0"}' \
    "http://localhost:${TEST_PORT}/events/add" 2>/dev/null)
EVENT_ID=$(echo "$event" | jq -r '.Event.id // empty')
[ -n "$EVENT_ID" ] && pass "seed: event created (id=$EVENT_ID)" || { fail "seed: event created"; EVENT_ID=0; }
attr=$(curl -s -X POST -H "Authorization: ${ADMIN_KEY}" -H "Accept: application/json" -H "Content-Type: application/json" \
    -d "{\"event_id\":\"${EVENT_ID}\",\"category\":\"Payload delivery\",\"type\":\"attachment\",\"value\":\"migrate.txt\",\"data\":\"$(printf 'migration attachment content' | base64)\"}" \
    "http://localhost:${TEST_PORT}/attributes/add/${EVENT_ID}" 2>/dev/null)
ATTR_ID=$(echo "$attr" | jq -r '.Attribute.id // empty')
[ -n "$ATTR_ID" ] && pass "seed: attachment uploaded (attr_id=$ATTR_ID)" || { fail "seed: attachment uploaded"; ATTR_ID=0; }
# bash 3 (macOS) has no associative arrays: one variable per table
for table in events attributes users organisations servers tags; do
    eval "SOURCE_COUNT_$table=\"$(db_query "SELECT COUNT(*) FROM $table")\""
done
echo "  source rows: events=${SOURCE_COUNT_events} attributes=${SOURCE_COUNT_attributes} users=${SOURCE_COUNT_users} organisations=${SOURCE_COUNT_organisations} servers=${SOURCE_COUNT_servers} tags=${SOURCE_COUNT_tags}"

section "Same engine: MariaDB to MariaDB"
${COMPOSE} up -d mysql-target 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
wait_for_service mysql-target "healthcheck.sh --connect --innodb_initialized"
set +e
out=$(migrate_run -e DB_HOST=mysql-target); rc=$?
set -e
assert_eq "mariadb: migrate exits 0" "0" "$rc"
assert_contains "mariadb: rows copied" "$out" "database copied"
assert_contains "mariadb: attachments copied" "$out" "attachments copied from /mnt/source: 1 files"
set +e
out=$(migrate_run -e DB_HOST=mysql-target); rc=$?
set -e
assert_eq "mariadb: non-empty target is refused (exit 3)" "3" "$rc"
set +e
out=$(migrate_run -e DB_HOST=mysql-target -e MISP_UUID=00000000-0000-0000-0000-000000000000); rc=$?
set -e
assert_eq "mariadb: identity mismatch is refused (exit 4)" "4" "$rc"
assert_contains "mariadb: identity mismatch names the setting" "$out" "MISP.uuid on the source differs from MISP_UUID"
set +e
out=$(migrate_run -e DB_HOST=mysql-target -e MIGRATE_FORCE=true); rc=$?
set -e
assert_eq "mariadb: MIGRATE_FORCE replaces the target (exit 0)" "0" "$rc"
assert_contains "mariadb: forced run dropped the tables first" "$out" "dropped"
switch_stack "${SCRIPT_DIR}/docker-compose.migrate-mysql.yml"
verify_copy "mariadb" "mysql-target"

section "Cross engine: MariaDB to PostgreSQL"
${COMPOSE} stop mysql-target 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
${COMPOSE} up -d postgres 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
wait_for_service postgres 'pg_isready -U "$POSTGRES_USER" -d misp'
set +e
out=$(migrate_run -e DB_ENGINE=postgres -e DB_HOST=postgres -e DB_PORT=5432); rc=$?
set -e
assert_eq "postgres: migrate exits 0" "0" "$rc"
assert_contains "postgres: rows copied" "$out" "database copied"
switch_stack "${SCRIPT_DIR}/docker-compose.postgres.yml"
verify_copy "postgres" "postgres"
assert_eq "postgres: new rows get ids past the copied ones" "1" \
    "$(db_query "SELECT COUNT(*) FROM tags WHERE id = (SELECT last_value FROM tags_id_seq)")"

section "Teardown"
${COMPOSE} down -v 2>&1 | grep -v '^[0-9a-f]\{64\}$' || true
rm -rf "${MIGRATE_FIXTURE_DIR}"

section "Results"
echo " Results: $PASSED passed, $FAILED failed"
if [ $FAILED -gt 0 ]; then
    for f in "${FAILURES[@]}"; do echo "  - $f"; done
    exit 1
fi
