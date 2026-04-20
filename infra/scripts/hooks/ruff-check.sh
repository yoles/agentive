#!/bin/bash
# ruff check pre-commit hook — exécuté via Docker
# Image : python:3.14-slim + uv

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

# Récupérer les fichiers Python modifiés dans le backend
FILES=$(git diff --cached --name-only --diff-filter=ACMR | grep '^backend/.*\.py$' || true)

if [ -z "$FILES" ]; then
    exit 0
fi

echo "  → ruff check (Python)..."

docker run --rm -v "$REPO_ROOT/backend:/app" -w /app python:3.14-slim sh -c "
    pip install --quiet --no-cache-dir --root-user-action=ignore uv >&2 &&
    uv sync --frozen >&2 2>&1 &&
    uv run ruff check . --no-cache
"
