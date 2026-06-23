# Axonate — PROMPTS.md (Claude Code build prompts)

Copy-paste into Claude Code **in order**, one phase at a time.

## How to use
1. Put `PLAN.md`, `GOLIVE.md`, and this file in an empty repo, open it in Claude Code on the mini.
2. Paste one phase prompt. Let Claude Code work, then **review the diff before accepting.**
3. Run the phase's acceptance check. Only then move on.
4. Use plan mode for the bigger phases (4, 5, 8).

Standing rules (prompts restate the important ones):
- Build the **smallest thing that passes the acceptance check**. No speculative features.
- Everything OpenAI-compatible. Secrets via env / Docker secrets, never in code.
- Default to read-only / localhost. Security defaults stay on unless a prompt says otherwise.
- After each phase, stop and print: what changed, how to run it, how to test it.

---

### Prompt 0 — Scaffold (containerized)
```
Read PLAN.md. Do Phase 0 only — scaffold, no features.
Create the repo layout from PLAN.md for a tool called "axonate". Include: a Dockerfile per
service (router, adapter, auth, litellm), a docker-compose.yml with `poc` and `prod`
profiles and an `axonate-` prefix on every service, a commented-out optional `axonate-redis`
service, an `axonate-db` postgres service with a named volume, restart: unless-stopped on
all services, a .env.example with every secret as a placeholder, a Makefile (up/down/logs/help),
and a CLAUDE.md capturing the project, conventions, branding (CLI `axonate`/`ax`), and the
"control plane permanent, adapter disposable" principle.
Acceptance: `make help` lists targets, `docker compose --profile poc config` validates, and
no real secrets are committed. Stop and show me the tree.
```

### Prompt 1 — CLI adapter (disposable)
```
Implement Phase 1: services/adapter/cli_adapter.py — an OpenAI-compatible FastAPI server
wrapping the Claude Code CLI (`claude -p`) and Codex CLI (`codex exec`), plus its Dockerfile.
Requirements:
- POST /v1/chat/completions and GET /v1/models, OpenAI-shaped, stream:true via SSE.
- Backend registry: model name -> CLI. Two Claude logins via separate CLAUDE_CONFIG_DIR
  (mounted as volumes so logins survive rebuilds), plus codex.
- Two profiles per agent: read-only chat (default) and a write-enabled "-code" profile that
  takes a project dir. Read-only is default.
- CLI flags env-overridable (CLAUDE_FLAGS / CODEX_FLAGS). Optional ADAPTER_TOKEN auth,
  subprocess timeout. Bind 127.0.0.1. Only runs under the `poc` compose profile.
Acceptance: with the CLIs logged in, curl each model and get an answer; write profile edits a
file in a test dir, read-only one can't. Stop and show me the curl tests.
```

### Prompt 2 — LiteLLM + Postgres
```
Implement Phase 2: services/litellm/litellm_config.yaml wiring three models behind LiteLLM,
backed by the axonate-db postgres service.
- minimax -> MiniMax subscription key, OpenAI-compatible base URL (direct).
- claude  -> the adapter, TWO entries (acctA primary, acctB failover — prioritized, not round-robin).
- codex   -> the adapter.
Enable virtual keys + master_key from env, drop_params, one retry, postgres for keys/spend.
Wire it into compose on :4000. Acceptance: all three models answer through the proxy with the
master key. Stop and show me a curl for each.
```

### Prompt 3 — Cost/quota-aware router
```
Implement Phase 3: services/router/{router.py,routing.yaml} — a proxy in FRONT of LiteLLM
(:4100) handling model "auto" and passing everything else through unchanged.
- routing.yaml holds the policy (no hardcoded opinions): length rule, keyword rules
  (code->codex, bulk/transform->minimax, deep reasoning->claude), optional classifier (off),
  default. Add a cost/quota signal: prefer cheap/abundant backends for easy tasks and avoid a
  backend near its limit; keep it tunable in the yaml.
- Log every decision; return X-Router-Route / X-Router-Reason; pass streaming through.
Acceptance: `auto` sends a coding prompt to codex, a summarize prompt to minimax, a reasoning
prompt to claude, logging the reason each time. Stop and show me the three log lines.
```

### Prompt 4 — SSO + identity (plan mode)
```
Implement Phase 4: SSO + per-user identity. Plan first.
- Document the Cloudflare setup: a cloudflared tunnel (axonate-cloudflared service) to the
  router, and a Cloudflare Access app using Google as IdP. No open ports.
- services/auth/auth_shim.py: VERIFY the Cloudflare Access JWT against the team's public keys
  (do not trust headers blindly), extract the user email, map it to that user's LiteLLM
  virtual key, reject unverified requests.
- Wire per-user virtual keys + budgets in LiteLLM.
Acceptance: a request without a valid Access JWT is rejected; a valid one is attributed to the
right user and uses their key/budget. Stop and show me how to add a user + test both paths.
```

### Prompt 5 — Logging + dashboard + trace
```
Implement Phase 5: per-user logging, a trace view, and a usage dashboard.
- One trace record per call in postgres: timestamp, user email, model requested, route+reason,
  latency, tokens/usage, cost, status. Prompt-content logging is OPT-IN only (privacy default off).
- Stand up LiteLLM's built-in usage UI first. Add a minimal read-only per-user dashboard page
  only if the UI is missing something.
Acceptance: per-user usage is visible and matches the trace for a few test calls. Stop and show
me the dashboard URL and a sample trace row.
```

### Prompt 6 — Thin CLI (`ax`)
```
Implement Phase 6: clients/ax — a small CLI for the laptop that POSTs to the gateway.
Support: `ax "prompt"` (model auto), `ax -m claude "prompt"`, piped stdin
(`git diff | ax "review this"`), and streaming output. Config (gateway URL + key) via env or a
dotfile. Acceptance: all usages work end to end against the running stack. Stop and show me examples.
```

### Prompt 7 — Production swap prep
```
Implement Phase 7: make the POC->production swap trivial and safe.
- config/poc.env + config/prod.env and a selector.
- prod model_list entries using real APIs (Anthropic, OpenAI, etc.), mirroring the POC model
  names EXACTLY so clients don't change.
- Quarantine the adapter to the `poc` profile so `prod` never starts it and it can be deleted.
- Write SWAP.md: the exact subscriptions->APIs checklist.
Acceptance: switching the profile changes backends with zero client-side changes. Stop and show
me the swap checklist.
```

### Prompt 8 — Harden to GOLIVE (plan mode)
```
Implement Phase 8: bring axonate to the GOLIVE.md bar. Plan first.
- Add the go-live UX layer: streaming end-to-end, conversation/session continuity (thread->session),
  a provider fallback chain, and human-readable errors (budget, rate limit, provider down).
- Then walk GOLIVE.md and satisfy every blocking item: hard global spend ceiling + per-user
  budgets, health endpoints, graceful failure, config + DB backup with a tested restore,
  per-user rate limiting, and the pre-launch smoke gate.
Acceptance: every blocking item in GOLIVE.md is checked and the smoke gate passes. Stop and show
me the completed checklist.
```

---

### Tips
- If a phase gets big, ask Claude Code to make a todo list and do one item at a time.
- The adapter (Phase 1) is the only throwaway piece — keep it isolated.
- Re-run earlier acceptance checks after later phases to catch regressions.
