# Axonate — PLAN.md

> **One key, every model — routed smart.**
> CLI: `axonate` (alias `ax`) · Subdomain: `axonate.<yourdomain>` (behind Cloudflare Access)

## Goal
A self-hosted, OpenAI-compatible gateway that fronts multiple AI models behind one
endpoint, with SSO login, per-user identity, cost/quota-aware routing, logging, and a
usage dashboard. Built **for fun as a POC on flat subscriptions** today, designed so the
move to **real, controllable APIs** later is a credential swap — not a rewrite.

## Guiding principle
> The control plane is the product. The subscription adapter is scaffolding.

Everything speaks OpenAI's `/v1/chat/completions` shape, so POC → production changes only
which backend a model name points at. Build the control plane production-grade now; treat
the adapter as disposable.

## Decisions (locked)
- **Audience:** small trusted group (NOT open signup).  ← assumption; flip if wrong
- **Cost control:** hard global spend ceiling + enforced per-user budgets.  ← assumption; flip if wrong
- Subscriptions for POC; real provider APIs for production.
- OpenAI-compatible at every layer (clean swap).
- Python 3.11+ / FastAPI + LiteLLM.
- Router sits in front of LiteLLM.
- Claude/Codex default to **read-only**; write access is an explicit profile.
- SSO via Cloudflare Access (Google IdP); origin **verifies** the Access JWT.
- Deployed with Docker + docker-compose, `poc` / `prod` profiles.
- Redis is **optional**, off by default (seam wired, enable when scaling/caching).

## Architecture
```
   users (laptop)
        |
        v
  Cloudflare Zero Trust  -- Google SSO + Tunnel (no open ports)
        |  forwards verified identity (user email / signed JWT)
        v
  +----------------------- Mac mini (docker-compose) -----------------------+
  | axonate-router    -> verify JWT, map user->key, cost/quota-aware route   |
  | axonate-litellm   -> virtual keys, per-user budgets, spend logging       |
  | axonate-db        -> postgres: keys, spend, audit/trace                  |
  | axonate-adapter   -> POC ONLY: claude -p / codex exec (disposable)       |
  | axonate-cloudflared -> the tunnel                                        |
  | (axonate-redis)   -> OPTIONAL: caching / shared rate limits (off)        |
  +------------------------------------+------------------------------------+
            |                          v
     model backends             usage dashboard
   POC:  CLI adapter (claude -p, codex exec) + MiniMax subscription
   PROD: Anthropic API + OpenAI API + others (drop-in)
```

## Deployment model (Docker)
One `docker-compose.yml` with named volumes and `restart: unless-stopped`. Profiles:
- `poc` — includes `axonate-adapter` + MiniMax subscription backends.
- `prod` — excludes the adapter; backends are real APIs.
`docker compose --profile poc up` for the POC, `--profile prod up` for production.
`axonate-redis` is defined but commented/profiled off until needed.

## Two tracks
**Permanent (the product — build production-grade now):** Cloudflare ZT + Google SSO,
auth shim (verify JWT -> user -> virtual key), LiteLLM (keys/budgets/logging/UI),
cost/quota-aware router, per-user audit + trace view, thin CLI, the compose stack.

**Disposable (scaffolding — delete after swap):** `axonate-adapter` wrapping `claude -p`
and `codex exec` so the POC has something to talk to before real API keys exist.

## The swap (POC -> production)
Only `model_list` entries change. Clients, router, SSO, budgets, logs, dashboard untouched.

| Model name | POC backend | Production backend |
|---|---|---|
| `claude` | `openai/claude-acctA` -> adapter | `anthropic/claude-sonnet-4-6` + API key |
| `codex`  | `openai/codex` -> adapter | `openai/gpt-5.x` + API key |
| `minimax`| `minimax/MiniMax-M2.5` (subscription key) | same model, API/PAYG key |
| `deepseek` (optional) | n/a in POC | `deepseek/deepseek-v4-pro` + API key |

(Verify exact model strings at build time — they drift.)

