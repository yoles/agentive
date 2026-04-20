#!/bin/bash
# eslint pre-commit hook — exécuté via Docker
# Image : node:24-alpine + named volume cache pour node_modules
#
# On cache `node_modules` dans un volume Docker nommé (`agentive_precommit_node_modules`)
# pour éviter le `npm ci` à chaque commit. Le cache est invalidé manuellement via
# `docker volume rm agentive_precommit_node_modules` quand package-lock.json change.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
NODE_IMAGE="${NODE_IMAGE:-node:24-alpine}"
CACHE_VOLUME="${CACHE_VOLUME:-agentive_precommit_node_modules}"

# Short-circuit if no frontend files are staged.
FILES=$(git diff --cached --name-only --diff-filter=ACMR | grep -E '^frontend/.*\.(ts|tsx|js|jsx)$' || true)

if [ -z "$FILES" ]; then
    exit 0
fi

echo "  → eslint (Frontend)..."

# Filter staged files to absolute paths inside the container (eslint only these).
# If eslint's config requires more context (e.g. tsconfig), we fall back to lint
# the whole workspace but it's still cheaper than npm ci on every run.
FILES_IN_CONTAINER=$(echo "$FILES" | sed 's|^frontend/||' | tr '\n' ' ')

docker run --rm \
    -v "$REPO_ROOT/frontend:/app" \
    -v "$CACHE_VOLUME:/app/node_modules" \
    -w /app \
    "$NODE_IMAGE" \
    sh -c "
        if [ ! -d node_modules/eslint ]; then
            echo '    (cold cache: npm ci — lancement unique, cache persisté dans volume $CACHE_VOLUME)' >&2;
            npm ci --silent;
        fi
        npx eslint $FILES_IN_CONTAINER
    "
