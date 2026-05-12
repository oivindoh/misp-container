#!/bin/bash
set -euo pipefail

#
# Hub-spoke sync integration test.
# Tests MISP server synchronization with tag-based pull rules.
# Everything is configured via the sync container -- no cake CLI calls.
#
# Topology:
#   B (hub) pulls all events from A and C.
#   A pulls from B only events tagged "release-to:A".
#   C pulls from B only events tagged "release-to:C".
#
# Usage:
#   ./tests/run-sync-test.sh
#   ./tests/run-sync-test.sh --skip-build

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f ${SCRIPT_DIR}/docker-compose.sync-test.yml"

PASSED=0
FAILED=0
FAILURES=()

pass() { echo "  PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL: $1"; FAILED=$((FAILED + 1)); FAILURES+=("$1"); }
assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then pass "$desc"; else fail "$desc (expected: '$expected', got: '$actual')"; fi
}

api_post() {
    local key="$1" port="$2" path="$3" data="$4"
    curl -sf -X POST -H "Authorization: $key" -H "Accept: application/json" \
        -H "Content-Type: application/json" -d "$data" \
        "http://localhost:${port}${path}" 2>/dev/null
}
api_get() {
    local key="$1" port="$2" path="$3"
    curl -sf -H "Authorization: $key" -H "Accept: application/json" \
        "http://localhost:${port}${path}" 2>/dev/null
}

wait_for_instance() {
    local name="$1" port="$2"
    echo "  waiting for $name at :$port..."
    local retries=90
    until curl -sf -o /dev/null "http://localhost:${port}/users/login" 2>/dev/null || [ $retries -le 0 ]; do
        sleep 2; retries=$((retries - 1))
    done
    [ $retries -gt 0 ] || { echo "ERROR: $name not ready"; exit 1; }
}

wait_for_sync_jobs() {
    local mysql_svc="$1" retries=30
    while [ $retries -gt 0 ]; do
        local pending=$(${COMPOSE} exec -T "$mysql_svc" mariadb -u misp -pmisp-sync-test -N misp -e \
            "SELECT COUNT(*) FROM jobs WHERE status IN (1,2);" 2>/dev/null | tr -d '[:space:]')
        [ "${pending:-1}" = "0" ] && return 0
        sleep 2; retries=$((retries - 1))
    done
    return 1
}

# Run the sync container against a specific instance
run_sync() {
    local inst="$1" key="$2" yaml="$3"
    ${COMPOSE} run --rm -T \
        -e ADMIN_KEY="${key}" \
        -e SYNC_BASE_URL="http://${inst}-caddy:8080" \
        -e MYSQL_HOST="${inst}-mysql" \
        -e MYSQL_PORT=3306 \
        -e MYSQL_USER=misp \
        -e MYSQL_PASSWORD=misp-sync-test \
        -e MYSQL_DATABASE=misp \
        -e MISP_REDIS_HOST="${inst}-redis" \
        -e ORG_CONFIG_FILE=/etc/misp-docker/orgs.yaml \
        -v "${yaml}:/etc/misp-docker/orgs.yaml:ro" \
        sync 2>&1 | tail -3
}

echo "============================================="
echo " MISP Hub-Spoke Sync Test"
echo "============================================="
echo ""

${COMPOSE} down -v 2>/dev/null || true

if [ "${1:-}" != "--skip-build" ]; then
    echo "Building images..."
    ${COMPOSE} build --quiet 2>&1
fi

echo "Starting 3 MISP instances..."
${COMPOSE} up -d 2>&1
echo ""
echo "--- Waiting for instances ---"
wait_for_instance "A" 18091
wait_for_instance "B" 18092
wait_for_instance "C" 18093
pass "all instances ready"

echo ""
echo "--- Phase 1: Configure everything via sync container ---"

