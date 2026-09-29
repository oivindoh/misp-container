#!/usr/bin/env bash
set -euo pipefail

#
# Applies the Kustomize base with the mariadb and redis components to a kind
# cluster (overlay tests/kind) and runs tests/smoketest.sh against it.
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
ENGINE="${CONTAINER_CMD:-podman}"
TAG="${MISP_IMAGE_TAG:-2.5.37}"
CLUSTER="${KIND_CLUSTER:-misp-kind}"
NAMESPACE=misp
PORT=38080
# The admin key tests/kind/kustomization.yaml sets
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
    for target in job/configure deploy/web deploy/worker statefulset/mysql; do
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

section "Apply"
kustomize build "$SCRIPT_DIR/kind" | kubectl apply -f -

section "Configure Job"
deadline=$(( $(date +%s) + 900 ))
while :; do
    state=$(kc get job configure -o jsonpath='{.status.conditions[?(@.status=="True")].type}' 2>/dev/null || true)
    case "$state" in
        *Complete*) echo "configure Job succeeded"; break ;;
        *Failed*) echo "configure Job failed"; diagnose; exit 1 ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "configure Job did not finish in 15 minutes"; diagnose; exit 1
    fi
    sleep 5
done

section "Rollout"
for deploy in web worker modules metrics; do
    if ! kc rollout status "deploy/$deploy" --timeout=600s; then
        diagnose; exit 1
    fi
done

section "Smoke test"
kc port-forward svc/web "${PORT}:8080" > "$WORK_DIR/port-forward.log" 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do
    curl -sf -o /dev/null "http://localhost:${PORT}/users/login" && break
    sleep 2
done
if ! "$SCRIPT_DIR/smoketest.sh" "http://localhost:${PORT}" "$ADMIN_KEY"; then
    diagnose; exit 1
fi
echo ""
echo "kind test passed in $(( $(date +%s) - START ))s"
