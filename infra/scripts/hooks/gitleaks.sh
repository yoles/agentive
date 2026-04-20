#!/bin/bash
# gitleaks pre-commit hook — exécuté via Docker
# Image : zricethezav/gitleaks (pinned version)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
GITLEAKS_IMAGE="${GITLEAKS_IMAGE:-zricethezav/gitleaks:v8.30.1}"

echo "  → gitleaks (secrets scan)..."

docker run --rm \
    -v "$REPO_ROOT:/src:ro" \
    "$GITLEAKS_IMAGE" \
    protect --source="/src" --staged --no-banner --config=/src/.gitleaks.toml --exit-code=1