# Get org UUIDs and admin keys via the sync container (direct DB access)
ORG_A_UUID=$(${COMPOSE} exec -T a-mysql mariadb -u misp -pmisp-sync-test -N misp -e "SELECT uuid FROM organisations WHERE id=1;" 2>/dev/null | tr -d '[:space:]')
ORG_B_UUID=$(${COMPOSE} exec -T b-mysql mariadb -u misp -pmisp-sync-test -N misp -e "SELECT uuid FROM organisations WHERE id=1;" 2>/dev/null | tr -d '[:space:]')
ORG_C_UUID=$(${COMPOSE} exec -T c-mysql mariadb -u misp -pmisp-sync-test -N misp -e "SELECT uuid FROM organisations WHERE id=1;" 2>/dev/null | tr -d '[:space:]')
echo "  org UUIDs: A=$ORG_A_UUID B=$ORG_B_UUID C=$ORG_C_UUID"

# Admin keys: read the auto-generated initial key from each instance's DB.
# MISP stores only the hash, so we create a known key via the sync container.
ADMIN_KEY_A="adminKeyForA0000000000000000000000000000"
ADMIN_KEY_B="adminKeyForB0000000000000000000000000000"
ADMIN_KEY_C="adminKeyForC0000000000000000000000000000"

for inst_key in "a:${ADMIN_KEY_A}" "b:${ADMIN_KEY_B}" "c:${ADMIN_KEY_C}"; do
    IFS=: read -r inst key <<< "$inst_key"
    ${COMPOSE} run --rm -T \
        -e MYSQL_HOST="${inst}-mysql" -e MYSQL_USER=misp \
        -e MYSQL_PASSWORD=misp-sync-test -e MYSQL_DATABASE=misp \
        sync python3 -c "
import bcrypt, pymysql, uuid
key = '${key}'
h = bcrypt.hashpw(key.encode(), bcrypt.gensalt(12)).decode().replace('\$2b\$', '\$2y\$')
conn = pymysql.connect(host='${inst}-mysql', user='misp', password='misp-sync-test', database='misp')
with conn.cursor() as cur:
    cur.execute('DELETE FROM auth_keys WHERE user_id = 1')
    cur.execute('INSERT INTO auth_keys (uuid, authkey, authkey_start, authkey_end, created, user_id, expiration) VALUES (%s, %s, %s, %s, UNIX_TIMESTAMP(), 1, 0)', (str(uuid.uuid4()), h, key[:4], key[-4:], ))
conn.commit()
" 2>/dev/null
done
echo "  admin keys set via sync container"

# Sync user keys
SYNC_KEY_A_FOR_B="syncKeyAforB0000000000000000000000000000"
SYNC_KEY_B_FOR_A="syncKeyBforA0000000000000000000000000000"
SYNC_KEY_B_FOR_C="syncKeyBforC0000000000000000000000000000"
SYNC_KEY_C_FOR_B="syncKeyCforB0000000000000000000000000000"

# Create events WITH ATTRIBUTES on A and C (minimal=1 requires attribute_count>0)
EVENT_A=$(api_post "$ADMIN_KEY_A" 18091 "/events/add" \
    '{"Event":{"info":"Event from A","distribution":3,"Attribute":[{"category":"Network activity","type":"ip-dst","value":"10.0.0.1"}]}}')
EVENT_A_ID=$(echo "$EVENT_A" | jq -r '.Event.id // empty')
api_post "$ADMIN_KEY_A" 18091 "/events/publish/${EVENT_A_ID}" '{}' > /dev/null
echo "  A: created + published event $EVENT_A_ID (with attribute)"

EVENT_C=$(api_post "$ADMIN_KEY_C" 18093 "/events/add" \
    '{"Event":{"info":"Event from C","distribution":3,"Attribute":[{"category":"Network activity","type":"ip-dst","value":"10.0.0.2"}]}}')
EVENT_C_ID=$(echo "$EVENT_C" | jq -r '.Event.id // empty')
api_post "$ADMIN_KEY_C" 18093 "/events/publish/${EVENT_C_ID}" '{}' > /dev/null
echo "  C: created + published event $EVENT_C_ID (with attribute)"

# Configure sync via YAML on each instance
cat > /tmp/sync-test-a.yaml <<EOF
teams:
  - uuid: "${ORG_A_UUID}"
    name: "Org-A"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-b@a.test
        authkey: "${SYNC_KEY_A_FOR_B}"
    servers:
      - name: "Hub B"
        url: "http://b-caddy:8080"
        authkey: "${SYNC_KEY_B_FOR_A}"
        pull: true
        push: false
        pull_tags:
          - "release-to:A"
