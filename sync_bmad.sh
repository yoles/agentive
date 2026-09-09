#!/usr/bin/env bash
# Copie/synchronise _bmad-output et _bmad vers ../bmad_agentive/
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$(cd "$SRC_DIR/.." && pwd)/bmad_agentive"

mkdir -p "$DEST_DIR"

for folder in "_bmad-output" "_bmad"; do
    if [ ! -d "$SRC_DIR/$folder" ]; then
        echo "Absent, ignore : $folder" >&2
        continue
    fi
    echo "Synchronisation de $folder vers $DEST_DIR/$folder"
    rsync -a --delete "$SRC_DIR/$folder/" "$DEST_DIR/$folder/"
done

echo "Termine."
