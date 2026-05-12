#!/usr/bin/env bash
#
# Remote MISP instance smoke test.
# Verifies a live MISP instance is healthy via the API.
#
# Usage:
#   tests/smoketest.sh https://misp.example.com <authkey>
#   mise run smoketest https://misp.example.com
#
set -euo pipefail

BASE_URL="${1:?Usage: smoketest.sh <base_url> [authkey]}"
API_KEY="${2:-}"

# Strip trailing slash
BASE_URL="${BASE_URL%/}"

PASSED=0
FAILED=0

pass() { echo "  PASS: $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  FAIL: $1"; FAILED=$((FAILED + 1)); }

api() {
    curl -sf --max-time 10 \
        -H "Authorization: ${API_KEY}" \
        -H "Accept: application/json" \
        "${BASE_URL}${1}" 2>/dev/null
}

echo "============================================="
echo " MISP Smoke Test: ${BASE_URL}"
echo "============================================="
echo ""

# --- Connectivity ---
echo "--- Connectivity ---"
http_code=$(curl -sf -o /dev/null -w '%{http_code}' --max-time 10 "${BASE_URL}/users/login" 2>/dev/null || echo "000")
if [ "$http_code" = "200" ]; then
    pass "login page reachable (HTTP ${http_code})"
else
    fail "login page unreachable (HTTP ${http_code})"
    echo "Cannot reach ${BASE_URL} -- aborting."
    exit 1
fi

# --- Auth ---
if [ -z "$API_KEY" ]; then
    echo ""
    echo "No API key provided -- skipping authenticated tests."
    echo "Results: ${PASSED} passed, ${FAILED} failed"
    exit ${FAILED}
fi

echo ""
echo "--- Authentication ---"
user_json=$(api "/users/view/me")
email=$(echo "$user_json" | jq -r '.User.email // empty' 2>/dev/null)
if [ -n "$email" ]; then
    pass "API auth works (user: ${email})"
else
    fail "API auth failed"
    echo "Cannot authenticate -- aborting."
    exit 1
fi

role=$(echo "$user_json" | jq -r '.Role.name // empty' 2>/dev/null)
echo "  info: role=${role}"

# --- Instance info ---
echo ""
echo "--- Instance info ---"
version_json=$(api "/servers/getVersion")
version=$(echo "$version_json" | jq -r '.version // empty' 2>/dev/null)
if [ -n "$version" ]; then
    pass "MISP version: ${version}"
else
    fail "could not get version"
fi

# --- Database settings ---
echo ""
echo "--- Settings ---"
baseurl=$(api "/servers/getSetting/MISP.baseurl" | jq -r '.value // empty' 2>/dev/null)
if [ -n "$baseurl" ]; then
    pass "MISP.baseurl = ${baseurl}"
else
    fail "could not read MISP.baseurl"
fi

live=$(api "/servers/getSetting/MISP.live" | jq -r '.value // empty' 2>/dev/null)
if [ "$live" = "true" ] || [ "$live" = "1" ]; then
    pass "MISP.live = true"
else
    fail "MISP.live = ${live:-unset}"
fi

# --- Organisations ---
echo ""
echo "--- Data ---"
orgs=$(api "/organisations" | jq 'length' 2>/dev/null || echo "0")
pass "organisations: ${orgs}"

events=$(api "/events/index" | jq 'length' 2>/dev/null || echo "0")
pass "events: ${events}"

users=$(api "/admin/users/index" | jq 'length' 2>/dev/null || echo "0")
if [ "$users" != "0" ]; then
    pass "users: ${users}"
else
    # Non-admin users can't list users
    pass "users: (no admin access)"
fi

# --- Workers ---
echo ""
echo "--- Workers ---"
workers_json=$(api "/servers/getWorkers")
if [ -n "$workers_json" ]; then
    sv_status=$(echo "$workers_json" | jq -r '.supervisord_status // false' 2>/dev/null)
    if [ "$sv_status" = "true" ]; then
        pass "supervisord reachable"
    else
        fail "supervisord not reachable"
    fi
    for queue in default prio email cache update; do
        num=$(echo "$workers_json" | jq -r ".${queue}.workers | length // 0" 2>/dev/null)
        count=$(echo "$workers_json" | jq -r ".${queue}.jobCount // 0" 2>/dev/null)
        if [ "$num" -gt 0 ] 2>/dev/null; then
            pass "worker queue '${queue}': ${num} workers (${count} jobs)"
        else
            fail "worker queue '${queue}': no workers"
        fi
    done
else
    fail "could not get worker status"
fi

# --- Modules ---
echo ""
echo "--- Modules ---"
enrichment_url=$(api "/servers/getSetting/Plugin.Enrichment_services_url" | jq -r '.value // empty' 2>/dev/null)
enrichment_port=$(api "/servers/getSetting/Plugin.Enrichment_services_port" | jq -r '.value // empty' 2>/dev/null)
if [ -n "$enrichment_url" ]; then
    pass "enrichment URL: ${enrichment_url}:${enrichment_port:-6666}"
else
    fail "enrichment URL not configured"
fi

# --- Create + delete test event ---
echo ""
echo "--- Event lifecycle ---"
event_json=$(curl -s --max-time 10 -X POST \
    -H "Authorization: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d '{"Event":{"info":"smoketest-event","distribution":"0","Attribute":[{"type":"domain","value":"smoketest.example.com","to_ids":false}]}}' \
    "${BASE_URL}/events/add" 2>/dev/null || echo '{}')
event_id=$(echo "$event_json" | jq -r '.Event.id // empty' 2>/dev/null)
if [ -n "$event_id" ]; then
    pass "created test event (id=${event_id})"

    # Clean up
    curl -s --max-time 10 -X POST \
        -H "Authorization: ${API_KEY}" \
        -H "Accept: application/json" \
        "${BASE_URL}/events/delete/${event_id}" >/dev/null 2>&1
    pass "deleted test event"
else
    fail "could not create test event"
fi

# --- Results ---
echo ""
echo "============================================="
echo " Results: ${PASSED} passed, ${FAILED} failed"
echo "============================================="

exit ${FAILED}