EOF

cat > /tmp/sync-test-b.yaml <<EOF
tags:
  - name: "release-to:A"
    colour: "#0000ff"
  - name: "release-to:C"
    colour: "#ff0000"

teams:
  - uuid: "${ORG_B_UUID}"
    name: "Org-B"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-a@b.test
        authkey: "${SYNC_KEY_B_FOR_A}"
      - email: sync-c@b.test
        authkey: "${SYNC_KEY_B_FOR_C}"
    servers:
      - name: "Spoke A"
        url: "http://a-caddy:8080"
        authkey: "${SYNC_KEY_A_FOR_B}"
        pull: true
        push: false
      - name: "Spoke C"
        url: "http://c-caddy:8080"
        authkey: "${SYNC_KEY_C_FOR_B}"
        pull: true
        push: false
EOF

cat > /tmp/sync-test-c.yaml <<EOF
teams:
  - uuid: "${ORG_C_UUID}"
    name: "Org-C"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-b@c.test
        authkey: "${SYNC_KEY_C_FOR_B}"
    servers:
      - name: "Hub B"
        url: "http://b-caddy:8080"
        authkey: "${SYNC_KEY_B_FOR_C}"
        pull: true
        push: false
        pull_tags:
          - "release-to:C"
EOF

echo "  configuring A via sync..."
run_sync "a" "$ADMIN_KEY_A" "/tmp/sync-test-a.yaml"
echo "  configuring B via sync..."
run_sync "b" "$ADMIN_KEY_B" "/tmp/sync-test-b.yaml"
echo "  configuring C via sync..."
run_sync "c" "$ADMIN_KEY_C" "/tmp/sync-test-c.yaml"
pass "all instances configured via sync container"

echo ""
echo "--- Phase 2: B pulls from A and C ---"

B_SERVERS=$(api_get "$ADMIN_KEY_B" 18092 "/servers")
B_SERVER_A_ID=$(echo "$B_SERVERS" | jq -r '.[] | select(.Server.name=="Spoke A") | .Server.id')
B_SERVER_C_ID=$(echo "$B_SERVERS" | jq -r '.[] | select(.Server.name=="Spoke C") | .Server.id')
echo "  B servers: A=$B_SERVER_A_ID, C=$B_SERVER_C_ID"

api_get "$ADMIN_KEY_B" 18092 "/servers/pull/${B_SERVER_A_ID}/full" > /dev/null || true
echo "  B: triggered pull from A"
api_get "$ADMIN_KEY_B" 18092 "/servers/pull/${B_SERVER_C_ID}/full" > /dev/null || true
echo "  B: triggered pull from C"

sleep 10
wait_for_sync_jobs "b-mysql" || true

# Show pull results
${COMPOSE} exec -T b-mysql mariadb -u misp -pmisp-sync-test -N misp -e \
    "SELECT message FROM jobs WHERE job_type='pull' ORDER BY id;" 2>/dev/null

B_EVENTS=$(api_get "$ADMIN_KEY_B" 18092 "/events/index")
B_HAS_A=$(echo "$B_EVENTS" | jq '[.[] | select(.info=="Event from A")] | length')
B_HAS_C=$(echo "$B_EVENTS" | jq '[.[] | select(.info=="Event from C")] | length')
assert_eq "B pulled event from A" "1" "$B_HAS_A"
assert_eq "B pulled event from C" "1" "$B_HAS_C"

echo ""
echo "--- Phase 3: Tag events on B for selective distribution ---"

B_EVENT_A_ID=$(echo "$B_EVENTS" | jq -r '.[] | select(.info=="Event from A") | .id')
B_EVENT_C_ID=$(echo "$B_EVENTS" | jq -r '.[] | select(.info=="Event from C") | .id')
echo "  event IDs on B: A=$B_EVENT_A_ID, C=$B_EVENT_C_ID"

