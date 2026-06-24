# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Axonate — a self-hosted, OpenAI-compatible AI gateway. One endpoint fronts multiple models
(Claude, Codex, MiniMax, later real provider APIs) behind SSO, with per-user identity,
cost/quota-aware routing, spend logging, and a usage dashboard.

CLI brand: `axonate` (alias `ax`). Subdomain: `axonate.<yourdomain>` behind Cloudflare Access.

**Current state:** all 8 phases scaffolded and locally tested (control plane up, routing,
auth verify, trace, rate limit, fallback, `ax` CLI). Remaining before go-live needs *your*
accounts: in-container `claude`/`codex` OAuth login, MiniMax key, and the Cloudflare Access +
Google IdP setup. See `STATUS.md` for the exact tested/not-tested matrix and next steps.

## Governing principle

> **The control plane is the product. The subscription adapter is scaffolding.**

Everything speaks OpenAI's `/v1/chat/completions` shape at every layer. POC → production is a
**credential swap, not a rewrite**: only `model_list` backend entries change; clients, router,
SSO, budgets, logs, and dashboard stay untouched. Model names are identical across `poc`/`prod`.

Two tracks, treat differently:
- **Permanent (production-grade now):** Cloudflare ZT + Google SSO, auth shim (verify JWT →
  user → virtual key), LiteLLM (keys/budgets/logging/UI), cost/quota-aware router, per-user
  audit + trace, thin CLI, the compose stack.
- **Disposable (delete after swap):** `services/adapter/` wrapping `claude -p` / `codex exec`.
  Keep it thin and isolated — it gets deleted. Quarantined to the `poc` profile only.

### Adapter auth (containerized — read before touching the adapter)

The adapter runs the Claude/Codex CLIs **inside the container** (image bundles node + both CLIs).
Auth is the trap:

- macOS GUI `claude` login may store creds in the **Keychain** — a Linux container can't read it.
  So **do not** rely on the host login. Log in *inside* the container; with no Keychain the CLI
  falls back to file-based creds in `$CLAUDE_CONFIG_DIR/.credentials.json` (Codex: `~/.codex/auth.json`).
- Each account's `CLAUDE_CONFIG_DIR` (and the Codex auth dir) is a **named volume**. Token lives in
  the volume, not the container layer → survives `restart`, `down`/`up`, rebuild, `--force-recreate`.
- **Footgun:** `docker compose down -v` (or `docker volume rm`) destroys the volume → forces
  re-login. Never `-v` the adapter volumes. Normal restarts are safe.
- Two Claude accounts = two volumes + two `CLAUDE_CONFIG_DIR` (acctA primary, acctB failover).
- Log in once via `docker compose --profile poc exec axonate-adapter env CLAUDE_CONFIG_DIR=/cfg/acctA claude`
  (headless/device-token flow, not browser-on-localhost).
- **Token expiry** (not restart) is the real recurring risk — OAuth tokens time out; need a periodic
  `exec` re-login routine. All of this disappears at the prod swap (real API keys, no login).
- **Claude headless auth** is an OAuth token, not a config-dir login: `make login-claude` prints it,
  `make set-claude-token TOKEN=.. SLOT=A` saves it to `CLAUDE_OAUTH_TOKEN_A`; the adapter injects it
  as `CLAUDE_CODE_OAUTH_TOKEN`. Codex uses device-auth (`make login-codex`), token in `/cfg/codex`.
