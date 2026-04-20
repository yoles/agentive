#!/bin/bash
# Agentive — Rotation de secrets via SOPS + age
# Sprint 0 : stub — à compléter Story 1.7 + 9.3

set -euo pipefail

echo "🔐 Rotation de secrets Agentive"
echo ""
echo "Stub — à implémenter pleinement avec Stories 1.7 (auth token rotation) et 9.3 (API keys management)."
echo ""
echo "Étapes prévues :"
echo "  1. Générer nouveau AGENTIVE_API_TOKEN (openssl rand -base64 32)"
echo "  2. Chiffrer via SOPS+age dans .env.encrypted"
echo "  3. docker compose run --rm backend uv run python -m scripts.rotate_token"
echo "  4. Audit event 'token_rotated' émis"
echo ""
echo "TODO"
exit 0
