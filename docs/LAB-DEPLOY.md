# Lab deploy — Axonate on a home server behind Cloudflare

Step-by-step to run Axonate for a few **lab** (household) users: an always-on home server, a
Cloudflare tunnel (no open ports), per-person Google SSO, backed by the owner's Claude/Codex
**subscriptions** (the adapter stays — private lab use, not a commercial team).

The `lab` compose profile runs the permanent services **plus** the subscription adapter **plus**
the cloudflared tunnel together (`poc` has no tunnel; `prod` has no adapter).

## 0. Prerequisites

- An always-on server (mini PC / NAS / Linux box) with Docker + git.
- A Cloudflare account and a domain managed in Cloudflare.
- A Google account for the IdP (Cloudflare Access login).
- The owner's Claude and Codex subscriptions (for the in-container logins).

## 1. Clone + base config

```bash
git clone git@github.com:Axonate/axonate.git && cd axonate
cp .env.example .env
echo "LITELLM_CONFIG=litellm_config.yaml" >> .env   # subscription config (same model names as prod)
```

Edit `.env`:
- Set `AUTH_MODE=cloudflare`.
- Leave `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `CLOUDFLARE_TUNNEL_TOKEN` blank for now — filled
  in step 3.
- The infra secrets (`LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `POSTGRES_PASSWORD`,
  `ADAPTER_TOKEN`) must be non-empty and self-consistent; change them from the examples for a real
  deployment. Never paste a model/OAuth token into `ADAPTER_TOKEN` (it is the litellm↔adapter
  shared secret).

## 2. Bring the stack up

```bash
make up PROFILE=lab
make ps
# wait for litellm liveness (first boot runs DB migrations):
curl -fsS http://127.0.0.1:4000/health/liveliness && echo " litellm ok"
curl -fsS http://127.0.0.1:4100/health && echo " router ok"
```

`PROFILE=lab` starts: `axonate-db`, `axonate-litellm`, `axonate-router`, `axonate-adapter`,
`axonate-cloudflared`.

## 3. Cloudflare tunnel + Access (Google SSO)

Follow `docs/SSO.md` for the exact dashboard steps. Summary:
1. **Tunnel** — create a Cloudflare Tunnel; add a public hostname `axonate.<yourdomain>` routing to
   `http://axonate-router:4100` (the router runs inside the compose network as that hostname).
   Copy the tunnel token.
2. **Access application** — create a self-hosted Access app for `axonate.<yourdomain>`, add Google
   as the identity provider, and a policy allowing your lab members' emails. Copy the **team
   domain** and the application **AUD**.
3. Put all three into `.env`:
   ```
   CLOUDFLARE_TUNNEL_TOKEN=...
   CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
   CF_ACCESS_AUD=<application-aud-tag>
   ```
4. Apply: `make up PROFILE=lab` (recreates cloudflared + router with the new values).

The router verifies the Access JWT against Cloudflare's team keys — it does **not** trust headers,
so the tunnel is the only path in and every request is authenticated.

## 4. Subscription logins (on the server, one-time)

Run the interactive logins **on the server in a real terminal** (not over a non-TTY shell, or they
hang). Tokens persist in named volumes and survive restarts.

```bash
# Claude (primary account):
docker compose --profile lab exec axonate-adapter claude setup-token
# -> open the URL, authorize, paste the code; copy the sk-ant-oat... token, then:
make set-claude-token TOKEN=sk-ant-oat... SLOT=A
# Optional failover account (recommended for a shared token):
#   ...repeat setup-token for the second account, then: make set-claude-token TOKEN=... SLOT=B

# Codex (device-auth):
make login-codex
# -> open the URL, sign in, enter the device code; it saves to the codex volume.
```

**Never run `docker compose down -v`** — it destroys these login volumes and forces re-login.

## 5. Provision the lab members

In `AUTH_MODE=cloudflare`, **every** authorized email must be in `config/users.yaml` or its
requests are rejected. For each member:

```bash
make add-user EMAIL=alice@yourdomain.com BUDGET=50
# paste the returned key into config/users.yaml under `users:`  ->  alice@yourdomain.com: sk-...
```

Also add each email to the Cloudflare Access policy (step 3.2). Restart the router to pick up
`users.yaml` changes: `make up PROFILE=lab`.

## 6. Verify both paths

- **Unauthenticated:** open `https://axonate.<yourdomain>` in a private window with no Cloudflare
  session → blocked by Access (Google login prompt). Good.
- **Authenticated:** log in as a member, then point a client at the gateway and send a prompt; you
  get a routed answer. The dashboard at `https://axonate.<yourdomain>/trace/view` shows the call
  attributed to that member's email.

## 7. Operating notes

- **Concurrency:** one shared subscription token serves the lab — keep it a few people / low
  concurrency, and configure the `acctB` failover slot. Private lab use only.
- **Token expiry:** subscription OAuth tokens expire (not on restart — on timeout). When a model
  starts failing auth, re-run the step-4 login on the server. (An automatic expiry alert is a
  planned follow-up.)
- **Restarts:** `make down` then `make up PROFILE=lab` is safe (keeps volumes/logins). Only `-v`
  is destructive.
- **Backups:** `make backup` dumps Postgres (keys/spend/trace) to `./backups`; `make restore
  FILE=..` restores. Schedule it on the server (cron/systemd) for real use.
- **Diagnostics:** `make doctor` checks `ADAPTER_TOKEN` drift and health.
