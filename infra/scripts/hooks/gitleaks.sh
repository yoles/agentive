#!/bin/bash
# gitleaks pre-commit hook — exécuté via Docker
# Image : zricethezav/gitleaks:latest

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

echo "  → gitleaks (secrets scan)..."

docker run --rm -v "$REPO_ROOT:/src" \
    zricethezav/gitleaks:latest \
    protect --source="/src" --staged --no-banner --config=/src/.gitleaks.toml --exit-code=1
