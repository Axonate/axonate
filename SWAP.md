# SWAP.md — POC → Production

The swap is a **credential + config change, not a rewrite**. Clients, router, SSO, budgets,
logs, and dashboard are untouched. Model names are identical across poc/prod.

## What changes

| Layer | POC | Production |
|---|---|---|
| `claude` backend | CLI adapter (`claude -p`, acctA/acctB) | `anthropic/claude-sonnet-4-6` + `ANTHROPIC_API_KEY` |
| `codex` backend | CLI adapter (`codex exec`) | `openai/gpt-5.1` + `OPENAI_API_KEY` |
| `minimax` backend | subscription key | same model, API/PAYG key |
| `deepseek` (optional) | n/a | `deepseek/deepseek-chat` + `DEEPSEEK_API_KEY` |
| auth | `AUTH_MODE=dev` | `AUTH_MODE=cloudflare` (verify Access JWT) |
| adapter service | running (`poc` profile) | **not started** (`poc`-only), removable |
| config file | `litellm_config.yaml` | `litellm_config.prod.yaml` |

## Checklist

1. **Get real keys:** Anthropic, OpenAI, MiniMax (PAYG), optional DeepSeek.
2. **Fill `.env`** from `config/prod.env.example`:
   - `AUTH_MODE=cloudflare`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`, `CLOUDFLARE_TUNNEL_TOKEN`
   - provider API keys
   - `LITELLM_CONFIG=litellm_config.prod.yaml`
3. **Provision users:** `make add-user EMAIL=alice@you.com BUDGET=25` for each; paste keys into `config/users.yaml`.
4. **Verify model names** match exactly between `litellm_config.yaml` and `litellm_config.prod.yaml`
   (clients reference these — a rename breaks them).
5. **Dry run:** `docker compose --profile prod config` validates; confirm `axonate-adapter` is absent.
6. **Bring up:** `make up PROFILE=prod`.
7. **Smoke:** `make smoke` — all blocking gates pass.
8. **Quarantine/remove adapter:** it never starts under `prod`. Delete `services/adapter/` and its
   compose block + volumes once the swap is confirmed stable.

## Rollback

Set `LITELLM_CONFIG=litellm_config.yaml` + `AUTH_MODE=dev`, `make up PROFILE=poc`. Backends revert
to the adapter/subscription with zero client changes.
