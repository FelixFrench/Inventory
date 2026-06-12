#!/usr/bin/env bash
# Downloads self-hosted Swagger UI assets required for /docs.
# Run once after cloning, and again when upgrading swagger-ui-dist.
# Pinned version — update the VERSION variable here when upgrading.

set -euo pipefail

VERSION="5.18.2"
DEST="frontend/swagger-ui"
BASE="https://cdn.jsdelivr.net/npm/swagger-ui-dist@${VERSION}"

mkdir -p "$DEST"

echo "Downloading swagger-ui-dist@${VERSION}..."
curl -fsSL "${BASE}/swagger-ui-bundle.js" -o "${DEST}/swagger-ui-bundle.js"
curl -fsSL "${BASE}/swagger-ui.css"        -o "${DEST}/swagger-ui.css"
echo "Done. Assets written to ${DEST}/"
