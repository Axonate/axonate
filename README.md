<div align="center">

# Axonate

### One key, every model — routed smart.

A self-hosted, **OpenAI-compatible** gateway that fronts multiple AI models behind a single
endpoint — with SSO, per-user identity, cost/quota-aware routing, budgets, spend logging, and
a usage dashboard.

`axonate` · alias `ax` · `axonate.<yourdomain>` behind Cloudflare Access

![status](https://img.shields.io/badge/status-POC-orange)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![LiteLLM](https://img.shields.io/badge/LiteLLM-proxy-6E40C9)
![docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

</div>

---

## Why

Run one private endpoint for your whole team. Point any OpenAI-compatible client at it and get
Claude, Codex, MiniMax (and later any real provider API) — with who-used-what, hard spend
ceilings, per-user budgets, and smart routing. Built **for fun as a POC on flat subscriptions**
today, designed so the move to **real provider APIs** later is a credential swap, not a rewrite.

> **The control plane is the product. The subscription adapter is scaffolding.**

Everything speaks OpenAI's `/v1/chat/completions` at every layer, so POC → production changes
only which backend a model name points at.

## Architecture

```
  users ──► Cloudflare Zero Trust (Google SSO + Tunnel, no open ports)
                │  signed identity (verified Access JWT)
                ▼
        axonate-router  (:4100)   verify JWT · map user→key · cost/quota route · trace · rate-limit
                │
                ▼
        axonate-litellm (:4000)   virtual keys · per-user budgets · global ceiling · spend log · UI
                │
                ▼
            model backends
   POC :  CLI adapter (claude -p, codex exec) + MiniMax subscription   ← disposable
   PROD:  Anthropic API + OpenAI API + others                          ← drop-in swap
```

`axonate-db` (Postgres) holds keys, spend, and the audit/trace. Redis is an optional, off-by-default seam.

## Features

- **OpenAI-compatible** everywhere — use any existing client or SDK.
- **Smart `auto` routing** — keyword/length/cost-quota policy in `routing.yaml`; code→codex,
  bulk→minimax, reasoning→claude. Returns `X-Router-Route` / `X-Router-Reason`.
- **Identity + SSO** — Cloudflare Access JWT verified at the origin (headers never trusted).
- **Cost control** — hard global spend ceiling + enforced per-user budgets; over-budget errors, never hangs.
- **Observability** — per-call trace (user, route, latency, tokens, cost, status), trace view, LiteLLM UI.
- **Resilience** — prioritized fallback chain, per-user rate limiting, health endpoints, restart-self-heal.
- **Alerts** — real-time Slack budget/outage alerts + a clean daily spend digest.
- **Thin CLI (`ax`)** — streaming, piped stdin, session continuity.
- **Containerized** — `poc` / `prod` compose profiles; one-command ops.

## Quick start (POC)

```bash
cp .env.example .env                # set secrets (quotes on flag values matter)
echo "LITELLM_CONFIG=litellm_config.yaml" >> .env
make up PROFILE=poc                 # first boot runs LiteLLM migrations (a few minutes)

make login-claude                   # headless OAuth → make set-claude-token TOKEN=.. SLOT=A
make login-codex                    # device-auth
make smoke                          # pre-launch gate

AXONATE_KEY=$LITELLM_MASTER_KEY ./clients/ax -s work "explain this repo"
```

LiteLLM usage UI: `http://127.0.0.1:4000/ui` · Trace view: `http://127.0.0.1:4100/trace/view`

## Commands

| Command | Does |
|---|---|
| `make up` / `down` / `logs` / `ps` | stack lifecycle (`PROFILE=poc\|prod`) |
| `make smoke` | pre-launch smoke gate |
| `make login-claude` / `login-codex` | headless agent logins |
| `make add-user EMAIL=.. BUDGET=..` | provision a virtual key + budget |
| `make digest` | post the daily spend digest to Slack |
| `make backup` / `restore FILE=..` | Postgres dump / restore |

## The swap (POC → production)

Only `model_list` backends change — clients, router, SSO, budgets, logs, dashboard untouched.
See [`SWAP.md`](SWAP.md). SSO setup: [`docs/SSO.md`](docs/SSO.md). Secrets: [`docs/SECRETS.md`](docs/SECRETS.md).

## Repo layout

```
services/router/    routing + identity + trace + rate-limit + fallback
services/litellm/   proxy config (poc + prod)
services/adapter/   disposable CLI wrapper (claude -p / codex exec)
services/auth/      Cloudflare Access JWT verification
clients/ax          thin CLI
scripts/            add-user, backup, restore, smoke, spend-digest
docs/               SSO.md, SECRETS.md
```

## Status

POC, deployed and tested. See [`STATUS.md`](STATUS.md) for the tested/not-tested matrix and
[`GOLIVE.md`](GOLIVE.md) for the go-live checklist.

## License

[Apache-2.0](LICENSE) © CloudDrove Inc.
