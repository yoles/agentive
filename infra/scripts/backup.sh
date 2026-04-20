#!/bin/bash
# Agentive — pg_dump quotidien + chiffrement age
# Usage : ./infra/scripts/backup.sh [OUTPUT_DIR]
#
# Exécution via Docker (même l'opérateur backup tourne dans un container)

set -euo pipefail

OUTPUT_DIR="${1:-./backups}"
DATE=$(date +%Y%m%d-%H%M%S)
FILENAME="agentive-${DATE}.sql.age"

mkdir -p "$OUTPUT_DIR"

# pg_dump via container Postgres éphémère connecté au réseau docker-compose
docker compose exec -T db pg_dump -U agentive_owner -d agentive \
    --no-owner --no-acl --clean --if-exists --quote-all-identifiers \
    | docker run --rm -i -v "${AGE_KEY_PATH:-./age.key}:/age.key:ro" \
        ghcr.io/filosottile/age:latest \
        age --recipient "$(age-keygen -y /age.key)" \
    > "$OUTPUT_DIR/$FILENAME"

echo "✅ Backup chiffré : $OUTPUT_DIR/$FILENAME"

# Rotation : garder les 7 derniers backups
find "$OUTPUT_DIR" -name "agentive-*.sql.age" -type f -mtime +7 -delete

echo "✅ Rotation : backups > 7 jours supprimés"
