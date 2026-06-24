# Devcontainer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-click VS Code devcontainer that provisions the full Axonate POC toolchain (Python venv with all service deps, Docker/compose/make/gh via the host socket) so `make`-driven workflows and unit tests run immediately after "Reopen in Container".

**Architecture:** Tools-only container (docker-outside-of-docker). The devcontainer holds the toolchain and mounts the host Docker socket; `make up/smoke/login-*` drive the existing `docker-compose.yml` on the host daemon. OAuth-login named volumes stay on the host → tokens survive devcontainer rebuilds.

**Tech Stack:** VS Code Dev Containers spec, `mcr.microsoft.com/devcontainers/python:3.11` base, devcontainer Features (docker-outside-of-docker, github-cli), bash post-create script, Python 3.11 venv.

## Global Constraints

- Python 3.11+ (matches `services/router/requirements.txt`, FastAPI 0.138 / uvicorn 0.49).
- Never overwrite an existing `.env` (holds secrets; `.env` is gitignored).
- Do NOT modify `docker-compose.yml`, `Makefile`, or any service code.
- No Docker-in-Docker; mount the host `/var/run/docker.sock`.
- Do NOT bundle Claude/Codex CLIs in the base image (real adapter login lives in the adapter container); expose them only as an off-by-default option.
- All service-prefix / naming conventions in CLAUDE.md remain untouched.

---

### Task 1: post-create provisioning script

**Files:**
- Create: `.devcontainer/post-create.sh`

**Interfaces:**
- Consumes: `services/router/requirements.txt`, `services/adapter/requirements.txt`, `.env.example` (all existing).
- Produces: a `.venv/` at repo root with router+adapter deps; a `.env` only if absent. Invoked by `devcontainer.json` `postCreateCommand` (Task 2) as `bash .devcontainer/post-create.sh`.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Devcontainer post-create: provision the Axonate POC toolchain.
# Idempotent — safe to re-run on rebuild.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Creating Python venv (.venv) if missing"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> Installing service dependencies"
pip install --upgrade pip >/dev/null
pip install -r services/router/requirements.txt
pip install -r services/adapter/requirements.txt

echo "==> Seeding .env from .env.example (only if .env is missing)"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    created .env — edit secrets before 'make up'"
else
  echo "    .env already present — left untouched"
fi

cat <<'BANNER'

==========================================================
 Axonate devcontainer ready.
   . .venv/bin/activate
   python3 tests/test_routing.py     # unit tests (no Docker)
   make up PROFILE=poc               # start the stack (host Docker)
   make smoke                        # pre-launch gate
==========================================================
BANNER
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x .devcontainer/post-create.sh`
Expected: no output; file mode now `0755`.

- [ ] **Step 3: Verify it runs on the host (proxy for in-container run)**

Run: `bash .devcontainer/post-create.sh`
Expected: creates `.venv`, installs deps, prints the banner. `.env` left untouched if it already exists. (Router+adapter requirements cover auth's deps — httpx, pyyaml, python-jose — so `tests/test_routing.py` will import cleanly.)

- [ ] **Step 4: Verify unit tests pass under the new venv**

Run: `. .venv/bin/activate && python3 tests/test_routing.py`
Expected: PASS (same result as the documented `make`-less test path in CLAUDE.md).

- [ ] **Step 5: Commit**

```bash
git add .devcontainer/post-create.sh
git commit -m "Add devcontainer post-create provisioning script"
```

---

### Task 2: devcontainer.json

**Files:**
- Create: `.devcontainer/devcontainer.json`

**Interfaces:**
- Consumes: `.devcontainer/post-create.sh` (Task 1) via `postCreateCommand`.
- Produces: the container definition VS Code reads for "Reopen in Container". Mounts host docker socket; forwards 4000/4100.

- [ ] **Step 1: Write the config**

```jsonc
{
  "name": "Axonate",
  "image": "mcr.microsoft.com/devcontainers/python:3.11",
  "features": {
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": {
      "moby": true,
      "installDockerComposeSwitch": true
    },
    "ghcr.io/devcontainers/features/github-cli:1": {}
  },
  "mounts": [
    "source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"
  ],
  "postCreateCommand": "bash .devcontainer/post-create.sh",
  "forwardPorts": [4000, 4100],
  "portsAttributes": {
    "4000": { "label": "litellm UI/proxy" },
    "4100": { "label": "router" }
  },
  "remoteUser": "vscode",
  "customizations": {
    "vscode": {
      "extensions": [
        "ms-python.python",
        "ms-azuretools.vscode-docker",
        "redhat.vscode-yaml"
      ],
      "settings": {
        "python.defaultInterpreterPath": "${containerWorkspaceFolder}/.venv/bin/python",
        "editor.formatOnSave": true
      }
    }
  }
}
```

- [ ] **Step 2: Validate JSON (strip JSONC comments first)**

Run: `python3 -c "import json,re,sys; t=open('.devcontainer/devcontainer.json').read(); t=re.sub(r'(?m)//.*$','',t); json.loads(t); print('OK')"`
Expected: `OK` (no JSON parse error; comments are valid JSONC but we confirm the structure parses).

- [ ] **Step 3: Sanity-check the schema essentials**

Run: `python3 -c "import json,re; t=re.sub(r'(?m)//.*$','',open('.devcontainer/devcontainer.json').read()); d=json.loads(t); assert d['image'].endswith('python:3.11'); assert '/var/run/docker.sock' in d['mounts'][0]; assert d['postCreateCommand']=='bash .devcontainer/post-create.sh'; assert 4000 in d['forwardPorts'] and 4100 in d['forwardPorts']; print('schema OK')"`
Expected: `schema OK`.

- [ ] **Step 4: Commit**

```bash
git add .devcontainer/devcontainer.json
git commit -m "Add devcontainer.json (tools-only + host docker socket)"
```

---

### Task 3: optional Claude/Codex CLI toggle + devcontainer README note

**Files:**
- Create: `.devcontainer/README.md`
- Modify: `.devcontainer/devcontainer.json` (add a commented optional feature)

**Interfaces:**
- Consumes: the `devcontainer.json` from Task 2.
- Produces: documentation of the topology + how to enable the optional CLIs; no behavior change by default.

- [ ] **Step 1: Add the commented optional feature to devcontainer.json**

In `.devcontainer/devcontainer.json`, inside `"features"`, after the `github-cli` entry, add the commented block (kept off by default to keep the image lean):

```jsonc
    "ghcr.io/devcontainers/features/github-cli:1": {},
    // OPTIONAL — uncomment for in-devcontainer Claude/Codex CLI experiments.
    // Real adapter OAuth login still happens inside the adapter container, NOT here.
    // "ghcr.io/devcontainers/features/node:1": { "version": "lts" }
