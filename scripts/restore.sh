#!/usr/bin/env bash
# Restore Postgres from a dump produced by backup.sh.
# Usage: ./scripts/restore.sh backups/axonate-db-YYYYmmdd-HHMMSS.sql.gz
set -euo pipefail
FILE="${1:?usage: restore.sh <dump.sql.gz>}"
[ -f .env ] && set -a && . ./.env && set +a

echo "Restoring $FILE into ${POSTGRES_DB}..."
gunzip -c "$FILE" | docker compose exec -T axonate-db \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
echo "restore complete. Verify with: make smoke"
