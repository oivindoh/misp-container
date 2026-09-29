#!/usr/bin/env bash
# Renders the Kustomize base alone, with each component, and with every
# component together, and validates each render against the Kubernetes and
# CRD schemas.
#
# Usage: scripts/check-kustomize.sh
# Needs kustomize and kubeconform on PATH (mise install; CI downloads them).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Components are tried on a copy, so the check writes nothing into the tree
cp -R "$REPO/deploy" "$WORK/"
mkdir -p "$WORK/schemas"

COMPONENTS=$(cd "$REPO/deploy/components" && ls -d */ | tr -d /)

# The Cilium CRDs come from the datree catalog; everything else from the
# Kubernetes schemas
validate() {
    kubeconform -strict -summary -cache "$WORK/schemas" \
        -schema-location default \
        -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
}

overlay() {
    local name="$1"; shift
    local dir="$WORK/deploy/overlays/check-$name"
    mkdir -p "$dir"
    {
        echo "apiVersion: kustomize.config.k8s.io/v1beta1"
        echo "kind: Kustomization"
        echo "resources: [../../base]"
        if [ "$#" -gt 0 ]; then
            echo "components:"
            for c in "$@"; do echo "  - ../../components/$c"; done
        fi
    } > "$dir/kustomization.yaml"
    echo "$dir"
}

failed=0
check() {
    local name="$1"; shift
    local dir
    dir=$(overlay "$name" "$@")
    printf '%-18s ' "$name"
    if ! kustomize build "$dir" > "$dir/render.yaml" 2> "$dir/build.err"; then
        echo "kustomize build failed:"
        sed 's/^/    /' "$dir/build.err"
        failed=1
        return
    fi
    if ! validate < "$dir/render.yaml"; then
        failed=1
    fi
}

check base
for c in $COMPONENTS; do
    check "$c" "$c"
done
# shellcheck disable=SC2086
check all $COMPONENTS

exit "$failed"
