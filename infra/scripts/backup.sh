#!/bin/bash
# Agentive — pg_dump quotidien + chiffrement age
# Usage : ./infra/scripts/backup.sh [OUTPUT_DIR]
#
# Docker-first : pg_dump via `docker compose exec`, age via container alpine
# avec le package `age` installé à la volée (aucune image pré-construite
# publique officielle — alpine + apk est le chemin le plus fiable).

set -euo pipefail

OUTPUT_DIR="${1:-./backups}"
DATE=$(date +%Y%m%d-%H%M%S)
FILENAME="agentive-${DATE}.sql.age"
AGE_KEY_PATH="${AGE_KEY_PATH:-./age.key}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.21}"

mkdir -p "$OUTPUT_DIR"

if [ ! -f "$AGE_KEY_PATH" ]; then
    echo "❌ Clé age absente : $AGE_KEY_PATH" >&2
    echo "   Générer via : docker run --rm ${ALPINE_IMAGE} sh -c 'apk add --no-cache age > /dev/null && age-keygen' > age.key" >&2
    exit 1
fi

# Extract the age recipient (public key) INSIDE a container so `age-keygen`
# is NEVER required on the host.
RECIPIENT=$(docker run --rm -i --entrypoint sh "$ALPINE_IMAGE" -c \
    "apk add --no-cache age > /dev/null 2>&1 && age-keygen -y" < "$AGE_KEY_PATH")

if [ -z "$RECIPIENT" ]; then
    echo "❌ Impossible d'extraire la clé publique depuis $AGE_KEY_PATH" >&2
    exit 1
fi

TMP_FILE="$OUTPUT_DIR/.${FILENAME}.partial"
trap 'rm -f "$TMP_FILE"' EXIT

# pg_dump via docker-compose exec, pipe en local, puis age encrypt dans un container.
docker compose exec -T db pg_dump -U agentive_owner -d agentive \
    --no-owner --no-acl --clean --if-exists --quote-all-identifiers \
    | docker run --rm -i --entrypoint sh "$ALPINE_IMAGE" -c \
        "apk add --no-cache age > /dev/null 2>&1 && age --recipient '$RECIPIENT'" \
    > "$TMP_FILE"

if [ ! -s "$TMP_FILE" ]; then
    echo "❌ Backup vide — pg_dump échoué ?" >&2
    exit 1
fi

mv "$TMP_FILE" "$OUTPUT_DIR/$FILENAME"
trap - EXIT

echo "✅ Backup chiffré : $OUTPUT_DIR/$FILENAME ($(wc -c < "$OUTPUT_DIR/$FILENAME") bytes)"

# Rotation : garder les 7 derniers backups (maxdepth=1 + type=f + ne suit PAS les symlinks)
find "$OUTPUT_DIR" -maxdepth 1 -name "agentive-*.sql.age" -type f -mtime +7 -delete -print
echo "✅ Rotation : backups > 7 jours supprimés"
