#!/usr/bin/env bash
#
# Regenerate files/misp-config/settings-upstream.yaml from a live MISP.
#
# Brings up the Compose test stack (images from the current tree), waits for
# MISP, runs scripts/update_settings.py --write, and tears the stack down.
#
# Usage:
#   scripts/update-settings.sh              # build, run, update, tear down
#   scripts/update-settings.sh --skip-build # reuse existing images
#   scripts/update-settings.sh --keep       # leave the stack running
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="${COMPOSE_CMD:-podman compose} -f ${REPO}/deploy/docker-compose.yml -f ${REPO}/tests/docker-compose.test.yml"
PORT=18080
BUILD=1
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --skip-build) BUILD=0 ;;
        --keep) KEEP=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

cleanup() {
    if [ "$KEEP" -eq 0 ]; then
        ${COMPOSE} down -v >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

${COMPOSE} down -v >/dev/null 2>&1 || true
if [ "$BUILD" -eq 1 ]; then
    ${COMPOSE} build
fi
${COMPOSE} up -d

echo "Waiting for MISP..."
retries=100
until curl -sf -o /dev/null "http://localhost:${PORT}/users/login" 2>/dev/null || [ $retries -le 0 ]; do
    sleep 3; retries=$((retries - 1))
done
if [ $retries -le 0 ]; then
    echo "ERROR: MISP did not become ready" >&2
    ${COMPOSE} logs --tail=50 configure web
    exit 1
fi

KEY=$(${COMPOSE} exec -T web /var/www/MISP/app/Console/cake user change_authkey test-admin@example.com 2>&1 | grep -o '[A-Za-z0-9]\{40\}')
cd "$REPO"
PYTHONPATH=files python3 scripts/update_settings.py --write --url "http://localhost:${PORT}" --key "$KEY"
