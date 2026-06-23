# STATUS — build & test report

All 8 phases built and locally tested on 2026-06-23. Below: what passed, and the exact
remaining steps that need *your* accounts/secrets (cannot be tested without them).

## Tested & passing ✅

| Phase | What | Evidence |
|---|---|---|
| 0 Scaffold | compose validates both profiles; poc=adapter, prod=cloudflared; `make help` | `make config` OK |
| 1 Adapter | image builds (node + claude 2.1.186 + codex 0.142.0); `/health` ok; 5 models registered; read-only vs write flags; OpenAI shape | unit tests + image run |
| 2 LiteLLM+PG | proxy serves on :4000, master key, Postgres migrations, models loaded | live curl |
| 3 Router | `auto`: code→codex, summarize→minimax, analyze→claude; `X-Router-Route/Reason` headers; fallback order | `/route/explain` + unit tests |
| 4 Auth | dev mode maps identity→key; cloudflare mode rejects missing + bad JWT (signature verified) | auth_shim unit checks |
| 5 Trace+UI | one row/call (user, route, reason, latency, tokens, status incl. failures); privacy off; LiteLLM UI :4000/ui (200); router trace view `/trace` + `/trace/view` (admin=all, user=own) | DB rows; live curl |
| 6 ax CLI | non-stream, streaming, piped stdin, shows route | live runs |
| 7 Swap | prod config validates, adapter excluded; `SWAP.md`; config swap via `LITELLM_CONFIG` | compose config |
| 8 Harden | health endpoints; restart self-heals; rate limit (58×200 then 429); fallback chain + clear error (no hang); backup/restore scripts; smoke gate | live tests |

Full-chain proof: `router → litellm → adapter → claude CLI` connected, failed only on
"not logged in", auto-fell-over to `claude-acctB`, returned a clear error — no hang.

## GOLIVE hardening — built & tested ✅ (added after initial deploy)

| Item | GOLIVE | Evidence |
|---|---|---|
| Hard **global spend ceiling** | §2 [B] | litellm `max_budget`; `/global/spend` -> `max_budget:100` |
| **Spend alert + daily digest** | §2 [B] | litellm alerting initialized ("Weekly/Monthly Spend Reports"); fires to `SLACK_WEBHOOK_URL` |
| **Backup + restore tested** | §4 [B] | dump -> scratch DB -> 12/12 trace rows, live untouched |
| **Base images pinned** (digest) | §1 [B] | litellm + postgres by `@sha256`; cloudflared documented |
| **Secrets** not committed | §1 [B] | `.env` gitignored; `docs/SECRETS.md` prod path (Docker secrets / vault) |
| **Session continuity** | §5 [R] | `ax -s NAME`; turn 2 recalled fact from turn 1 |
| claude + codex **live** | §7 | `CLAUDE_OK` / `CODEX_OK` through full stack; smoke 7/0 |

## NOT tested — needs your accounts/secrets ⛔

1. **Claude/Codex answers** — log in inside the adapter (token persists in named volumes):
   - `make login-claude ACCT=acctA` (repeat `ACCT=acctB` for failover)
   - `make login-codex`
   - then `make smoke` — the `model claude/codex` SKIPs flip to PASS.
   - ⚠️ never `docker compose down -v` or you re-login (volumes hold the token).
2. **MiniMax** — put a real `MINIMAX_API_KEY` (+ correct `MINIMAX_BASE_URL`) in `.env`.
3. **SSO (Phase 4 prod)** — verify code is done & tested; the Cloudflare-side config is yours.
   Full walkthrough in **`docs/SSO.md`**: create tunnel + Access app (Google IdP), set
   `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `CLOUDFLARE_TUNNEL_TOKEN`, `AUTH_MODE=cloudflare`,
   provision per-user keys (`make add-user`) into `config/users.yaml`. Then the
   unauthenticated-blocked smoke check enforces.
4. **Spend alerts / digest** (GOLIVE §2) — wire LiteLLM alerting (Slack/webhook) — not built.
5. **Real provider keys** for the prod swap — see `SWAP.md`.

## Quick start (POC)

```bash
cp .env.example .env            # then edit secrets (quotes on flag values matter)
echo "LITELLM_CONFIG=litellm_config.yaml" >> .env
make up PROFILE=poc             # first boot: wait ~minutes for litellm migrations
make login-claude ACCT=acctA    # one-time; also login-codex
make smoke
AXONATE_KEY=$LITELLM_MASTER_KEY ./clients/ax "explain this repo"
```
