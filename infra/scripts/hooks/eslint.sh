#!/bin/bash
# eslint pre-commit hook — exécuté via Docker
# Image : node:24-alpine

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

FILES=$(git diff --cached --name-only --diff-filter=ACMR | grep -E '^frontend/.*\.(ts|tsx|js|jsx)$' || true)

if [ -z "$FILES" ]; then
    exit 0
fi

echo "  → eslint (Frontend)..."

# Utiliser le package.json du frontend pour les scripts
docker run --rm -v "$REPO_ROOT/frontend:/app" -w /app node:24-alpine sh -c "
    if [ ! -d node_modules ]; then
        npm ci --silent
    fi
    npm run lint --if-present
"
