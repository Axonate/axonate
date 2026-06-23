# SSO setup — Cloudflare Zero Trust + Google IdP (Phase 4)

Goal: no open inbound ports; users hit `https://axonate.<yourdomain>`, Cloudflare Access
forces Google login, and the origin **verifies** the signed Access JWT before serving.

```
user -> Cloudflare Access (Google SSO) -> Cloudflare Tunnel -> axonate-router
                                          (cf-access-jwt-assertion header, signed)
        axonate-router: auth_shim VERIFIES the JWT vs team JWKS -> email -> virtual key
```

## What the code already does

`services/auth/auth_shim.py` (imported by the router), with `AUTH_MODE=cloudflare`:
- reads the `Cf-Access-Jwt-Assertion` header (never trusts a plain email header),
- verifies signature against `https://<team>/cdn-cgi/access/certs`, plus `aud` + `exp`,
- extracts `email`, maps it to that user's LiteLLM virtual key from `config/users.yaml`,
- rejects anything unverified/unmapped (401). Tested: missing + tampered JWT both rejected.

You only need to do the Cloudflare-side config below and fill 3 env vars.

## 1. Cloudflare prerequisites
- Domain on Cloudflare (free plan fine). Zero Trust enabled (Team domain = `yourteam.cloudflareaccess.com`).
- Zero Trust > Settings > Authentication > add **Google** as a login method (Google Cloud OAuth
  client: authorized redirect URI `https://yourteam.cloudflareaccess.com/cdn-cgi/access/callback`).

## 2. Create the tunnel (no open ports)
Zero Trust > Networks > Tunnels > Create tunnel (`cloudflared`), name `axonate`.
- Copy the **tunnel token** -> `.env` `CLOUDFLARE_TUNNEL_TOKEN=...`.
- Public hostname: `axonate.<yourdomain>` -> service `http://axonate-router:4100`
  (the tunnel runs in the compose network, so it reaches the router by service name).
- The `axonate-cloudflared` service (prod profile) runs the tunnel from that token.

## 3. Create the Access application
Zero Trust > Access > Applications > Add > Self-hosted:
- Application domain: `axonate.<yourdomain>`.
- Identity providers: Google only.
- Policy: Allow, rule e.g. emails ending `@yourdomain.com` (or an explicit email list).
- After saving, open the app > **Overview** > copy the **Application Audience (AUD) tag**
  -> `.env` `CF_ACCESS_AUD=...`. Also set `CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com`.

## 4. Provision users (email -> virtual key + budget)
For each allowed person:
```bash
make add-user EMAIL=alice@yourdomain.com BUDGET=25     # 25 USD / 30d enforced
```
Paste the printed key into `config/users.yaml` under `users:`. In cloudflare mode an email with
no key is rejected.

## 5. Turn it on
```bash
# .env: AUTH_MODE=cloudflare, CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD, CLOUDFLARE_TUNNEL_TOKEN set
make up PROFILE=prod
```

## 6. Test both paths
- **Unauthenticated blocked:** `curl https://axonate.<yourdomain>/v1/chat/completions ...`
  with no Access cookie/JWT -> Cloudflare blocks at the edge; direct-to-origin (if ever exposed)
  -> router 401. `make smoke` asserts the 401 in cloudflare mode.
- **Authenticated attributed:** open `https://axonate.<yourdomain>` in a browser, log in with
  Google, then use `ax` (cookie/JWT forwarded) -> request attributed to your email, metered to
  your key/budget. Check the trace: `curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  https://axonate.<yourdomain>/trace | jq` (admin) shows the row with your email.

## Notes
- JWKS is cached 1h in the router; key rotation is automatic on next fetch.
- For service/CLI access without a browser, use a Cloudflare Access **service token** and send
  `CF-Access-Client-Id` / `CF-Access-Client-Secret`; Access still issues a JWT the router verifies.
- Local dev stays on `AUTH_MODE=dev` (no Cloudflare needed).
