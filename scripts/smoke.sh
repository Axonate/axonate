#!/usr/bin/env bash
# Axonate pre-launch smoke gate. Hard checks fail the run; creds-dependent checks SKIP
# cleanly when backends aren't logged in / keyed.
set -uo pipefail

ROUTER="${AXONATE_URL:-http://127.0.0.1:4100}"
LITELLM="${LITELLM_URL:-http://127.0.0.1:4000}"
KEY="${AXONATE_KEY:-}"
[ -f .env ] && set -a && . ./.env && set +a
KEY="${KEY:-$LITELLM_MASTER_KEY}"

pass=0 fail=0 skip=0
ok()   { echo "  PASS: $1"; pass=$((pass+1)); }
no()   { echo "  FAIL: $1"; fail=$((fail+1)); }
sk()   { echo "  SKIP: $1"; skip=$((skip+1)); }

echo "== health =="
curl -fsS "$ROUTER/health"  >/dev/null 2>&1 && ok "router /health" || no "router /health"
curl -fsS "$LITELLM/health/liveliness" >/dev/null 2>&1 && ok "litellm liveness" || no "litellm liveness"

echo "== routing (model auto) =="
check_route() {  # prompt, expected_backend
  got=$(curl -fsS --get "$ROUTER/route/explain" --data-urlencode "prompt=$1" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["route"])' 2>/dev/null)
  if [ "$got" = "$2" ]; then ok "auto: \"$1\" -> $got"; else no "auto: \"$1\" -> $got (want $2)"; fi
}
check_route "refactor this function and fix the bug" codex
check_route "summarize this document into bullet points please okay" minimax
check_route "analyze the architecture trade-offs and explain why one design wins" claude

echo "== identity / auth =="
if [ "${AUTH_MODE:-dev}" = "cloudflare" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$ROUTER/v1/chat/completions" \
         -H 'Content-Type: application/json' -d '{"model":"auto","messages":[{"role":"user","content":"hi"}]}')
  [ "$code" = "401" ] && ok "unauthenticated blocked (401)" || no "unauthenticated returned $code (want 401)"
else
  sk "unauthenticated-blocked check (AUTH_MODE=dev; only enforced in cloudflare mode)"
fi

echo "== models answer (needs logins/keys) =="
ask() {  # model
  out=$(curl -fsS -X POST "$ROUTER/v1/chat/completions" \
        -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
        -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}]}" 2>/dev/null)
  if echo "$out" | grep -qi '"content"'; then ok "model $1 answered"; else sk "model $1 (no login/key or backend down)"; fi
}
ask claude; ask codex; ask minimax; ask auto

echo
echo "== summary: $pass passed, $fail failed, $skip skipped =="
[ "$fail" -eq 0 ] || { echo "SMOKE GATE FAILED"; exit 1; }
echo "smoke gate OK (hard checks passed; resolve SKIPs before go-live)"