```

(Node is the runtime both CLIs need; the developer then `npm i -g @anthropic-ai/claude-code @openai/codex` as desired. We only ship the toggle.)

- [ ] **Step 2: Re-validate JSON after the edit**

Run: `python3 -c "import json,re; json.loads(re.sub(r'(?m)//.*$','',open('.devcontainer/devcontainer.json').read())); print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Write the README**

```markdown
# Axonate devcontainer

One-click POC dev environment. **Reopen in Container** in VS Code.

## What it is

A **tools-only** container (docker-outside-of-docker). It holds the toolchain —
Python 3.11 + a `.venv` with all service deps, `docker`/`compose`, `make`, `gh` —
and mounts the **host** Docker socket. So `make up`, `make smoke`,
`make login-claude`, etc. drive the existing `docker-compose.yml` on the host
Docker daemon exactly as documented in the root `CLAUDE.md`.

## Why tools-only (not Docker-in-Docker)

The Claude/Codex OAuth-login named volumes live on the **host** daemon, so tokens
**persist across devcontainer rebuilds**. Docker-in-Docker would nest those
volumes and lose them on rebuild — forcing re-login (the project's #1 footgun).

## First run

`postCreateCommand` runs `.devcontainer/post-create.sh`, which:
1. creates `.venv` and installs `services/router` + `services/adapter` requirements
   (these cover the auth shim's deps too),
2. seeds `.env` from `.env.example` **only if `.env` is missing**,
3. prints next steps.

Then:

```bash
. .venv/bin/activate
python3 tests/test_routing.py     # unit tests, no Docker
make up PROFILE=poc               # start the stack on host Docker
make smoke
```

Ports 4000 (litellm UI/proxy) and 4100 (router) are forwarded.

## Optional: Claude/Codex CLIs in the devcontainer

Off by default. Uncomment the `node` feature in `devcontainer.json`, rebuild, then
`npm i -g @anthropic-ai/claude-code @openai/codex`. This is only for experiments —
the **real** adapter login still happens inside the adapter container.

## Requirements / gotchas

- Needs a running host Docker daemon; the socket is bind-mounted. If docker
  commands report the daemon is unreachable, start Docker on the host.
- Never run `docker compose down -v` — it destroys the login volumes.
```

- [ ] **Step 4: Commit**

```bash
git add .devcontainer/README.md .devcontainer/devcontainer.json
git commit -m "Document devcontainer topology + optional CLI toggle"
```

---

## Self-Review

- **Spec coverage:** topology (tools-only + host socket) → Task 2 mounts; venv+deps+`.env` seed → Task 1; VS Code extensions/format-on-save/forwarded ports → Task 2; optional claude/codex toggle → Task 3; non-goals (no DinD, no adapter login, no compose/Makefile edits) → respected across all tasks + Global Constraints. Acceptance checks 1–5 from the spec map to Task 1 Step 4 (tests), Task 2 Steps 2–3 (config validity), and the README gotchas (socket/rebuild persistence).
- **Placeholder scan:** none — every file has full content.
- **Type consistency:** `postCreateCommand` string in Task 2 matches the script path created in Task 1; README references match the actual script behavior.
