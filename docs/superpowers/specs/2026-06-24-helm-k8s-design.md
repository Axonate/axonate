# Helm chart / Kubernetes deploy — design

**Date:** 2026-06-24
**Status:** approved (brainstorm)
**Track:** production (prod swap)

## Goal

A prod-grade Helm chart that deploys Axonate's **permanent** services to a Kubernetes cluster,
as an alternative prod target to single-host docker-compose. Preserves the governing principle:
POC → prod is a credential swap, not a rewrite — same OpenAI-compatible boundaries, same model
names, same router/litellm/auth code. Only the deployment substrate changes.

## Scope

**In:** litellm, router, cloudflared, Postgres (external managed by default, in-cluster
optional). Plus ConfigMaps (routing.yaml + litellm config), a Secret, Services, optional Ingress,
optional backup CronJob.

**Out:** the CLI adapter. It is POC-only and disposable (CLAUDE.md), single-user by ToS, and its
OAuth named volumes do not fit k8s. The chart never deploys it.

## Decisions

| Area | Decision |
|---|---|
| Maturity | Prod-grade chart; advanced ops (HPA/PDB/NetworkPolicy/ESO) deferred — see Non-goals |
| Postgres | External managed (RDS/Cloud SQL) via `DATABASE_URL` is the **default**; in-cluster Bitnami `postgresql` subchart is **optional** (`postgresql.enabled`) for dev/self-host |
| External access | cloudflared Deployment (tunnel) is default — no open ports, Access-JWT model preserved; standard Ingress + cert-manager available behind a flag; both values-toggled |
| Secrets | Plain k8s Secret created from values, **or** reference an `existingSecret` made out-of-band. No ESO/sealed-secrets dependency |

## Chart layout — `deploy/helm/axonate/`

```
Chart.yaml          # app chart; dependency: bitnami/postgresql (condition: postgresql.enabled)
values.yaml         # all toggles + image pins + resources
templates/
  _helpers.tpl      # name/label/selector helpers
  serviceaccount.yaml
  configmap.yaml    # routing.yaml + the litellm_config (poc|prod) selected by values
  secret.yaml       # plain Secret from values, skipped if existingSecret set
  litellm-deployment.yaml
  litellm-service.yaml      # ClusterIP
  router-deployment.yaml
  router-service.yaml       # ClusterIP
  cloudflared-deployment.yaml   # rendered when cloudflared.enabled; tunnel token from Secret; no Service
  ingress.yaml      # rendered when ingress.enabled; cert-manager annotations
  backup-cronjob.yaml       # rendered only when postgresql.enabled (in-cluster PG)
  NOTES.txt         # post-install: how to reach the UI, check health, next steps
README.md           # values reference + external-PG / secret / access setup
```

## values.yaml (shape)

```yaml
image:
  litellm:   { repository: ghcr.io/berriai/litellm, tag: main-stable, digest: "sha256:..." }
  router:    { repository: <your-registry>/axonate-router, tag: v1.0 }
  cloudflared: { repository: cloudflare/cloudflared, tag: latest }

litellm:
  configMode: prod        # poc|prod -> selects which config the ConfigMap carries
  replicas: 1
  resources: {}
router:
  replicas: 2             # router is stateless -> safe to scale
  resources: {}

postgresql:
  enabled: false          # default: external managed PG
  # when true, Bitnami subchart values pass through here
externalDatabase:
  databaseUrl: ""         # used when postgresql.enabled=false (or via existingSecret)

cloudflared:
  enabled: true
  tunnelToken: ""         # from Secret
ingress:
  enabled: false
  className: nginx
  host: axonate.example.com
  tls: { enabled: true, clusterIssuer: letsencrypt-prod }

secret:
  existingSecret: ""      # if set, chart does NOT create a Secret
  # otherwise these populate a chart-created Secret:
  masterKey: ""
  saltKey: ""
  adapterToken: ""        # only relevant if a POC adapter is reached externally; usually empty in prod
  databaseUrl: ""
  anthropicApiKey: ""
  openaiApiKey: ""
  minimaxApiKey: ""
  minimaxBaseUrl: ""
```

## Data flow

users → Cloudflare ZT (Google SSO + Tunnel) → cloudflared Deployment → router Service
(ClusterIP, verify JWT → map user→key → cost/quota route) → litellm Service (ClusterIP, virtual
keys, budgets, spend log) → model backends. Postgres external or in-cluster. Identical request
shape to the compose stack.

When `ingress.enabled`, the Ingress routes to the router Service instead of (or alongside)
cloudflared; operator's responsibility to keep the security posture (Cloudflare in front, or
equivalent).

## Configuration

- litellm config + routing.yaml ship as a ConfigMap built from the existing
  `services/litellm/litellm_config*.yaml` and `services/router/routing.yaml`. `configMode`
  selects poc vs prod litellm config — same file the compose `LITELLM_CONFIG` env selects.
- Image tags reuse the digest pins already in `docker-compose.yml` for litellm/postgres; the
  router/auth images are built and pushed to the operator's registry (documented in README).

## Error handling / edge cases

- `postgresql.enabled=false` and no `externalDatabase.databaseUrl`/`existingSecret` → chart
  fails fast at template time with a clear `required`/`fail` message.
- `existingSecret` set → `secret.yaml` is not rendered; deployments reference the named secret.
- `cloudflared.enabled=false` and `ingress.enabled=false` → render a NOTES/`fail` warning that
  the gateway has no ingress path.
- litellm first boot runs ~128 DB migrations (minutes) — readiness probe on
  `/health/liveliness`; document the slow first rollout.
- backup CronJob only rendered with in-cluster PG (external PG → cloud provider owns backups).

## Testing / acceptance

1. `helm lint deploy/helm/axonate` clean.
2. `helm template` with defaults (external PG) renders valid manifests; `--validate` against a
   cluster (kind/minikube) installs.
3. In-cluster PG path: `--set postgresql.enabled=true` brings up PG + litellm migrates + router
   ready; `/route/explain` and litellm `/health/liveliness` reachable via port-forward.
4. cloudflared path: tunnel Deployment connects with a real token (manual/your-account).
5. Ingress path: `--set ingress.enabled=true` renders an Ingress to the router Service.
6. `existingSecret` path: deployments mount the referenced secret; no chart Secret created.

## Non-goals (this pass — documented as "future" in chart README, NOT built)

- HorizontalPodAutoscaler, PodDisruptionBudget, NetworkPolicy.
- External Secrets Operator / sealed-secrets integration.
- Multi-environment values overlays (staging/prod) — single values.yaml + `--set`/`-f` for now.
- GitOps (ArgoCD/Flux) wiring.

These add real template surface; revisit once the chart is in use. Secrets are plain k8s Secrets
(or `existingSecret`) until then.
