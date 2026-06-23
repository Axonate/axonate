#!/usr/bin/env bash
# Provision a LiteLLM virtual key for a user, with an enforced budget.
# Usage: ./scripts/add_user.sh alice@yourdomain.com 25.00
set -euo pipefail

EMAIL="${1:?usage: add_user.sh <email> <monthly_budget_usd>}"
BUDGET="${2:?usage: add_user.sh <email> <monthly_budget_usd>}"

# load env
[ -f .env ] && set -a && . ./.env && set +a
: "${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY in .env}"
LITELLM="${LITELLM_URL:-http://127.0.0.1:4000}"

resp=$(curl -sS -X POST "$LITELLM/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\":\"$EMAIL\",\"max_budget\":$BUDGET,\"budget_duration\":\"30d\"}")

key=$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

echo "provisioned: $EMAIL  budget=\$$BUDGET/30d"
echo "virtual key: $key"
echo
echo "Add to config/users.yaml under users::"
echo "  $EMAIL: $key"
