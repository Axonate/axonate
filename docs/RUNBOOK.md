# Axonate Runbook — setup & run

Step-by-step for the **POC / local** path (the one verified end-to-end). Production swap and
Kubernetes are pointers at the end. Every command runs from the repo root.

## 0. Prerequisites

- Docker Desktop running (`docker version` prints a Server version).
- `make`, `git`.
- A Claude subscription (for the live Claude model). MiniMax/Codex are optional extra models.
- Python 3.11+ only if you want to run the unit tests (`. .venv/bin/activate`).

## 1. Configure secrets (`.env`)

```bash
cp .env.example .env
echo "LITELLM_CONFIG=litellm_config.yaml" >> .env   # poc config (adapter-backed)
```

For a **local POC** the example values work as-is: the master key, salt, Postgres password, and
`ADAPTER_TOKEN` are non-empty and internally consistent (the same `.env` feeds every service).
You do **not** need real values to boot. Only change them for real deployments.

`ADAPTER_TOKEN` is the litellm↔adapter shared secret — never paste a model/OAuth token into it.

## 2. Bring the stack up

```bash
make up PROFILE=poc      # first run builds the router + adapter images
```

First litellm boot may run DB migrations (up to several minutes). Wait for health:

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness   # litellm -> {"status":"ok"...} or 200
curl -fsS http://127.0.0.1:4100/health               # router  -> {"status":"ok",...}
```

Subsequent boots are fast.

## 3. Log the Claude model in (one-time, interactive)

`claude setup-token` is interactive — run it in a **real terminal window** (NOT through an
editor/agent `!` prompt, which has no TTY and will hang):

```bash
docker compose --profile poc exec axonate-adapter claude setup-token
```

Open the printed URL → authorize with your Claude account → paste the code back into that
terminal → copy the `sk-ant-oat...` token it prints. Then save it:

```bash
make set-claude-token TOKEN=sk-ant-oat... SLOT=A     # SLOT=B for a failover account
```

This writes `CLAUDE_OAUTH_TOKEN_A` to `.env` and recreates the adapter. The token lives in a
named volume / `.env` and survives restarts. **Never `docker compose down -v`** — that wipes the
login volumes and forces re-login.

> Security: the token is long-lived. Don't paste it into shared shells/transcripts; for real
> deployments inject via Docker secrets / a vault (`docs/SECRETS.md`), and rotate via a fresh
> `claude setup-token` if it leaks.

## 4. Verify end-to-end

```bash
make smoke      # expect: claude -> PASS; health + routing PASS
```

Live round-trip through the whole chain (client → router → litellm → adapter → claude CLI):

```bash
export AXONATE_KEY=$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
./clients/ax --model claude "Reply with exactly: AXONATE_E2E_OK"
./clients/ax "analyze the trade-offs of monolith vs microservices"   # model: auto -> routes to claude
```

`[route: <model>]` is printed before the answer. Dashboard / usage UI: http://127.0.0.1:4000/ui
(log in with the master key). Per-call trace: router `/trace`.

## 5. Daily use

```bash
./clients/ax "your prompt"                 # model: auto (policy-routed)
./clients/ax --model claude "..."          # force a model
echo "some text" | ./clients/ax "summarize"# piped stdin
./clients/ax -s mysession "remember X"     # named session (multi-turn continuity)
```

Routing policy lives in `services/router/routing.yaml` — `auto` picks per prompt; any explicit
model name passes through unchanged.

## 6. Add the other models (optional)

- **Codex:** `make login-codex` (device-auth, headless — follow the printed code/URL).
- **MiniMax:** put a real `MINIMAX_API_KEY` (+ correct `MINIMAX_BASE_URL`) in `.env`, then
  `docker compose --profile poc up -d axonate-litellm` to reload.

Re-run `make smoke` — the corresponding SKIPs flip to PASS.

## 7. Provision users & budgets

```bash
make add-user EMAIL=someone@you.com BUDGET=50    # mints a LiteLLM virtual key with a spend cap
```

In dev `AUTH_MODE` (default) the router maps a dev identity to the master key. Per-user identity
+ budgets are enforced once SSO is on (prod).

## 8. Stop / restart / backup

```bash
make down                 # stop; KEEPS volumes + logins (safe)
make up PROFILE=poc       # back up, fast (no rebuild, no migrations)
make backup               # dump Postgres + config to ./backups
make restore FILE=./backups/<file>.sql.gz
make doctor               # diagnose ADAPTER_TOKEN drift / health issues
```

Never use `down -v` unless you intend to wipe the database and CLI logins.

## 9. Going to production

POC = personal subscriptions, single-user (ToS). Production is a **credential swap, not a
rewrite** — same model names, same clients:

- Real provider API keys + `LITELLM_CONFIG=litellm_config.prod.yaml` — see `SWAP.md`.
- Cloudflare Zero Trust + Google SSO (`AUTH_MODE=cloudflare`) — see `docs/SSO.md`.
- Kubernetes instead of single-host compose — `deploy/helm/axonate/README.md`
  (build/push the router image via `.github/workflows/build-router.yml` on a `v*` tag).
- Pre-go-live checklist — `GOLIVE.md`.
