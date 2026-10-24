#!/usr/bin/env bash
#
# Resolve MISP's composer dependencies for the current CORE_TAG plus this
# image's extra packages, and write the result to files/composer.lock.
# The image build installs exactly that lock and fails when it is out of date
# with upstream's composer.json (a new MISP release), so run this on every bump.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGINE="${CONTAINER_CMD:-podman}"
IMAGE=localhost/misp-container-composer-lock

# The Dockerfile's composer-lock stage resolves the lock
${ENGINE} build --target composer-lock --tag "${IMAGE}" "${REPO}"
${ENGINE} run --rm "${IMAGE}" cat /tmp/composer.lock > "${REPO}/files/composer.lock.new"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${REPO}/files/composer.lock.new"
mv "${REPO}/files/composer.lock.new" "${REPO}/files/composer.lock"
echo "wrote files/composer.lock ($(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['packages']))" "${REPO}/files/composer.lock") packages)"
