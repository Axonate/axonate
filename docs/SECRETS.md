# Secrets handling (GOLIVE §1 [B])

## Current state (POC)
- All secrets live in `.env`, which is **gitignored** (`.gitignore` covers `.env`, `config/poc.env`,
  `config/prod.env`, `*.pem`, `*.key`). Nothing secret is committed — the "no committed `.env`"
  bar is met.
- Generated keys (master, salt, adapter token) are random per host (`make`/`openssl`).
- Claude OAuth tokens + provider keys are entered locally, never in git.

## Production hardening (do at the swap)
For real multi-user production, move off plain `.env` to one of:

### Option A — Docker secrets (compose overlay)
`docker-compose.secrets.yml` (run with `-f docker-compose.yml -f docker-compose.secrets.yml`):
```yaml
services:
  axonate-litellm:
    secrets: [litellm_master_key, slack_webhook_url]
    environment:
      LITELLM_MASTER_KEY_FILE: /run/secrets/litellm_master_key   # entrypoint exports *_FILE -> var
secrets:
  litellm_master_key:
    file: ./secrets/litellm_master_key
  slack_webhook_url:
    file: ./secrets/slack_webhook_url
```
LiteLLM/router read env vars, so add a tiny entrypoint that does
`export LITELLM_MASTER_KEY="$(cat $LITELLM_MASTER_KEY_FILE)"` before launch. Keep `./secrets/`
out of git (already covered by `*.key`/add `secrets/`).

### Option B — external secret store (recommended for prod)
Pull from a manager at deploy time and inject as env: Cloudflare/1Password/Vault/AWS Secrets
Manager. The compose stays unchanged; only how `.env` is produced changes (CI/deploy renders it
from the store, never writes it to disk in git).

## Rotation
- Adapter token: regenerate, update `.env`, recreate `axonate-litellm` + `axonate-adapter` together
  (they must share it — see the incident note in CLAUDE.md).
- Master key: rotate, recreate litellm + re-issue virtual keys if needed.
- Claude OAuth: re-run `make login-claude` + `make set-claude-token` (tokens expire).
