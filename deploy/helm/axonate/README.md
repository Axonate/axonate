# Axonate Helm chart

Deploys Axonate's **permanent** control-plane services — litellm, router, cloudflared,
and (optionally) in-cluster Postgres. The disposable POC CLI adapter is intentionally
**not** part of this chart.

## Prerequisites

- Build and push the router image from the repo root:
  `docker build -f services/router/Dockerfile -t <registry>/axonate-router:v1.0 .` then push.
- A Postgres database: either external managed (default) or in-cluster (`postgresql.enabled=true`).

## Install

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm dependency build deploy/helm/axonate

helm install axonate deploy/helm/axonate \
  --set image.router.repository=<registry>/axonate-router \
  --set secret.masterKey=sk-... --set secret.saltKey=... \
  --set secret.postgres.host=<rds-endpoint> \
  --set secret.postgres.user=axonate --set secret.postgres.password=... \
  --set secret.postgres.database=axonate \
  --set secret.tunnelToken=<cloudflare-tunnel-token> \
  --set router.cfAccessTeamDomain=<team>.cloudflareaccess.com \
  --set router.cfAccessAud=<aud>
```

## Key values

| Value | Default | Purpose |
|---|---|---|
| `image.router.repository` | "" (required) | Your pushed router image |
| `litellm.configMode` | `prod` | `poc` or `prod` litellm config (from `files/`) |
| `postgresql.enabled` | `false` | In-cluster Bitnami PG vs external managed |
| `cloudflared.enabled` | `true` | Tunnel ingress (no open ports) |
| `ingress.enabled` | `false` | Standard Ingress + cert-manager to the router |
| `secret.existingSecret` | "" | Reference a Secret you created out-of-band |
| `backup.enabled` | `false` | pg_dump CronJob (in-cluster PG only) |

## Secrets

By default the chart creates one k8s Secret from `secret.*` values. For production,
prefer creating the Secret out-of-band (CI / a secret manager) and setting
`secret.existingSecret=<name>` with the same keys
(`LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `DATABASE_URL`, `POSTGRES_*`,
`CLOUDFLARE_TUNNEL_TOKEN`, provider keys).

## External vs in-cluster Postgres

- **External (default):** set `secret.postgres.{host,user,password,database}`; the chart
  builds litellm's `DATABASE_URL` and the router's `POSTGRES_*` from those.
- **In-cluster:** `--set postgresql.enabled=true` plus matching `postgresql.auth.*` and
  `secret.postgres.*`. The Bitnami subchart provisions PG with a PVC. Enable `backup.enabled`
  for the pg_dump CronJob (swap the `emptyDir` for a PVC for durable backups).

## Backup CronJob

Only rendered with in-cluster PG. Mirrors `scripts/backup.sh`. The default `emptyDir`
volume is ephemeral — mount a PersistentVolumeClaim for real backups.

## Not built yet (future)

HorizontalPodAutoscaler, PodDisruptionBudget, NetworkPolicy, External Secrets Operator /
sealed-secrets, and multi-environment overlays are intentionally out of scope for this
chart version. Secrets are plain k8s Secrets (or `existingSecret`) until then.
