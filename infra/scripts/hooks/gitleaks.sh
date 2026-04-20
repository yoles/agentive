#!/bin/bash
# gitleaks pre-commit hook — exécuté via Docker
# Image : zricethezav/gitleaks (pinned version)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
GITLEAKS_IMAGE="${GITLEAKS_IMAGE:-zricethezav/gitleaks:v8.30.1}"

echo "  → gitleaks (secrets scan)..."

# `gitleaks git --staged` remplace `gitleaks protect` (déprécié v8.28+).
# `--redact` empêche les valeurs de secrets détectés d'apparaître dans les
# logs (le hook s'exécute en pre-commit, mais ça protège aussi si stdout
# est capturé par l'IDE / tmux buffers).
docker run --rm \
    -v "$REPO_ROOT:/src:ro" \
    "$GITLEAKS_IMAGE" \
    git /src --staged --pre-commit --redact --no-banner --config=/src/.gitleaks.toml
