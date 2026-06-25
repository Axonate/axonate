# Lab Deploy — design

**Date:** 2026-06-24
**Status:** approved (brainstorm)
**Track:** Deployment (subscription-backed lab gateway)

## Goal

Make Axonate reachable for a few household ("lab") users from anywhere, running on an always-on
home server behind a Cloudflare tunnel, with per-person Google SSO. Backed by the owner's
Claude/Codex **subscriptions** (the adapter stays — this is private lab use, not a commercial
team, so real API keys are not required).

## Scope

In: a new `lab` compose profile that runs the subscription adapter **and** the cloudflared tunnel
together, plus a `docs/LAB-DEPLOY.md` runbook that ties together server setup, in-container
subscription logins, the Cloudflare tunnel + Access(Google) configuration, and per-person key
provisioning.

Out (separate, next spec): token-expiry detection + alert; scheduled backups; rate-limit/error
polish. Real-API-key prod swap (not needed for lab).

## What already exists (no change)

- Permanent services (`axonate-db`, `axonate-litellm`, `axonate-router`) run in every profile
  (they carry no `profiles:` key).
- Router supports `AUTH_MODE=cloudflare`: verifies the Cloudflare Access JWT against the team
  keys, maps the verified email → virtual key via `config/users.yaml`. Env already present:
  `AUTH_MODE`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`.
- `cloudflared` service exists (currently `profiles: ["prod"]`), runs the tunnel from
  `CLOUDFLARE_TUNNEL_TOKEN`, no open ports.
- `adapter` service exists (currently `profiles: ["poc"]`), bundles the Claude/Codex CLIs; logins
  persist in named volumes.
- `make add-user EMAIL=.. BUDGET=..` provisions a LiteLLM virtual key; `docs/SSO.md` documents the
  full Cloudflare tunnel + Access(Google IdP) walkthrough.

## Gap (the only thing to build in code)

No profile runs the adapter **and** cloudflared together. `poc` = adapter, no tunnel; `prod` =
tunnel, no adapter. The lab needs both.

## Component 1 — `lab` compose profile

In `docker-compose.yml`, add `"lab"` to the `profiles:` list of **both** services:
- `axonate-adapter`: `profiles: ["poc", "lab"]`
- `axonate-cloudflared`: `profiles: ["prod", "lab"]`

Effect: `make up PROFILE=lab` (i.e. `docker compose --profile lab up -d`) starts the permanent
services (always) + the adapter (subscription backend) + cloudflared (tunnel). No new service, no
new env, no behavioral change to existing profiles.

Acceptance: `make config PROFILE=lab` validates; the rendered config for `PROFILE=lab` includes
`axonate-adapter` and `axonate-cloudflared` and the three permanent services; `PROFILE=poc` and
`PROFILE=prod` render unchanged (adapter-only / cloudflared-only respectively).

## Component 2 — `docs/LAB-DEPLOY.md` runbook

A step-by-step for the always-on home server. Sections:

1. **Prereqs** — Docker + git on the server; a Cloudflare account + a domain on Cloudflare; a
   Google account for the IdP; the owner's Claude/Codex subscriptions.
2. **Clone + `.env`** — `cp .env.example .env`; set `LITELLM_CONFIG=litellm_config.yaml` (poc/
   subscription config — same model names), `AUTH_MODE=cloudflare`, and the Cloudflare values
   `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `CLOUDFLARE_TUNNEL_TOKEN` (filled in step 4).
3. **Bring up the stack** — `make up PROFILE=lab`; wait for litellm liveness; `make ps`.
4. **Cloudflare tunnel + Access (Google)** — point to `docs/SSO.md` for the exact dashboard steps:
   create the tunnel (route the public hostname `axonate.<domain>` → `http://axonate-router:4100`),
   create the Access application with the Google IdP, copy the tunnel token + team domain + AUD
   into `.env`, then `make up PROFILE=lab` to apply.
5. **Subscription logins on the server** — run the interactive logins **on the server in a real
   terminal** (not over a non-TTY shell): `docker compose --profile lab exec axonate-adapter claude
   setup-token` → `make set-claude-token TOKEN=.. SLOT=A` (repeat `SLOT=B` for the failover
   account); `make login-codex`. Tokens persist in named volumes — never `docker compose down -v`.
6. **Provision the lab members** — for each person: `make add-user EMAIL=person@domain BUDGET=..`,
   paste the returned key into `config/users.yaml` (in `AUTH_MODE=cloudflare` every authorized
   email MUST be in `users.yaml` or the request is rejected), and add the email to the Access
   policy.
7. **Verify both paths** — unauthenticated request to `axonate.<domain>` is blocked by Access; an
   authenticated member gets a routed answer; the dashboard at `/trace/view` shows per-person rows.
8. **Concurrency note** — keep it a few people / low concurrency (one shared subscription token);
   configure the `acctB` failover slot; this is private lab use.

## Data flow (deployed)

lab member → Cloudflare Access (Google login) → tunnel → `axonate-router` (verify Access JWT →
email → virtual key → route) → `axonate-litellm` (per-user budget, spend log) → `axonate-adapter`
(claude/codex subscription CLIs). No open inbound ports.

## Error handling / edge cases

- Missing `CLOUDFLARE_TUNNEL_TOKEN` / `CF_ACCESS_*` while `AUTH_MODE=cloudflare`: documented as a
  required-before-up checklist in the runbook; the router rejects unverifiable requests rather than
  trusting headers.
- `down -v` destroys the login volumes (re-login) — called out in the runbook.
- Token expiry affects all members — flagged as the next spec (out of scope here).

## Testing

- `make config PROFILE=lab` validates; assert adapter + cloudflared + permanent services present;
  assert `poc`/`prod` renders unchanged.
- Manual/E2E on the server: after the runbook, an authenticated member round-trips a prompt and
  the dashboard shows their row; an unauthenticated request is blocked by Access.

## Decision note

Named the profile `lab` (not `home`) per the owner. The lab path uses the `poc`/subscription
litellm config and keeps the adapter — distinct from the adapter-excluded Helm chart, which
targets a future real-API-key path that this private lab does not need.
