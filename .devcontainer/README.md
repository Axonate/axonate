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
