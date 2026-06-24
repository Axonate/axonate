# Devcontainer — design

**Date:** 2026-06-24
**Status:** approved (brainstorm)
**Track:** POC / developer experience

## Goal

One-click reproducible dev environment: clone the repo, open in a devcontainer, and have the
full Axonate toolchain ready — Python venv with all service deps, Docker/compose/make/gh to
drive the stack, unit tests runnable immediately. Lower the onboarding cost from "read CLAUDE.md
and install five things" to "Reopen in Container".

## Topology decision

**Tools-only container + host Docker socket** (docker-outside-of-docker).

The devcontainer holds only the toolchain. It mounts the host Docker socket, so `make up`,
`make smoke`, `make login-claude`, etc. drive the existing `docker-compose.yml` on the host
daemon — the workflow is unchanged.

Rationale:
- The OAuth-login named volumes (`CLAUDE_CONFIG_DIR`, codex auth) live on the host daemon, so
  tokens **persist across devcontainer rebuilds**. Docker-in-Docker would nest these volumes
  and lose them on rebuild — re-login is the project's documented #1 footgun (CLAUDE.md).
- Fast file I/O, light image, no nested daemon.
- Trade-off: shares the host Docker daemon (not fully isolated). Acceptable for solo/POC dev.

Rejected: Docker-in-Docker (heavier, volume-nesting loses logins); compose-as-devcontainer
(couples the dev env to runtime services).

## Components

### `.devcontainer/devcontainer.json`
- Base image: `mcr.microsoft.com/devcontainers/python:3.11`.
- Features:
  - `ghcr.io/devcontainers/features/docker-outside-of-docker` — docker CLI + compose plugin
    talking to the mounted host socket.
  - `ghcr.io/devcontainers/features/github-cli` — `gh` for PRs.
  - Optional `claude-codex` toggle (off by default) — installs the Claude/Codex CLIs for
    in-devcontainer experiments. NOT required for normal work; the real adapter login happens
    inside the adapter container, not here. Kept off to keep the image lean.
- Mounts: host `/var/run/docker.sock`.
- `postCreateCommand`: runs `.devcontainer/post-create.sh`.
- VS Code customizations: extensions (`ms-python.python`, `ms-azuretools.vscode-docker`,
  `redhat.vscode-yaml`); format-on-save; point Python at `.venv`.
- `forwardPorts`: 4000 (LiteLLM UI/proxy), 4100 (router).
- `remoteUser`: `vscode`.

### `.devcontainer/post-create.sh`
- Create `.venv` (Python 3.11) if absent.
- `pip install` the union of `services/router/requirements.txt`,
  `services/adapter/requirements.txt`, and any auth deps — so `python3 tests/test_routing.py`
  runs on open.
- `cp .env.example .env` only if `.env` is missing (never overwrite secrets).
- Print a short next-steps banner: `make up PROFILE=poc`, then `make smoke`.

## Data flow

Developer → devcontainer (toolchain) → `make` → docker CLI over host socket → compose stack on
host daemon. No change to how services talk to each other.

## Error handling / edge cases

- Missing host Docker socket → docker commands fail with the standard daemon-unreachable error;
  documented in the devcontainer README note.
- `.env` already present → post-create leaves it untouched.
- Rebuild of devcontainer → host volumes (logins, Postgres data) untouched; no re-login.

## Testing / acceptance

1. "Reopen in Container" builds without error.
2. After open: `. .venv/bin/activate && python3 tests/test_routing.py` passes.
3. `make config` validates compose from inside the container.
4. `docker ps` from inside the container lists host containers (socket mount works).
5. Rebuild the devcontainer → previously-running stack + any logins survive.

## Non-goals

- No adapter OAuth login inside the devcontainer (stays in the adapter container).
- No Docker-in-Docker.
- No change to `docker-compose.yml`, Makefile, or service code.