- **`ADAPTER_TOKEN` is the litellm↔adapter shared secret** (`adt-…`), NOT a model credential.
  litellm and the adapter must hold the *same* value — if they drift, every model returns
  `invalid adapter token` (401). Never paste a Claude/OpenAI/OAuth token into `ADAPTER_TOKEN`
  (past incident: doing so 401'd all models). Rotate by recreating both services together.

## Architecture

Request path: `users → Cloudflare Zero Trust (Google SSO + Tunnel, no open ports) →
axonate-router (:4100, verify JWT, map user→key, cost/quota route) → axonate-litellm (:4000,
virtual keys, per-user budgets, spend logging) → model backends`. Postgres (`axonate-db`)
holds keys, spend, audit/trace. Redis is optional, off by default (seam wired only).

The router sits **in front of** LiteLLM. It handles `model: auto` (policy-driven choice) and
passes every other model name through unchanged. Routing policy lives in
`services/router/routing.yaml` — no hardcoded opinions in `router.py`.

## Build phases (sequential — gate on acceptance)

Containerized from Phase 0. Don't start a phase until the previous acceptance check passes.
Full prompts + acceptance criteria are in `PROMPTS.md`; phase detail in `PLAN.md`.

0. Scaffold (compose, Dockerfiles, Makefile, `.env.example`, this file)
1. CLI adapter (disposable, `poc` only, localhost) — `services/adapter/cli_adapter.py`
2. LiteLLM + Postgres — 3 models behind proxy
3. Cost/quota-aware router — `model: auto`
4. SSO + identity (plan mode) — `services/auth/auth_shim.py`
5. Logging + dashboard + trace
6. Thin CLI (`ax`) — `clients/ax`
7. Production swap prep — `poc`/`prod` profiles, `SWAP.md`
8. Harden to GOLIVE (plan mode) — every blocking item in `GOLIVE.md`

Working rule: build the **smallest thing that passes the acceptance check**. No speculative
features. After each phase, stop and print: what changed, how to run it, how to test it.

## Target repo layout (per PLAN.md)

```
services/
  router/    Dockerfile, router.py, routing.yaml
  adapter/   Dockerfile, cli_adapter.py   # disposable
  auth/      auth_shim.py
  litellm/   litellm_config.yaml
clients/ax                  # thin CLI
docker-compose.yml          # poc / prod profiles
config/poc.env.example  config/prod.env.example
.devcontainer/              # tools-only dev env (host Docker socket) — see below
deploy/helm/axonate/        # prod-track Helm chart (permanent services only)
.github/workflows/build-router.yml   # build+push router image to GHCR on v* tags
```

## Commands

- `make help` / `make up` / `make down` / `make logs` / `make ps`
- `make up PROFILE=poc` (default) / `make up PROFILE=prod`
- `make config` — validate compose; `make smoke` — pre-launch gate
- `make login-claude ACCT=acctA` / `make login-codex` — in-container CLI OAuth
- `make add-user EMAIL=.. BUDGET=..` — provision a LiteLLM virtual key
- `make backup` / `make restore FILE=..` — Postgres dump/restore
- `make helm-sync-config` — copy `services/litellm/litellm_config.prod.yaml` into the chart's
  `deploy/helm/axonate/files/` (chart loads it via `.Files.Get`; run after editing the prod config)
- Unit tests (no Docker): `. .venv/bin/activate && python3 tests/test_routing.py`
  — needs the `.venv` (router+adapter requirements; bare system python lacks `asyncpg`)
- Note: LiteLLM's **first** boot runs ~128 DB migrations (several minutes) — wait for
  `http://127.0.0.1:4000/health/liveliness` = 200 before testing. Subsequent boots are fast.
- LiteLLM usage UI / dashboard: `http://127.0.0.1:4000/ui` (master key).

## Dev environment & Kubernetes deploy

Two deployment substrates exist for the **permanent** services; they are alternatives, not a
rewrite — same OpenAI-compatible boundaries, same model names.

- **docker-compose** (`docker-compose.yml`) — the POC + single-host prod path. `poc` profile
  adds the disposable adapter; `prod` adds cloudflared. This is the primary path.
- **Helm chart** (`deploy/helm/axonate/`) — prod-track Kubernetes path. Ships **only** the
  permanent services (litellm, router, cloudflared, optional in-cluster Postgres). The CLI
  adapter is **never** in the chart (disposable / POC-only / ToS single-user). There is **no
  `configMode=poc`** — the chart always loads the prod litellm config; a poc config in-cluster
  would be broken (no adapter, no `ADAPTER_TOKEN`). Key invariants carried into the chart:
  router/litellm Services are `ClusterIP` (no open ports); cloudflared tunnel is the default
  ingress path (standard Ingress is an opt-in values toggle); one k8s Secret (or `existingSecret`)
  feeds both services — the `axonate.databaseUrl` helper derives litellm's `DATABASE_URL` from the
  same `secret.postgres.*` parts the router consumes (single source of truth); litellm probes use
  a high `failureThreshold` to survive the slow first-boot migrations. Validate with
  `helm lint deploy/helm/axonate` + `helm template`. See `deploy/helm/axonate/README.md`.

The **router is the only Axonate-authored image** the chart needs (litellm/cloudflared/postgres
are public). `.github/workflows/build-router.yml` builds it from `services/router/Dockerfile`
(build context = repo root, since the Dockerfile COPYs from `services/auth` and `config/`) and
pushes to `ghcr.io/<owner>/axonate-router` on a `v*` tag or manual dispatch; the run summary
prints the digest to pin in `image.router.digest`.

**Devcontainer** (`.devcontainer/`) — tools-only container that mounts the **host** Docker socket
(not Docker-in-Docker), so `make` drives the host compose stack and the OAuth-login volumes
persist across rebuilds. `postCreateCommand` provisions `.venv` + deps and seeds `.env` (only if
missing). Claude/Codex CLIs are an off-by-default optional feature; real adapter login still
happens in the adapter container.

## Conventions

- Python 3.11+, FastAPI + uvicorn, httpx, pyyaml, LiteLLM, Postgres, Docker Compose, cloudflared.
- Every service prefixed `axonate-`. `restart: unless-stopped` on all. Named volume for Postgres.
- `axonate-redis` defined but commented/profiled off until scaling needs it.
- OpenAI-compatible at every boundary. Secrets via env / Docker secrets — **never** in code,
  never a committed `.env`.

## Security defaults (stay on unless a prompt says otherwise)

- Default read-only / localhost. Claude/Codex are **read-only** by default; write access is an
  explicit `-code` profile taking a project dir.
- Origin **verifies** the Cloudflare Access JWT against team public keys — headers are not trusted.
- No open inbound ports; only the `axonate-cloudflared` tunnel reaches the gateway.
- Hard global spend ceiling + enforced per-user budgets; over-budget returns a clear error, never a hang.
- Prompt-content logging is **opt-in only** (privacy default off).

## ToS guardrail (POC only)

Sharing personal Claude/Codex/MiniMax subscriptions across users violates their terms. Keep
the POC single-user; multi-user goes live only on real API / team-tier keys (the swap).
