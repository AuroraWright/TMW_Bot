#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${TMW_BACKUP_DIR:-/usr/src/tmw_bot/backups}"
CONTAINER_NAME="${TMW_CONTAINER_NAME:-discord-tmw-bot-container}"
CONTAINER_DB_PATH="${TMW_CONTAINER_DB_PATH:-/app/data/db.sqlite3}"
CONTAINER_BACKUP_DIR="${TMW_CONTAINER_BACKUP_DIR:-/app/backups}"
TIMESTAMP=$(date +%Y-%m-%d_%H%M)
BACKUP_NAME="db_${TIMESTAMP}.sqlite3"
BACKUP_FILE="${BACKUP_DIR}/${BACKUP_NAME}"
CONTAINER_BACKUP_FILE="${CONTAINER_BACKUP_DIR}/${BACKUP_NAME}"

mkdir -p "$BACKUP_DIR"

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" != "true" ]; then
    echo "BACKUP FAILED: $CONTAINER_NAME is not running" >&2
    exit 1
fi

if ! docker exec "$CONTAINER_NAME" \
    python -m scripts.database_crypto backup \
    "$CONTAINER_DB_PATH" "$CONTAINER_BACKUP_FILE"; then
    echo "BACKUP FAILED: SQLCipher backup or verification failed" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

if [ ! -s "$BACKUP_FILE" ]; then
    echo "BACKUP FAILED: backup file is empty or missing" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

HEADER=$(od -An -tx1 -N16 "$BACKUP_FILE" | tr -d ' \n')
if [ "$HEADER" = "53514c69746520666f726d6174203300" ]; then
    echo "BACKUP FAILED: backup has a plaintext SQLite header" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

chmod 600 "$BACKUP_FILE"
echo "Encrypted backup OK: $BACKUP_FILE"

# Keep only the last 30 verified backups.
mapfile -t BACKUPS < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    -name 'db_*.sqlite3' -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
if [ "${#BACKUPS[@]}" -gt 30 ]; then
    printf '%s\0' "${BACKUPS[@]:30}" | xargs -0 -r rm -f --
fi
