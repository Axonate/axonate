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
