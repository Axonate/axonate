"""Axonate CLI adapter (POC, DISPOSABLE).

OpenAI-compatible FastAPI server that wraps the Claude Code CLI (`claude -p`) and the
Codex CLI (`codex exec`). It exists only so the POC has a backend to talk to before real
provider API keys exist. At the production swap this whole service is deleted and LiteLLM
points the same model names at real APIs instead.

Endpoints: GET /health, GET /v1/models, POST /v1/chat/completions (stream + non-stream).
Binds 127.0.0.1 only. Runs under the `poc` compose profile exclusively.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

ADAPTER_TOKEN = os.environ.get("ADAPTER_TOKEN", "")
TIMEOUT = int(os.environ.get("ADAPTER_TIMEOUT", "300"))

# read-only default flags; write profiles get the *_CODE_FLAGS
CLAUDE_FLAGS = os.environ.get("CLAUDE_FLAGS", "--permission-mode plan").split()
CODEX_FLAGS = os.environ.get("CODEX_FLAGS", "--sandbox read-only").split()
CLAUDE_CODE_FLAGS = os.environ.get("CLAUDE_CODE_FLAGS", "--permission-mode acceptEdits").split()
CODEX_CODE_FLAGS = os.environ.get("CODEX_CODE_FLAGS", "--sandbox workspace-write").split()

# project dir used by the write ("-code") profiles
WRITE_DIR = os.environ.get("ADAPTER_WRITE_DIR", "/work")


@dataclass
class Backend:
    cli: str               # "claude" | "codex"
    config_dir: str | None  # CLAUDE_CONFIG_DIR / CODEX_HOME for this account
    write: bool            # write-enabled profile?
    token_env: str | None = None  # env var holding this account's OAuth token (claude headless)


# model name -> backend. Claude auth is headless via CLAUDE_CODE_OAUTH_TOKEN (from `claude
# setup-token`), one token per account. Codex auth lives in CODEX_HOME (device-auth login).
BACKENDS: dict[str, Backend] = {
    "claude":        Backend("claude", os.environ.get("CLAUDE_CONFIG_DIR_A", "/cfg/acctA"), write=False, token_env="CLAUDE_OAUTH_TOKEN_A"),
    "claude-acctB":  Backend("claude", os.environ.get("CLAUDE_CONFIG_DIR_B", "/cfg/acctB"), write=False, token_env="CLAUDE_OAUTH_TOKEN_B"),
    "claude-code":   Backend("claude", os.environ.get("CLAUDE_CONFIG_DIR_A", "/cfg/acctA"), write=True,  token_env="CLAUDE_OAUTH_TOKEN_A"),
    "codex":         Backend("codex",  os.environ.get("CODEX_HOME", "/cfg/codex"),          write=False),
    "codex-code":    Backend("codex",  os.environ.get("CODEX_HOME", "/cfg/codex"),          write=True),
}

app = FastAPI(title="axonate-adapter", version="0.1.0")


def _check_auth(authorization: str | None) -> None:
    if not ADAPTER_TOKEN:
        return
    expected = f"Bearer {ADAPTER_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid adapter token")


def _messages_to_prompt(messages: list[dict]) -> tuple[str, str]:
    """Fold OpenAI messages into (system_prompt, user_prompt) for a single CLI call."""
    system_parts, convo = [], []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # OpenAI content-parts -> text
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role == "system":
            system_parts.append(content)
        else:
            convo.append(f"{role.capitalize()}: {content}")
    return "\n".join(system_parts), "\n\n".join(convo)


async def _run_cli(backend: Backend, system: str, prompt: str) -> str:
    env = dict(os.environ)
    last_msg_file = None

    if backend.cli == "claude":
        if backend.config_dir:
            env["CLAUDE_CONFIG_DIR"] = backend.config_dir
        # headless auth: per-account OAuth token (from `claude setup-token`)
        tok = os.environ.get(backend.token_env or "", "")
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        flags = list(CLAUDE_CODE_FLAGS if backend.write else CLAUDE_FLAGS)
        cmd = ["claude", "-p", prompt, "--output-format", "text", *flags]
        if system:
            cmd += ["--append-system-prompt", system]
        cwd = WRITE_DIR if backend.write else None
        stdin = asyncio.subprocess.DEVNULL  # claude -p warns/waits on stdin otherwise
    else:  # codex
        if backend.config_dir:
            env["CODEX_HOME"] = backend.config_dir
        flags = list(CODEX_CODE_FLAGS if backend.write else CODEX_FLAGS)
        last_msg_file = f"/tmp/codex_last_{uuid.uuid4().hex[:8]}"
        full = f"{system}\n\n{prompt}" if system else prompt
        cmd = ["codex", "exec", *flags, "--skip-git-repo-check",
               "--output-last-message", last_msg_file, full]
        cwd = WRITE_DIR  # codex needs a real cwd; /work is the sandbox
        stdin = asyncio.subprocess.DEVNULL

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=stdin, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"{backend.cli} timed out after {TIMEOUT}s")
    if proc.returncode != 0:
        msg = (err.decode() or out.decode())[:500].strip()
        raise HTTPException(status_code=502, detail=f"{backend.cli} failed: {msg}")

    # codex: clean final answer from the last-message file (stdout has reasoning preamble)
    if last_msg_file:
        try:
            with open(last_msg_file) as f:
                return f.read().strip()
        except FileNotFoundError:
            pass
    return out.decode().strip()


def _completion_json(model: str, text: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        # CLI gives no token counts; report zeros (POC). Real APIs fill this at swap.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _stream_chunks(model: str, text: str) -> AsyncIterator[bytes]:
    """SSE stream. CLI returns full text, so chunk it to satisfy the streaming contract."""
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def frame(delta: dict, finish=None) -> bytes:
        payload = {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    yield frame({"role": "assistant"})
    step = 256
    for i in range(0, len(text), step):
        yield frame({"content": text[i:i + step]})
        await asyncio.sleep(0)  # yield control
    yield frame({}, finish="stop")
    yield b"data: [DONE]\n\n"


@app.get("/health")
async def health():
    return {"status": "ok", "service": "axonate-adapter", "models": list(BACKENDS)}


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "axonate-adapter"} for m in BACKENDS
    ]}


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    body = await request.json()
    model = body.get("model")
    backend = BACKENDS.get(model)
    if backend is None:
        raise HTTPException(status_code=404, detail=f"unknown model '{model}'")
    system, prompt = _messages_to_prompt(body.get("messages", []))
    if not prompt:
        raise HTTPException(status_code=400, detail="no user message")
    text = await _run_cli(backend, system, prompt)
    if body.get("stream"):
        return StreamingResponse(_stream_chunks(model, text), media_type="text/event-stream")
    return JSONResponse(_completion_json(model, text))