# Tag events using POST body (no tag ID lookup needed)
api_post "$ADMIN_KEY_B" 18092 "/events/addTag" \
    "{\"event\":\"${B_EVENT_A_ID}\",\"tag\":\"release-to:A\"}" > /dev/null || true
api_post "$ADMIN_KEY_B" 18092 "/events/addTag" \
    "{\"event\":\"${B_EVENT_C_ID}\",\"tag\":\"release-to:C\"}" > /dev/null || true

# Republish so spokes can pull the tagged versions
api_post "$ADMIN_KEY_B" 18092 "/events/publish/${B_EVENT_A_ID}" '{}' > /dev/null || true
api_post "$ADMIN_KEY_B" 18092 "/events/publish/${B_EVENT_C_ID}" '{}' > /dev/null || true
echo "  B: tagged and republished events"

# Verify tags were applied
B_EVENT_A_TAGS=$(api_get "$ADMIN_KEY_B" 18092 "/events/view/${B_EVENT_A_ID}" | jq '[.Event.Tag[]?.name // empty] | length')
B_EVENT_C_TAGS=$(api_get "$ADMIN_KEY_B" 18092 "/events/view/${B_EVENT_C_ID}" | jq '[.Event.Tag[]?.name // empty] | length')
assert_eq "B event from A has tags" "1" "$B_EVENT_A_TAGS"
assert_eq "B event from C has tags" "1" "$B_EVENT_C_TAGS"

echo ""
echo "--- Phase 4: A and C pull from B (with tag filters) ---"

A_SERVER_B_ID=$(api_get "$ADMIN_KEY_A" 18091 "/servers" | jq -r '.[] | select(.Server.name=="Hub B") | .Server.id')
C_SERVER_B_ID=$(api_get "$ADMIN_KEY_C" 18093 "/servers" | jq -r '.[] | select(.Server.name=="Hub B") | .Server.id')

# Flush Redis on A and C to clear stale event indexes
${COMPOSE} exec -T a-redis redis-cli -a redis-sync-test FLUSHALL 2>/dev/null
${COMPOSE} exec -T c-redis redis-cli -a redis-sync-test FLUSHALL 2>/dev/null

api_get "$ADMIN_KEY_A" 18091 "/servers/pull/${A_SERVER_B_ID}/full" > /dev/null || true
echo "  A: triggered pull from B"
api_get "$ADMIN_KEY_C" 18093 "/servers/pull/${C_SERVER_B_ID}/full" > /dev/null || true
echo "  C: triggered pull from B"

sleep 10
wait_for_sync_jobs "a-mysql" || true
wait_for_sync_jobs "c-mysql" || true

echo ""
echo "--- Verifying tag-filtered pull ---"

# A should have "Event from A" (its own) but NOT "Event from C"
# (A's server has pull_tags: ["release-to:A"] so only tagged events come through)
A_EVENTS=$(api_get "$ADMIN_KEY_A" 18091 "/events/index")
A_EVENT_COUNT=$(echo "$A_EVENTS" | jq length)
A_OWN=$(echo "$A_EVENTS" | jq '[.[] | select(.info=="Event from A")] | length')
A_FOREIGN=$(echo "$A_EVENTS" | jq '[.[] | select(.info=="Event from C")] | length')
echo "  A has $A_EVENT_COUNT events total"
assert_eq "A has its own event" "1" "$A_OWN"
assert_eq "A does NOT have event from C" "0" "$A_FOREIGN"

# C should have "Event from C" (its own) but NOT "Event from A"
C_EVENTS=$(api_get "$ADMIN_KEY_C" 18093 "/events/index")
C_EVENT_COUNT=$(echo "$C_EVENTS" | jq length)
C_OWN=$(echo "$C_EVENTS" | jq '[.[] | select(.info=="Event from C")] | length')
C_FOREIGN=$(echo "$C_EVENTS" | jq '[.[] | select(.info=="Event from A")] | length')
echo "  C has $C_EVENT_COUNT events total"
assert_eq "C has its own event" "1" "$C_OWN"
assert_eq "C does NOT have event from A" "0" "$C_FOREIGN"

echo ""
echo "--- Phase 5: B pushes a tagged event to A ---"