## Build phases
Containerized from Phase 0. Each phase is independently testable; don't start one until
the previous acceptance check passes.

- **Phase 0 — Scaffold (containerized).** Repo layout, `CLAUDE.md`, Dockerfile per service,
  `docker-compose.yml` (poc/prod profiles, redis commented), `.env.example`, Makefile.
  *Accept:* `make up` brings a healthy skeleton stack online; no secrets committed.
- **Phase 1 — CLI adapter (disposable, poc profile).** OpenAI-compatible wrapper for
  `claude -p` + `codex exec`; read-only + write profiles; localhost only. *Accept:* curl
  each model and get an answer; write profile edits a file, read-only can't.
- **Phase 2 — LiteLLM + Postgres.** MiniMax (subscription) + adapter backends; virtual
  keys; master key; DB volume. *Accept:* all 3 models answer through the proxy.
- **Phase 3 — Cost/quota-aware router.** `model: auto` in front of LiteLLM; decision logging;
  `X-Router-*` headers; budget/quota signal in the choice. *Accept:* `auto` routes sensibly
  and logs why.
- **Phase 4 — SSO + identity.** `cloudflared` tunnel + Access (Google IdP); auth shim
  verifies the Access JWT and maps email -> virtual key; per-user budgets. *Accept:*
  unauthenticated blocked; authenticated attributed + metered.
- **Phase 5 — Logging + dashboard + trace.** Per-user audit log; one trace view (user,
  model, route, latency, tokens, cost, status); LiteLLM UI for usage. *Accept:* per-user
  usage visible and matches the trace.
- **Phase 6 — Thin CLI (`ax`).** `ax "..."` (auto), `ax -m claude "..."`, piped stdin,
  streaming. *Accept:* all usages work end to end.
- **Phase 7 — Production swap prep.** poc/prod profiles; prod API entries mirroring POC
  names; adapter quarantined; `SWAP.md`. *Accept:* profile flip changes backends, zero
  client change.
- **Phase 8 — Harden to GOLIVE.** Streaming, session memory, fallback chain, human-readable
  errors; then walk every blocking item in `GOLIVE.md`. *Accept:* all GOLIVE blocking items checked.

## "Nice" layer — sorted
- **Go-live:** streaming, session continuity, provider fallback, human-readable errors, trace view.
- **Fast-follow:** observability (latency/error per model), spend alerts + digest, one-command ops, rollback path.
- **Deferred:** polished custom dashboard, Redis/caching, multi-replica, RBAC beyond allow/deny,
  prompt library, playground, BYO-key per user, multi-region. (Each has a documented seam.)

## Risks & guardrails
- **POC ToS.** Sharing personal subscriptions across users violates Claude/Codex/MiniMax
  terms. Keep the POC to yourself; multi-user goes live only on real API/team tier.
- **Security.** Network-reachable agent can run shell/file ops. Localhost behind the tunnel;
  verify the Access JWT; Claude/Codex read-only unless a request uses the write profile;
  secrets in a store / Docker secrets, never in code.
- **Auth durability (POC only).** Headless Claude/Codex logins expire; document a
  device-code / re-login routine. Disappears at the swap.
- **Don't over-build.** Smallest thing that passes each acceptance check. Keep the adapter
  thin — it's going to be deleted.

## Tech stack
Python 3.11+, FastAPI + uvicorn, httpx, pyyaml, LiteLLM, Postgres, Docker + docker-compose,
cloudflared, Cloudflare Access (Google IdP). Redis optional.

## Repo layout
```
axonate/
  services/
    router/        Dockerfile, router.py, routing.yaml
    adapter/       Dockerfile, cli_adapter.py        # disposable
    auth/          auth_shim.py
    litellm/       litellm_config.yaml
  clients/ax       # thin CLI
  docker-compose.yml          # poc / prod profiles
  config/poc.env.example
  config/prod.env.example
  CLAUDE.md
  PLAN.md  PROMPTS.md  GOLIVE.md  SWAP.md
```
