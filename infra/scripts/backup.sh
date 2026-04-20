#!/bin/bash
# Agentive — pg_dump + chiffrement age
# Usage : ./infra/scripts/backup.sh [OUTPUT_DIR]

set -euo pipefail

OUTPUT_DIR="${1:-./backups}"
DATE=$(date +%Y%m%d-%H%M%S)
FILENAME="agentive-${DATE}.sql.age"
AGE_KEY_PATH="${AGE_KEY_PATH:-./age.key}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.21}"

mkdir -p "$OUTPUT_DIR"

if [ ! -f "$AGE_KEY_PATH" ]; then
    echo "❌ Clé age absente : $AGE_KEY_PATH" >&2
    echo "   Générer : docker run --rm ${ALPINE_IMAGE} sh -c 'apk add --no-cache age >/dev/null && age-keygen' > age.key" >&2
    exit 1
fi

# Extract the age recipient (public key) INSIDE a container — `age-keygen` is
# never required on the host.
RECIPIENT=$(docker run --rm -i --entrypoint sh "$ALPINE_IMAGE" -c \
    "apk add --no-cache age > /dev/null 2>&1 && age-keygen -y" < "$AGE_KEY_PATH")

# Validate format : age recipients are `age1[a-z0-9]{58}` (bech32, 62 chars total).
if [ -z "$RECIPIENT" ] || ! printf '%s\n' "$RECIPIENT" | grep -qE '^age1[a-z0-9]{58}$'; then
    echo "❌ Recipient age invalide — attendu 'age1xxxxxx...' (62 chars bech32)" >&2
    echo "   Vérifier : cat $AGE_KEY_PATH (doit contenir 'AGE-SECRET-KEY-1...')" >&2
    exit 1
fi

TMP_FILE="$OUTPUT_DIR/.${FILENAME}.partial"
trap 'rm -f "$TMP_FILE"' EXIT

# Verify backend + db services are running + healthy
if ! docker compose ps --format '{{.Service}} {{.Health}}' | grep -qE '^db healthy$'; then
    echo "❌ Service db non healthy — `docker compose up -d` d'abord" >&2
    exit 1
fi

# pg_dump via docker-compose exec → pipe vers age container.
# On passe $RECIPIENT comme ARGUMENT positionnel ($1) au sh -c, pas par string
# interpolation → immune aux chars spéciaux / injection.
docker compose exec -T db pg_dump -U agentive_owner -d agentive \
    --no-owner --no-acl --clean --if-exists --quote-all-identifiers \
    | docker run --rm -i --entrypoint sh "$ALPINE_IMAGE" -c \
        'apk add --no-cache age >/dev/null 2>&1 && age --recipient "$1"' \
        _ "$RECIPIENT" \
    > "$TMP_FILE"

# Validate output size — empty file = pg_dump failed mid-stream
if [ ! -s "$TMP_FILE" ]; then
    echo "❌ Backup vide — pg_dump échoué ?" >&2
    exit 1
fi

# Sanity-check : file should be at least a few KB (age header + some data)
SIZE=$(wc -c < "$TMP_FILE")
if [ "$SIZE" -lt 512 ]; then
    echo "❌ Backup suspicieusement petit ($SIZE bytes) — truncation probable" >&2
    exit 1
fi

mv "$TMP_FILE" "$OUTPUT_DIR/$FILENAME"
trap - EXIT

echo "✅ Backup chiffré : $OUTPUT_DIR/$FILENAME ($SIZE bytes)"

# Rotation : garder les 7 derniers backups (maxdepth=1, no symlink traversal)
find "$OUTPUT_DIR" -maxdepth 1 -name "agentive-*.sql.age" -type f -mtime +7 -delete -print
echo "✅ Rotation : backups > 7 jours supprimés"