# Enable push on B's server pointing to A, with push_tags filter
# Push rules use tag IDs (integers), not names
TAG_A_ID=$(api_get "$ADMIN_KEY_B" 18092 "/tags" | jq -r '.Tag[] | select(.name=="release-to:A") | .id')
PUSH_RULES="{\"tags\":{\"OR\":[${TAG_A_ID}],\"NOT\":[]},\"orgs\":{\"OR\":[],\"NOT\":[]}}"
api_post "$ADMIN_KEY_B" 18092 "/servers/edit/${B_SERVER_A_ID}" \
    "{\"Server\":{\"push\":true,\"push_rules\":$(echo "$PUSH_RULES" | jq -Rs .)}}" > /dev/null || true
echo "  B: enabled push to A with push_tags=[release-to:A] (tag_id=$TAG_A_ID)"

# Create two events on B: one tagged release-to:A, one not
PUSH_YES=$(api_post "$ADMIN_KEY_B" 18092 "/events/add" \
    '{"Event":{"info":"Push yes (tagged)","distribution":3,"Attribute":[{"category":"Network activity","type":"ip-dst","value":"10.0.0.99"}]}}')
PUSH_YES_ID=$(echo "$PUSH_YES" | jq -r '.Event.id // .id')
api_post "$ADMIN_KEY_B" 18092 "/events/addTag" \
    "{\"event\":\"${PUSH_YES_ID}\",\"tag\":\"release-to:A\"}" > /dev/null || true
api_post "$ADMIN_KEY_B" 18092 "/events/publish/${PUSH_YES_ID}" '{}' > /dev/null || true

PUSH_NO=$(api_post "$ADMIN_KEY_B" 18092 "/events/add" \
    '{"Event":{"info":"Push no (untagged)","distribution":3,"Attribute":[{"category":"Network activity","type":"ip-dst","value":"10.0.0.100"}]}}')
PUSH_NO_ID=$(echo "$PUSH_NO" | jq -r '.Event.id // .id')
api_post "$ADMIN_KEY_B" 18092 "/events/publish/${PUSH_NO_ID}" '{}' > /dev/null || true
echo "  B: created event $PUSH_YES_ID (tagged release-to:A) and $PUSH_NO_ID (untagged)"

# Push from B to A
api_get "$ADMIN_KEY_B" 18092 "/servers/push/${B_SERVER_A_ID}/full" > /dev/null || true
echo "  B: triggered push to A"

sleep 10
wait_for_sync_jobs "b-mysql" || true

# Verify A received only the tagged event
A_EVENTS_AFTER=$(api_get "$ADMIN_KEY_A" 18091 "/events/index")
A_HAS_TAGGED=$(echo "$A_EVENTS_AFTER" | jq '[.[] | select(.info=="Push yes (tagged)")] | length')
A_HAS_UNTAGGED=$(echo "$A_EVENTS_AFTER" | jq '[.[] | select(.info=="Push no (untagged)")] | length')
assert_eq "A received tagged push from B" "1" "$A_HAS_TAGGED"
assert_eq "A did NOT receive untagged push from B" "0" "$A_HAS_UNTAGGED"

# =============================================================================
# Layout 2: Hub-initiated push/pull
#
# B is the only initiator. A and C are passive (no server entries).
# B pulls all from A and C, tags events, then pushes selectively.
# A and C don't know B exists -- they just have sync users.
# =============================================================================

echo ""
echo "============================================="
echo " Layout 2: Hub-initiated push/pull"
echo "============================================="

echo ""
echo "--- Phase 6: Reconfigure to hub-push layout ---"

# A: remove server entries, keep sync user for B
cat > /tmp/sync-test-a2.yaml <<EOF
teams:
  - uuid: "${ORG_A_UUID}"
    name: "Org-A"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-b@a.test
        authkey: "${SYNC_KEY_A_FOR_B}"
EOF

# B: pull all from A+C, push to A with release-to:A, push to C with release-to:C
cat > /tmp/sync-test-b2.yaml <<EOF
tags:
  - name: "release-to:A"
    colour: "#0000ff"
  - name: "release-to:C"
    colour: "#ff0000"

