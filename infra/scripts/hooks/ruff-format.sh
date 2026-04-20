#!/bin/bash
# ruff format check pre-commit hook — exécuté via Docker
# Image : python:3.14-slim + ruff (standalone, pas de deps projet)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
RUFF_VERSION="${RUFF_VERSION:-0.15.11}"

FILES=$(git diff --cached --name-only --diff-filter=ACMR | grep '^backend/.*\.py$' || true)

if [ -z "$FILES" ]; then
    exit 0
fi

echo "  → ruff format check (Python)..."

docker run --rm \
    -v "$REPO_ROOT/backend:/app:ro" \
    -w /app \
    --entrypoint sh \
    python:3.14-slim \
    -c "pip install --quiet --no-cache-dir --root-user-action=ignore ruff==$RUFF_VERSION >&2 && ruff format --check . --no-cache"
