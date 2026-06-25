# Public surfaces + self-service portal — design

**Date:** 2026-06-24
**Status:** approved (brainstorm)
**Track:** Deployment + router (lab gateway, public access)

## Goal

Expose the lab gateway through three clean, secure public surfaces on `clouddrove.in` (isolated
from the company prod domain `clouddrove.com`), so household ("lab") users self-onboard with no key
handouts and no hosted chat:

- **`api.clouddrove.in`** — public OpenAI-compatible API for tools (`ax`, VS Code, laptop chat
  apps). Edge-gated, per-user key auth. No SSO (a CLI can't do interactive login).
- **`app.clouddrove.in`** — Google-gated web portal. A user logs in, self-serves their API key +
  copy-paste setup, and sees their usage. Admins see everyone + manage keys.
- **`admin.clouddrove.in`** — the full LiteLLM admin UI, double-gated (Cloudflare Access admin-only
  + LiteLLM's own login). Deep key/budget/model/log management without SSH.

No hosted chat UI (users run their own client). No real API-key swap (subscription adapter stays —
private lab use). One Cloudflare tunnel (existing `7778072b`), backend on eva.

## Surfaces & routing

Three public hostnames route through the existing tunnel. `api.*` and `app.*` go to
`axonate-router:4100` (the router behaves differently per surface, keyed on the inbound `Host`
header); `admin.*` goes straight to `axonate-litellm:4000`.

| Host | Backend | Edge security | App auth |
|---|---|---|---|
| `api.clouddrove.in` | router `/v1/*` only | WAF + rate-limit + **shared CF Access service-token** | per-user `sk-` key (Bearer) → forwarded to LiteLLM |
| `app.clouddrove.in` | router portal + `/trace*` | **CF Access (Google SSO)** | Access JWT (verified) → email |
| `admin.clouddrove.in` | LiteLLM admin UI (`:4000/ui`) | **CF Access (Google SSO), admin emails only** | LiteLLM's own login (admin + master key) |

On `api.*`, any path other than `/v1/*` returns 403. On `app.*`, the portal + usage views are
served and the Access JWT is verified (router already supports `AUTH_MODE=cloudflare`).
`admin.*` exposes the full LiteLLM admin UI (keys, budgets, models, raw logs) — **double-gated**:
Cloudflare Access (Google, admin-only) at the edge, then LiteLLM's own admin login. It bypasses the
router entirely (tunnel → `axonate-litellm:4000`). LiteLLM's port stays unpublished to the host;
only the tunnel reaches it.

## Security model (defense in depth on `api.*`)

1. **Edge (Cloudflare, before eva):** automatic DDoS; WAF managed rules; per-IP rate limiting;
   a **shared Access service-token** policy on `api.clouddrove.in` — requests must carry
   `CF-Access-Client-Id`/`CF-Access-Client-Secret` or they are blocked at the edge and never reach
   the tunnel. Removing a user fully = rotate the shared service token (rare; acceptable for a
   household). Upgrade path: per-user service tokens minted via the CF API (future, not now).
2. **Key (LiteLLM virtual key):** every `/v1` request needs a valid `sk-` Bearer; LiteLLM hashes +
   looks it up (stored hashed in Postgres, shown once at mint), checks expiry/budget/model, meters
   spend, else 401. Revoke = delete row (instant). Rotate = mint new + delete old.
3. **Blast radius:** per-user budget caps; hard global ceiling; per-key rpm/tpm; spend-spike alert
   to Slack; no prompt-content logging (metadata only).

## Component 1 — router: surface-aware auth & path gating

The router currently resolves identity (dev/cloudflare) → maps to a virtual key → forwards. Add
surface awareness:

- **`api.*` (key-passthrough):** accept the caller's own `sk-` Bearer and forward it unchanged to
  LiteLLM (LiteLLM validates + meters per key). Do **not** remap to the master key. Only `/v1/*`
  is served; other paths → 403. `model: auto` routing still applies.
- **`app.*` (Access):** require and verify the Cloudflare Access JWT (existing cloudflare mode);
  resolve the email; serve the portal + usage.

Surface is determined by the `Host` header (configurable via env: `API_HOST=api.clouddrove.in`,
`APP_HOST=app.clouddrove.in`). A request to the wrong path/host for its surface is rejected.

## Component 2 — self-service portal (`app.*`, behind Access)

A small set of router endpoints + one HTML page, all requiring a verified Access email.

- **`GET /` (portal page):** role-aware.
  - Shows the user's **base URL** (`https://api.clouddrove.in/v1`), the **shared service-token**
    (id + secret, from config), and ready-to-paste setup snippets for `ax`, VS Code (Continue/
    Cline), and a generic OpenAI-compatible chat app.
  - **Key state:** queries LiteLLM by `user_id=<email>`. If no key → a **“Generate my key”**
    button; if a key exists → a **“Rotate key”** button (the old `sk-` can't be re-shown — only
    shown once at mint). After generate/rotate, the new `sk-` is shown once with a copy button.
  - **Usage:** the existing trace view, scoped — a normal user sees only their own rows; an admin
    (email in an `ADMIN_EMAILS` list) sees all + a key-management table.
- **`POST /portal/key` (generate/rotate):** mints a LiteLLM key via the master key
  (`/key/generate` with `user_id=<email>`, `max_budget=<default>`), revoking the prior key for that
  email on rotate. Returns the new key once. Verified-email-scoped (a user can only mint/rotate
  their own).
- Admin-only: **`GET /portal/admin`** — list users, keys (masked), budgets, spend; revoke.

The portal reuses the existing `/trace/stats` + `/trace` data for the usage section; no new trace
schema.

## Component 3 — Cloudflare config + onboarding (documented, user-side)

`docs/SURFACES.md` (or extend `docs/LAB-DEPLOY.md`): create the three public hostnames on the
tunnel (`api`/`app` → `axonate-router:4100`, `admin` → `axonate-litellm:4000`); create an **Access
application (Google IdP)** on `app.*` (policy: chosen `@clouddrove.com` emails) and another on
`admin.*` (policy: admin emails only); create an **Access service-token** + service-token policy on
`api.*`; set `.env` (`AUTH_MODE=cloudflare`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `API_HOST`,
`APP_HOST`, `ADMIN_EMAILS`, the service-token id/secret for display in the portal). Onboarding a
person = **add their email to the Access allow-list**; they then open `app.clouddrove.in`,
Google-login, and self-serve their key + setup. LiteLLM's `:4000` stays unpublished on the host —
reachable only via the `admin.*` tunnel route.

## Data flow

```
tool → api.clouddrove.in → CF (WAF, rate-limit, service-token check) → tunnel → router (/v1,
       key-passthrough) → litellm (validate+meter user key) → adapter (claude/codex) → answer

user → app.clouddrove.in → CF Access (Google login) → tunnel → router (verify Access JWT → email)
     → portal (mint/fetch key, setup snippets, own/all usage)

admin → admin.clouddrove.in → CF Access (Google, admin-only) → tunnel → litellm :4000/ui
      → LiteLLM admin login (master key) → full key/budget/model/log management
```

## Error handling / edge cases

- `api.*` request with no/invalid service-token → blocked at edge (never reaches eva).
- `api.*` valid service-token but bad/missing `sk-` key → 401 from LiteLLM.
- `api.*` non-`/v1` path → 403.
- `app.*` without a valid Access JWT → blocked by Access (and router rejects unverifiable JWT).
- Over budget / rate limit → clear error, never a hang.
- Portal generate/rotate scoped to the verified email — a user cannot mint another user's key;
  non-admin cannot reach `/portal/admin`.

## Testing

- Unit (no Docker): surface resolver (Host → api/app), path-gate (api allows only `/v1/*`),
  admin-vs-user role from `ADMIN_EMAILS`, setup-snippet rendering for a given email/key.
- Live E2E on eva: a generated key works via `https://api.clouddrove.in/v1` with the service-token
  headers; the portal at `app.clouddrove.in` mints/rotates a key and shows own usage; an admin sees
  all; an unauthenticated `app.*` request is blocked; an `api.*` request without the service-token
  is blocked at the edge; `admin.clouddrove.in` reaches the LiteLLM UI only after Cloudflare Access
  (admin email) + LiteLLM login, and a non-admin email is denied by the Access policy.

## Non-goals (separate / future)

- Hosted chat UI (users run their own client).
- Real provider API-key swap (subscription adapter stays for lab).
- Per-user CF service tokens (shared token now; per-user via CF API later).
- Token-expiry alert for the subscription tokens (separate spec).

## Decision notes

- Domain `clouddrove.in` (isolated from company prod `clouddrove.com`); login still restricted to
  `@clouddrove.com` via the Access policy — host and login domain are independent.
- Three surfaces (`api` public+key+edge-token, `app` Google portal, `admin` Access-gated LiteLLM
  UI) chosen over a hosted chat UI and over per-user service tokens, to minimize hosting + admin
  overhead for a small private lab. `admin.*` exposes the powerful LiteLLM UI but is double-gated
  (Cloudflare Access admin-only + LiteLLM's own login); the alternative (SSH port-forward to
  `:4000`) was rejected for convenience.
