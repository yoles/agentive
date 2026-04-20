#!/bin/bash
# ruff check pre-commit hook — exécuté via Docker
# Image : python:3.14-slim + ruff (pas de deps projet — ruff n'en a pas besoin)
#
# On installe seulement `ruff` (binaire Rust léger, quelques Mo) plutôt que
# de faire `uv sync` avec les 150+ packages de l'app. Le check est alors :
# - rapide (< 5s après première pull)
# - offline-friendly (l'image + ruff sont cached localement après le 1er run)
# - déterministe (pas de `uv sync` qui peut varier selon l'état de PyPI)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
RUFF_VERSION="${RUFF_VERSION:-0.15.11}"

# Only lint Python files actually being committed.
FILES=$(git diff --cached --name-only --diff-filter=ACMR | grep '^backend/.*\.py$' || true)

if [ -z "$FILES" ]; then
    exit 0
fi

echo "  → ruff check (Python)..."

docker run --rm \
    -v "$REPO_ROOT/backend:/app:ro" \
    -w /app \
    --entrypoint sh \
    python:3.14-slim \
    -c "pip install --quiet --no-cache-dir --root-user-action=ignore ruff==$RUFF_VERSION >&2 && ruff check . --no-cache"
