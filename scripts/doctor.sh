#!/usr/bin/env bash
# Axonate doctor — catch common misconfigurations before they bite.
# Today's main check: ADAPTER_TOKEN must MATCH between litellm and the adapter, or every
# model returns 401 "invalid adapter token". (Past incident: the value got clobbered.)
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
ok(){ echo "  OK:   $1"; }
no(){ echo "  WARN: $1"; fail=1; }

echo "== adapter token parity =="
lt=$(docker compose exec -T axonate-litellm sh -c 'printf %s "$ADAPTER_TOKEN"' 2>/dev/null)
ad=$(docker compose --profile poc exec -T axonate-adapter sh -c 'printf %s "$ADAPTER_TOKEN"' 2>/dev/null)
if [ -z "$ad" ]; then
  echo "  SKIP: adapter not running (poc profile) — nothing to compare"
elif [ "$lt" = "$ad" ] && [ -n "$lt" ]; then
  ok "litellm and adapter share ADAPTER_TOKEN"
else
  no "ADAPTER_TOKEN MISMATCH (litellm != adapter) — all models will 401. Fix .env so both match, then recreate both: docker compose --profile poc up -d axonate-litellm axonate-adapter"
fi

echo "== health =="
curl -fsS http://127.0.0.1:4100/health >/dev/null 2>&1 && ok "router /health" || no "router /health unreachable"
curl -fsS http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && ok "litellm liveness" || no "litellm liveness unreachable"

echo
[ "$fail" -eq 0 ] && echo "doctor: all good" || { echo "doctor: issues found above"; exit 1; }
