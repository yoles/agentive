#!/usr/bin/env bash
# Agentive — Déploiement staging (exécuté sur le serveur idem-agency)
#
# Appelé par .github/workflows/deploy-staging.yml via SSH après que le workflow
# a rsync le code et écrit `.env.staging` sur le serveur.
#
# Préreq serveur :
#   - Traefik + `traefik_network` déployés (via infrastructure_idem_helper)
#   - User courant dans groupe docker
#   - `.env.staging` présent à la racine du deploy

set -euo pipefail

REMOTE_PATH="${REMOTE_PATH:-/opt/app/agentive/staging}"
cd "$REMOTE_PATH"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deploy agentive-staging → $REMOTE_PATH"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Préflights ───
[ -f .env.staging ] || { echo "❌ .env.staging manquant dans $REMOTE_PATH"; exit 1; }
[ -f docker-compose.staging.yml ] || { echo "❌ docker-compose.staging.yml manquant"; exit 1; }
docker network inspect traefik_network >/dev/null 2>&1 \
  || { echo "❌ traefik_network absent — deploy infrastructure_idem_helper d'abord"; exit 1; }

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.staging.yml --env-file .env.staging -p agentive-staging)

echo "▶ Pull base images (pgvector, caddy)"
"${COMPOSE[@]}" pull db || true

echo "▶ Build app images (backend + frontend prod)"
"${COMPOSE[@]}" build backend frontend

echo "▶ Start DB and wait healthy"
"${COMPOSE[@]}" up -d --wait db

echo "▶ Apply Alembic migrations (ephemeral container, no-deps)"
"${COMPOSE[@]}" run --rm --no-deps backend uv run alembic upgrade head

echo "▶ Start backend + frontend and wait healthy"
"${COMPOSE[@]}" up -d --wait backend frontend

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ Deploy OK — status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
"${COMPOSE[@]}" ps
