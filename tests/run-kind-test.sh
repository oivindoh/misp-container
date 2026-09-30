#!/usr/bin/env bash
set -euo pipefail

#
# Installs the Helm chart with the mariadb and redis components on a kind cluster
# (values tests/kind/values.yaml) and runs the smoke test (tests/e2e/test_smoke.py)
# against it. Then upgrades the release with a changed value and runs the smoke
# test again: the upgrade runs a new configure Job and rolls the pods.
#
# Usage:
#   tests/run-kind-test.sh           # create the cluster, test, delete it
#   tests/run-kind-test.sh --keep    # leave the cluster running
#
# The images must exist locally as
#   ghcr.io/oivindoh/misp-container{,-caddy,-modules}:${MISP_IMAGE_TAG:-2.5.37}
# (build them with compose first). CONTAINER_CMD selects the engine, podman by
# default; kind uses the same one. With podman, kind runs on the machine's
# rootful connection (KIND_PODMAN_CONNECTION, default podman-machine-default-root):
# a kind node needs privileges that rootless podman does not give it.
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART="$SCRIPT_DIR/../deploy/chart"
ENGINE="${CONTAINER_CMD:-podman}"
TAG="${MISP_IMAGE_TAG:-2.5.37}"
CLUSTER="${KIND_CLUSTER:-misp-kind}"
NAMESPACE=misp
PORT=38080
# The admin key tests/kind/values.yaml sets
ADMIN_KEY=kindTESTkey0123456789abcdefghijklmnopqrs
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

# Physical path: a podman machine on macOS shares /private, not the /tmp symlink
WORK_DIR="$(cd "${TMPDIR:-/tmp}" && pwd -P)/misp-kind"
mkdir -p "$WORK_DIR"
export KUBECONFIG="$WORK_DIR/kubeconfig"

kind_cmd() {
    if [ "$ENGINE" = podman ]; then
        KIND_EXPERIMENTAL_PROVIDER=podman \
            CONTAINER_CONNECTION="${KIND_PODMAN_CONNECTION:-podman-machine-default-root}" kind "$@"
    else
        kind "$@"
    fi
}
kc() { kubectl -n "$NAMESPACE" "$@"; }

PF_PID=""
cleanup() {
    [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
    if [ "$KEEP" -eq 0 ]; then
        kind_cmd delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    else
        echo "Cluster kept: KUBECONFIG=$KUBECONFIG kubectl -n $NAMESPACE get pods"
    fi
}
trap cleanup EXIT

diagnose() {
    echo "--- pods ---"
    kc get pods -o wide || true
    echo "--- events ---"
    kc get events --sort-by=.lastTimestamp 2>/dev/null | tail -30 || true
    for target in job/configure-1 job/configure-2 deploy/web deploy/worker statefulset/mysql; do
        echo "--- logs $target ---"
        kc logs "$target" --all-containers --tail=60 2>/dev/null || true
    done
}

section() { echo ""; echo "--- $1 ($(( $(date +%s) - START ))s) ---"; }
START=$(date +%s)

echo "============================================="
echo " MISP Kubernetes test (kind)"
echo "============================================="

section "Cluster"
kind_cmd delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind_cmd create cluster --name "$CLUSTER" --wait 180s

section "Images"
for repo in ghcr.io/oivindoh/misp-container ghcr.io/oivindoh/misp-container-caddy ghcr.io/oivindoh/misp-container-modules; do
    ${ENGINE} tag "${repo}:${TAG}" "${repo}:kind"
    ${ENGINE} save -o "$WORK_DIR/image.tar" "${repo}:kind"
    kind_cmd load image-archive "$WORK_DIR/image.tar" --name "$CLUSTER"
    rm -f "$WORK_DIR/image.tar"
    echo "loaded ${repo}:kind"
done

# Waits for the configure Job of a release revision, then for the Deployments
wait_for_release() {
    local job="configure-$1" deadline state
    deadline=$(( $(date +%s) + 900 ))
    while :; do
        state=$(kc get job "$job" -o jsonpath='{.status.conditions[?(@.status=="True")].type}' 2>/dev/null || true)
        case "$state" in
            *Complete*) echo "$job succeeded"; break ;;
            *Failed*) echo "$job failed"; diagnose; exit 1 ;;
        esac
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "$job did not finish in 15 minutes"; diagnose; exit 1
        fi
        sleep 5
    done
    for deploy in web worker modules metrics; do
        if ! kc rollout status "deploy/$deploy" --timeout=600s; then
            diagnose; exit 1
        fi
    done
}

# The repository's venv has pytest (uv sync)
PYTHON="$SCRIPT_DIR/../.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3
smoke_test() {
    [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
    kc port-forward svc/web "${PORT}:8080" > "$WORK_DIR/port-forward.log" 2>&1 &
    PF_PID=$!
    for _ in $(seq 1 30); do
        curl -sf -o /dev/null "http://localhost:${PORT}/users/login" && break
        sleep 2
    done
    if ! "$PYTHON" -m pytest "$SCRIPT_DIR/e2e/test_smoke.py" -v -p no:cacheprovider \
            --url "http://localhost:${PORT}" --key "$ADMIN_KEY"; then
        diagnose; exit 1
    fi
}

section "Install"
helm install misp "$CHART" --namespace "$NAMESPACE" --create-namespace -f "$SCRIPT_DIR/kind/values.yaml"

section "Configure Job and rollout"
wait_for_release 1

section "Smoke test"
smoke_test
section "Logs"
# The chart's default is LOG_FORMAT=json: every line of the configure Job is one JSON object
json_lines=$(kc logs job/configure-1 | python3 -c '
import json, sys
lines = [l for l in sys.stdin if l.strip()]
print(sum(1 for l in lines if json.loads(l)))' 2>/dev/null || echo 0)
if [ "$json_lines" -lt 10 ]; then
    echo "configure Job logs are not JSON lines ($json_lines)"; diagnose; exit 1
fi
echo "configure Job: $json_lines JSON lines"

section "Upgrade"
# A changed value: a new configure Job, the old one removed, the pods rolled
web_before=$(kc get pods -l app.kubernetes.io/name=web -o name)
helm upgrade misp "$CHART" --namespace "$NAMESPACE" -f "$SCRIPT_DIR/kind/values.yaml" \
    --set env.PHP_MAX_FILE_UPLOADS=51
wait_for_release 2
if kc get job configure-1 >/dev/null 2>&1; then
    echo "the upgrade left job/configure-1"; diagnose; exit 1
fi
if [ "$(kc get pods -l app.kubernetes.io/name=web -o name)" = "$web_before" ]; then
    echo "the upgrade did not roll the web pods"; diagnose; exit 1
fi
smoke_test

echo ""
echo "kind test passed in $(( $(date +%s) - START ))s"
