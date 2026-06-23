# Axonate — GOLIVE.md (Go-Live Checklist)

Launch rule: **every blocking item must be checked.** Don't go live with any unchecked.
Legend: **[B]** blocking · **[R]** recommended for first launch · **[D]** deferred (don't let it delay launch).

---

## 0. Decisions (resolved — change here if needed)
- **Audience:** small trusted group (NOT open signup). *If this changes to open signup,
  promote abuse-limits and real RBAC from [D] to [B].*
- **Cost-control model:** hard global ceiling + enforced per-user budgets.

## 1. Security & access  [B]
- [ ] Production runs on real provider API keys — no shared personal subscriptions
- [ ] SSO enforced, deny-by-default (Cloudflare Access app live, Google IdP)
- [ ] Origin **verifies** the Cloudflare Access JWT (signature vs team keys) — headers not trusted
- [ ] No open inbound ports; only the `axonate-cloudflared` tunnel reaches the gateway
- [ ] Secrets in a real store / Docker secrets — not a committed `.env`
- [ ] Write-enabled agent path **off by default**, sandboxed when enabled
- [ ] Containers on an internal network; nothing public except via the tunnel
- [ ] Base images pinned; image scan reviewed

## 2. Cost control  [B]
- [ ] Hard global spend ceiling that actually blocks at the limit
- [ ] Per-user budgets configured
- [ ] Over-budget returns a clear error, never a hang
- [ ] Spend-spike alert + daily spend digest reaching you

## 3. Identity, logging & audit  [B]
- [ ] Every request attributed to an authenticated user
- [ ] Trace record per call: user, model, route+reason, latency, tokens, cost, status, time
- [ ] Prompt-content logging is **opt-in only** (privacy default off)
- [ ] Per-user usage visible (LiteLLM UI at minimum)

## 4. Reliability & ops
- [ ] [B] Health/status endpoint per service
- [ ] [B] `restart: unless-stopped` on all containers
- [ ] [B] Graceful failure when a provider is down or a user is over budget
- [ ] [B] Config + Postgres (keys/spend/trace) backed up, **restore tested once**
- [ ] [R] Versioned, reproducible config with a rollback path
- [ ] [R] Per-user rate limiting
- [ ] [R] One-command `up` / `down` / `logs`

## 5. UX quality bar (the "feel good" layer)  [R]
- [ ] Streaming works end to end
- [ ] Conversation / session continuity (thread -> session)
- [ ] Fallback chain when the preferred backend is busy or down
- [ ] Human-readable errors (budget, rate limit, provider down)
- [ ] Trace view — one table powering audit + cost + debugging

## 6. Production swap readiness  [B]
- [ ] `poc` / `prod` compose profiles; `prod` uses real APIs
- [ ] Model names identical across poc/prod (clients unchanged at swap)
- [ ] CLI adapter quarantined to the `poc` profile and removable
- [ ] `SWAP.md` exists and the swap was dry-run at least once

## 7. Pre-launch smoke gate  [B]
- [ ] Unauthenticated request is blocked
- [ ] Authenticated request is attributed and metered to the right user
- [ ] Each model answers; `model: auto` routes sensibly
- [ ] Over-budget user is blocked with a clear message
- [ ] Kill a backend -> graceful failure / fallback (no hang)
- [ ] Restart the whole stack -> comes back healthy on its own
- [ ] Force a spend spike -> alert actually fires

## 8. Deferred — NOT blocking  [D]
Polished custom dashboard · Redis / response caching · multiple replicas · RBAC beyond
allow/deny · prompt/template library · playground · bring-your-own-key per user · multi-region.
Each has a documented seam to enable later; none gate launch.

---

### Minimum to launch
All **[B]** items + the **[R]** ops basics (rate limiting, reproducible config) + the **[R]**
UX five. Everything in section 8 is consciously parked.
