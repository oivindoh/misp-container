#!/usr/bin/env bash
# Lints the Helm chart, renders it with the default values, with each component
# on, with every component on and with the values a release changes most, and
# validates each render against the Kubernetes and CRD schemas.
#
# Usage: scripts/check-chart.sh
# Needs helm and kubeconform on PATH (mise install; CI downloads them).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CHART="$REPO/deploy/chart"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/schemas"

# A component is a top-level key of values.yaml with an enabled: line
COMPONENTS=$(awk '/^[A-Za-z]+:/ {key = substr($1, 1, length($1) - 1)} /^  enabled:/ {print key}' "$CHART/values.yaml")

# The Cilium and Gateway API CRDs come from the datree catalog; everything else
# from the Kubernetes schemas
validate() {
    kubeconform -strict -summary -cache "$WORK/schemas" \
        -schema-location default \
        -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
}

failed=0
check() {
    local name="$1"; shift
    printf '%-22s ' "$name"
    if ! helm template misp "$CHART" --namespace misp "$@" > "$WORK/render.yaml" 2> "$WORK/render.err"; then
        echo "helm template failed:"
        sed 's/^/    /' "$WORK/render.err"
        failed=1
        return
    fi
    if ! validate < "$WORK/render.yaml"; then
        failed=1
    fi
}

helm lint --strict "$CHART" --namespace misp
check default
all=()
for c in $COMPONENTS; do
    check "$c" --set "$c.enabled=true"
    all+=(--set "$c.enabled=true")
done
check all "${all[@]}"
check secrets-supplied --set secrets.create=false
check attachments-in-s3 --set attachments.claim=false --set env.PLUGIN_S3_BUCKET_NAME=misp
check workflow-cronjob --set cronjobs.enabled=true --set 'cronjobs.workflows.7=0 4 * * *'

exit "$failed"
