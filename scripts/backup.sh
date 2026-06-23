#!/usr/bin/env bash
# Back up Postgres (keys/spend/trace) + config to ./backups/.
set -euo pipefail
[ -f .env ] && set -a && . ./.env && set +a
mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/axonate-db-$STAMP.sql.gz"

docker compose exec -T axonate-db \
  pg_dump -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip > "$OUT"

tar -czf "backups/axonate-config-$STAMP.tgz" \
  services/litellm/litellm_config.yaml services/router/routing.yaml config/ 2>/dev/null || true

echo "db dump:     $OUT"
echo "config dump: backups/axonate-config-$STAMP.tgz"
