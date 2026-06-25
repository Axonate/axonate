# Public surfaces — api / app / admin on clouddrove.in

Three Cloudflare hostnames front the lab gateway on eva (one tunnel). Host the gateway on
`clouddrove.in` (isolated from the company prod domain); restrict login to your chosen
`@clouddrove.com` accounts.

| Hostname | Tunnel service | Edge auth | Who |
|---|---|---|---|
| `api.clouddrove.in` | `http://axonate-router:4100` | WAF + rate-limit + **service-token** | tools (ax / VS Code / chat apps) |
| `app.clouddrove.in` | `http://axonate-router:4100` | **Access — Google SSO** | every allowed user (self-serve key + usage) |
| `admin.clouddrove.in` | `http://axonate-litellm:4000` | **Access — Google SSO, admins only** | admins (full LiteLLM UI) |

## 1. Tunnel public hostnames

In Zero Trust → Networks → Tunnels → your tunnel → **Public Hostname**, add three:
- `api.clouddrove.in` → HTTP → `axonate-router:4100`
- `app.clouddrove.in` → HTTP → `axonate-router:4100`
- `admin.clouddrove.in` → HTTP → `axonate-litellm:4000`

Each save auto-creates the DNS record.

## 2. Access applications (Google IdP)

- **app**: self-hosted Access app for `app.clouddrove.in`, identity provider Google, policy =
  allow the chosen `@clouddrove.com` emails. Copy the app **AUD** → `.env CF_ACCESS_AUD`.
- **admin**: self-hosted Access app for `admin.clouddrove.in`, Google, policy = admin emails only.
- Team domain (Zero Trust → Settings) → `.env CF_ACCESS_TEAM_DOMAIN`.
- `.env`: `AUTH_MODE=cloudflare`. (The router verifies the Access JWT on `app.*`.)

## 3. Service token for the API

Zero Trust → Access → **Service Auth → Create Service Token**. On `api.clouddrove.in`'s Access app,
add a policy of type **Service Auth** allowing that token (alongside/instead of the human policy).
Put the **Client ID** in `.env SERVICE_TOKEN_ID` (shown in the portal); distribute the **Client
Secret** to users out-of-band. Tools send both as `CF-Access-Client-Id` / `CF-Access-Client-Secret`
headers.

## 4. Router env (`.env`)

```
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
CF_ACCESS_AUD=<app-aud>
API_HOST=api.clouddrove.in
APP_HOST=app.clouddrove.in
ADMIN_EMAILS=you@clouddrove.com
PORTAL_DEFAULT_BUDGET=50
SERVICE_TOKEN_ID=<service-token-client-id>
```

Apply: `make up PROFILE=lab`.

## 5. Onboard a person

1. Add their Google email to the `app.clouddrove.in` Access policy (and `ADMIN_EMAILS` if admin).
2. They open `https://app.clouddrove.in`, Google-login, click **Generate my key**, copy it + the
   shown setup snippet. For API/tool use they also need the shared service-token secret (from you).
3. Done — no key handouts; revoke by removing the email + deleting their key in `admin.*`.

## 6. Verify

- `https://api.clouddrove.in/v1/models` **without** the service-token headers → blocked at the edge.
  With service-token headers + a valid `sk-` key → returns models.
- `https://app.clouddrove.in` without Google login → Access prompt. After login → portal; Generate
  → a working key; usage shows your own rows.
- `https://admin.clouddrove.in` → Access (admin email) → LiteLLM login → admin UI. Non-admin email
  is denied by the Access policy.