teams:
  - uuid: "${ORG_B_UUID}"
    name: "Org-B"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-a@b.test
        authkey: "${SYNC_KEY_B_FOR_A}"
      - email: sync-c@b.test
        authkey: "${SYNC_KEY_B_FOR_C}"
    servers:
      - name: "Spoke A"
        url: "http://a-caddy:8080"
        authkey: "${SYNC_KEY_A_FOR_B}"
        pull: true
        push: true
        push_tags:
          - "release-to:A"
      - name: "Spoke C"
        url: "http://c-caddy:8080"
        authkey: "${SYNC_KEY_C_FOR_B}"
        pull: true
        push: true
        push_tags:
          - "release-to:C"
EOF

# C: remove server entries, keep sync user for B
cat > /tmp/sync-test-c2.yaml <<EOF
teams:
  - uuid: "${ORG_C_UUID}"
    name: "Org-C"
    users:
      - email: admin@admin.test
        role: admin
    sync_users:
      - email: sync-b@c.test
        authkey: "${SYNC_KEY_C_FOR_B}"
EOF

echo "  reconfiguring A (no servers, passive)..."
run_sync "a" "$ADMIN_KEY_A" "/tmp/sync-test-a2.yaml"
echo "  reconfiguring B (pull+push hub)..."
run_sync "b" "$ADMIN_KEY_B" "/tmp/sync-test-b2.yaml"
echo "  reconfiguring C (no servers, passive)..."
run_sync "c" "$ADMIN_KEY_C" "/tmp/sync-test-c2.yaml"

# Verify A and C have no active servers
A_ACTIVE_SERVERS=$(api_get "$ADMIN_KEY_A" 18091 "/servers" | jq '[.[] | select(.Server.push==true or .Server.pull==true)] | length')
C_ACTIVE_SERVERS=$(api_get "$ADMIN_KEY_C" 18093 "/servers" | jq '[.[] | select(.Server.push==true or .Server.pull==true)] | length')
assert_eq "A has no active servers (passive)" "0" "$A_ACTIVE_SERVERS"
assert_eq "C has no active servers (passive)" "0" "$C_ACTIVE_SERVERS"

echo ""
echo "--- Phase 7: Create events, B pulls, tags, pushes ---"

# Create new events on A and C
EVENT_A2=$(api_post "$ADMIN_KEY_A" 18091 "/events/add" \
    '{"Event":{"info":"Layout2 from A","distribution":3,"Attribute":[{"category":"Network activity","type":"ip-dst","value":"10.1.0.1"}]}}')
EVENT_A2_ID=$(echo "$EVENT_A2" | jq -r '.Event.id // empty')
api_post "$ADMIN_KEY_A" 18091 "/events/publish/${EVENT_A2_ID}" '{}' > /dev/null
echo "  A: created + published event $EVENT_A2_ID"

EVENT_C2=$(api_post "$ADMIN_KEY_C" 18093 "/events/add" \
    '{"Event":{"info":"Layout2 from C","distribution":3,"Attribute":[{"category":"Network activity","type":"ip-dst","value":"10.1.0.2"}]}}')
EVENT_C2_ID=$(echo "$EVENT_C2" | jq -r '.Event.id // empty')
api_post "$ADMIN_KEY_C" 18093 "/events/publish/${EVENT_C2_ID}" '{}' > /dev/null
echo "  C: created + published event $EVENT_C2_ID"

# B pulls from both
B_SERVERS2=$(api_get "$ADMIN_KEY_B" 18092 "/servers")
B_SRV_A2=$(echo "$B_SERVERS2" | jq -r '.[] | select(.Server.name=="Spoke A") | .Server.id')
B_SRV_C2=$(echo "$B_SERVERS2" | jq -r '.[] | select(.Server.name=="Spoke C") | .Server.id')

api_get "$ADMIN_KEY_B" 18092 "/servers/pull/${B_SRV_A2}/full" > /dev/null || true
api_get "$ADMIN_KEY_B" 18092 "/servers/pull/${B_SRV_C2}/full" > /dev/null || true
echo "  B: triggered pull from A and C"

