#!/usr/bin/env bash
#
# Prepare a release: bump MISP version (optional), update image tags, create git tag.
#
# Usage:
#   scripts/release.sh              # hotfix: v2.5.37-r3 -> v2.5.37-r4
#   scripts/release.sh v2.5.38      # new upstream: update Dockerfile + tag v2.5.38
#
set -euo pipefail

UPSTREAM_TAG="${1:-}"

# --- Read current MISP version from Dockerfile ---
CURRENT_TAG=$(grep '^ARG CORE_TAG=' Dockerfile | cut -d= -f2)
echo "Current MISP upstream: ${CURRENT_TAG}"

# --- If upstream tag provided, update the Dockerfile ---
if [ -n "$UPSTREAM_TAG" ]; then
    if [ "$UPSTREAM_TAG" = "$CURRENT_TAG" ]; then
        echo "Dockerfile already at ${UPSTREAM_TAG}, treating as hotfix"
    else
        echo "Updating Dockerfile: ${CURRENT_TAG} -> ${UPSTREAM_TAG}"
        sed -i '' "s/^ARG CORE_TAG=.*/ARG CORE_TAG=${UPSTREAM_TAG}/" Dockerfile
        CURRENT_TAG="$UPSTREAM_TAG"
    fi
fi

# --- Strip the leading 'v' for image tags (v2.5.37 -> 2.5.37) ---
BASE_VERSION="${CURRENT_TAG#v}"

# --- Find existing release tags on origin for this version ---
echo "Checking origin for existing tags..."
EXISTING=$(git ls-remote --tags origin "refs/tags/v${BASE_VERSION}*" 2>/dev/null \
    | sed 's|.*refs/tags/||' | sort -V)

if [ -z "$EXISTING" ]; then
    # No tags exist yet for this version
    NEXT_TAG="v${BASE_VERSION}"
    IMAGE_TAG="${BASE_VERSION}"
else
    echo "Existing tags:"
    echo "$EXISTING" | sed 's/^/  /'

    # Find the highest -rN suffix
    LATEST=$(echo "$EXISTING" | tail -1)
    if echo "$LATEST" | grep -q -- '-r'; then
        # Has revision suffix: v2.5.37-r3 -> extract 3, increment to 4
        REV=$(echo "$LATEST" | grep -o 'r[0-9]*$' | tr -d 'r')
        NEXT_REV=$((REV + 1))
        NEXT_TAG="v${BASE_VERSION}-r${NEXT_REV}"
        IMAGE_TAG="${BASE_VERSION}-r${NEXT_REV}"
    else
        # Base tag exists but no revisions yet: v2.5.37 -> v2.5.37-r1
        NEXT_TAG="v${BASE_VERSION}-r1"
        IMAGE_TAG="${BASE_VERSION}-r1"
    fi
fi

echo ""
echo "Next release tag: ${NEXT_TAG}"
echo "Image tag:        ${IMAGE_TAG}"
echo ""

# --- Update kustomization.yaml image tags ---
echo "Updating deploy/base/kustomization.yaml..."
sed -i '' "/name: ghcr.io\/oivindoh\/misp-container/{n;s/newTag: .*/newTag: ${IMAGE_TAG}/;}" \
    deploy/base/kustomization.yaml

# --- Update docker-compose.yml image tags ---
echo "Updating deploy/docker-compose.yml..."
python3 -c "
import re, glob
tag = '$IMAGE_TAG'
for path in ['deploy/docker-compose.yml'] + glob.glob('tests/docker-compose*.yml'):
    text = open(path).read()
    text = re.sub(r'(ghcr\.io/oivindoh/misp-container(?:-[a-z]+)?):[^\s]+', rf'\1:{tag}', text)
    open(path, 'w').write(text)
    print(f'  {path}')
"

# --- Verify ---
echo ""
echo "Changes:"
git diff --stat
echo ""
git diff deploy/ Dockerfile | head -50

echo ""
read -p "Create tag ${NEXT_TAG} and commit? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Aborted."
    exit 1
fi

# --- Commit and tag ---
git add Dockerfile deploy/base/kustomization.yaml deploy/docker-compose.yml tests/docker-compose*.yml
git commit -m "(chore) release ${NEXT_TAG}"
git tag "${NEXT_TAG}"

echo ""
echo "Done. To push:"
echo "  git push origin master ${NEXT_TAG}"
