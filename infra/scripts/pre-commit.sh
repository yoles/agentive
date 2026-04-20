#!/bin/bash
# Git pre-commit hook — dispatche vers les hooks Docker
# Installé via `make precommit-install` dans .git/hooks/pre-commit

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

echo "🪝 Pre-commit hooks (via Docker)..."

# 1. Gitleaks (secrets detection)
bash "$REPO_ROOT/infra/scripts/hooks/gitleaks.sh" || exit 1

# 2. Python files → ruff
if git diff --cached --name-only --diff-filter=ACMR | grep -q '^backend/.*\.py$'; then
    bash "$REPO_ROOT/infra/scripts/hooks/ruff-check.sh" || exit 1
    bash "$REPO_ROOT/infra/scripts/hooks/ruff-format.sh" || exit 1
fi

# 3. Frontend files → eslint
if git diff --cached --name-only --diff-filter=ACMR | grep -qE '^frontend/.*\.(ts|tsx|js|jsx)$'; then
    bash "$REPO_ROOT/infra/scripts/hooks/eslint.sh" || exit 1
fi

echo "✅ Pre-commit passed"