sleep 10
wait_for_sync_jobs "b-mysql" || true

B_EVENTS2=$(api_get "$ADMIN_KEY_B" 18092 "/events/index")
B_HAS_A2=$(echo "$B_EVENTS2" | jq '[.[] | select(.info=="Layout2 from A")] | length')
B_HAS_C2=$(echo "$B_EVENTS2" | jq '[.[] | select(.info=="Layout2 from C")] | length')
assert_eq "B pulled layout2 event from A" "1" "$B_HAS_A2"
assert_eq "B pulled layout2 event from C" "1" "$B_HAS_C2"

# Tag events on B
B_EVT_A2_ID=$(echo "$B_EVENTS2" | jq -r '.[] | select(.info=="Layout2 from A") | .id')
B_EVT_C2_ID=$(echo "$B_EVENTS2" | jq -r '.[] | select(.info=="Layout2 from C") | .id')

api_post "$ADMIN_KEY_B" 18092 "/events/addTag" \
    "{\"event\":\"${B_EVT_A2_ID}\",\"tag\":\"release-to:A\"}" > /dev/null || true
api_post "$ADMIN_KEY_B" 18092 "/events/addTag" \
    "{\"event\":\"${B_EVT_C2_ID}\",\"tag\":\"release-to:C\"}" > /dev/null || true
api_post "$ADMIN_KEY_B" 18092 "/events/publish/${B_EVT_A2_ID}" '{}' > /dev/null || true
api_post "$ADMIN_KEY_B" 18092 "/events/publish/${B_EVT_C2_ID}" '{}' > /dev/null || true
echo "  B: tagged and republished events"

# B pushes to A and C
api_get "$ADMIN_KEY_B" 18092 "/servers/push/${B_SRV_A2}/full" > /dev/null || true
api_get "$ADMIN_KEY_B" 18092 "/servers/push/${B_SRV_C2}/full" > /dev/null || true
echo "  B: triggered push to A and C"

sleep 10
wait_for_sync_jobs "b-mysql" || true

echo ""
echo "--- Verifying hub-push selective distribution ---"

# A should have received "Layout2 from A" (via push) but NOT "Layout2 from C"
A_EVENTS2=$(api_get "$ADMIN_KEY_A" 18091 "/events/index")
A_L2_OWN=$(echo "$A_EVENTS2" | jq '[.[] | select(.info=="Layout2 from A")] | length')
A_L2_FOREIGN=$(echo "$A_EVENTS2" | jq '[.[] | select(.info=="Layout2 from C")] | length')
assert_eq "A has its layout2 event (via hub push)" "1" "$A_L2_OWN"
assert_eq "A does NOT have C's layout2 event" "0" "$A_L2_FOREIGN"

# C should have received "Layout2 from C" (via push) but NOT "Layout2 from A"
C_EVENTS2=$(api_get "$ADMIN_KEY_C" 18093 "/events/index")
C_L2_OWN=$(echo "$C_EVENTS2" | jq '[.[] | select(.info=="Layout2 from C")] | length')
C_L2_FOREIGN=$(echo "$C_EVENTS2" | jq '[.[] | select(.info=="Layout2 from A")] | length')
assert_eq "C has its layout2 event (via hub push)" "1" "$C_L2_OWN"
assert_eq "C does NOT have A's layout2 event" "0" "$C_L2_FOREIGN"

# --- Results ----------------------------------------------------------------

echo ""
echo "============================================="
echo " Results: ${PASSED} passed, ${FAILED} failed"
echo "============================================="

if [ ${FAILED} -gt 0 ]; then
    echo ""
    echo "Failures:"
    for f in "${FAILURES[@]}"; do echo "  - $f"; done
fi

rm -f /tmp/sync-test-a.yaml /tmp/sync-test-b.yaml /tmp/sync-test-c.yaml
rm -f /tmp/sync-test-a2.yaml /tmp/sync-test-b2.yaml /tmp/sync-test-c2.yaml

if [ "${KEEP_RUNNING:-}" != "true" ]; then
    echo ""
    echo "Cleaning up..."
    ${COMPOSE} down -v 2>/dev/null
fi

exit ${FAILED}
