#!/bin/bash
# Agentive — Déploiement prod manuel via SSH
# Sprint 0 : stub — à compléter avec cible serveur prod

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-agentive.example.com}"
REMOTE_USER="${REMOTE_USER:-deploy}"
REMOTE_PATH="${REMOTE_PATH:-/opt/agentive}"

echo "🚀 Déploiement sur $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH"
echo "   Stub — à implémenter avant première mise en prod."
echo ""
echo "   Étapes prévues :"
echo "   1. rsync code + config vers le serveur"
echo "   2. docker compose -f docker-compose.yml -f docker-compose.prod.yml pull"
echo "   3. docker compose -f ... -f ... up -d --wait"
echo "   4. run migrations : docker compose run --rm backend uv run alembic upgrade head"
echo "   5. healthcheck post-deploy"
echo ""
echo "TODO Sprint 1+"
exit 0
