"""Axonate router — sits in FRONT of LiteLLM (:4100 -> :4000).

Responsibilities:
  - Identity: resolve the caller via auth_shim (dev or Cloudflare Access JWT) -> virtual key.
  - Routing: model "auto" -> a concrete backend chosen from routing.yaml (keyword/length/cost
    -quota signal). Every other model passes through unchanged.
  - Forward to LiteLLM using the caller's virtual key (so LiteLLM meters per-user budgets).
  - Fallback: on backend failure, try the next backend in cost order (no hang).
  - Trace: one row per call (user, model, route+reason, latency, tokens, cost, status).
  - Rate limit: per-user requests/min (in-memory; Redis seam for later).
  - Headers: X-Router-Route / X-Router-Reason on the response.

This is permanent (production-grade). It does NOT change at the POC->prod swap.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
import math
from collections import defaultdict, deque

import asyncpg
import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import auth_shim
from auth_shim import AuthError

LITELLM_URL = os.environ.get("LITELLM_URL", "http://axonate-litellm:4000")
ROUTING_FILE = os.environ.get("ROUTING_FILE", "/app/routing.yaml")
LOG_PROMPTS = os.environ.get("LOG_PROMPTS", "false").lower() == "true"
RATE_LIMIT = int(os.environ.get("ROUTER_RATE_LIMIT", "60"))  # requests/min/user
REQUEST_TIMEOUT = float(os.environ.get("ROUTER_TIMEOUT", "300"))

PG = {
    "host": os.environ.get("POSTGRES_HOST", "axonate-db"),
    "user": os.environ.get("POSTGRES_USER", "axonate"),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
    "database": os.environ.get("POSTGRES_DB", "axonate"),
}

app = FastAPI(title="axonate-router", version="0.1.0")
_policy: dict = {}
_pool: asyncpg.Pool | None = None
_hits: dict[str, deque] = defaultdict(deque)  # user -> recent request timestamps


# ---------- routing decision ----------

def _decide(prompt: str, policy: dict) -> tuple[str, str]:
    """Return (backend, reason) for an 'auto' request."""
    backends = policy.get("backends", {})
    text = prompt.lower()

    # 1. keyword rules (first match wins) — explicit task intent beats cost bias
    for rule in policy.get("keyword_rules", []):
        for kw in rule.get("keywords", []):
            if kw.lower() in text:
                return rule["backend"], f"keyword '{kw}'"

    # 2. length rule — no strong signal + short/easy prompt -> cheap backend
    #    (this IS the cost_quota.prefer_cheap_for_easy bias)
    lr = policy.get("length_rule", {})
    if len(prompt) <= lr.get("short_char_threshold", 240) and lr.get("short_backend"):
        return lr["short_backend"], f"short prompt (<= {lr.get('short_char_threshold')} chars)"

    # 3. default
    d = policy.get("default", next(iter(backends), "minimax"))
    return d, "default"


def _fallback_order(chosen: str, policy: dict) -> list[str]:
    """chosen first, then remaining backends by ascending cost."""
    backends = policy.get("backends", {})
    rest = sorted((b for b in backends if b != chosen), key=lambda b: backends[b].get("cost", 99))
    return [chosen, *rest]


# ---------- spend / quota (cost-quota signal, best-effort) ----------

async def _near_limit(virtual_key: str) -> bool:
    """True if the caller's key is at/above near_limit_fraction of its budget.
    Best-effort: asks LiteLLM /key/info. Failure -> not near limit (don't block)."""
    cq = _policy.get("cost_quota", {})
    if not (cq.get("enabled") and cq.get("avoid_near_limit")):
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{LITELLM_URL}/key/info",
                            headers={"Authorization": f"Bearer {virtual_key}"})
            info = r.json().get("info", {})
        spend, budget = info.get("spend"), info.get("max_budget")
        if budget:
            return float(spend or 0) >= cq.get("near_limit_fraction", 0.9) * float(budget)
    except Exception:
        pass
    return False


# ---------- trace ----------

async def _trace(row: dict) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO axonate_trace
                   (ts, user_email, model_requested, route, reason, latency_ms,
                    prompt_tokens, completion_tokens, total_tokens, cost, status, prompt_text)
                   VALUES (now(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                row["user"], row["model_requested"], row["route"], row["reason"],
                row["latency_ms"], row["pt"], row["ct"], row["tt"], row["cost"],
                row["status"], row["prompt_text"],
            )
    except Exception as e:  # tracing must never break a request
        print(f"[trace] write failed: {e}", flush=True)


def _rate_limited(user: str) -> bool:
    now = time.time()
    dq = _hits[user]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return True
    dq.append(now)
    return False


# ---------- trace stats (pure, unit-tested) ----------
_WINDOWS = {
    "1h":  (timedelta(hours=1),  timedelta(minutes=5)),
    "24h": (timedelta(hours=24), timedelta(hours=1)),
    "7d":  (timedelta(days=7),   timedelta(hours=6)),
    "all": (None,                timedelta(days=1)),
}


def _p95(values: list) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = max(0, math.ceil(0.95 * len(s)) - 1)
    return int(s[idx])


def _stats_from_rows(rows: list, window: str, now: datetime) -> dict:
    """Aggregate already-fetched trace rows into the dashboard JSON shape.
    rows: dicts with ts(tz-aware), model_requested, route, latency_ms, total_tokens, status."""
    span, bucket = _WINDOWS.get(window, _WINDOWS["24h"])
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    reqs = len(rows)
    errors = sum(1 for r in rows if (r.get("status") or 0) >= 400)
    total_tokens = sum((r.get("total_tokens") or 0) for r in rows)
    avg = int(sum(lat) / len(lat)) if lat else 0

    models: dict = {}
    for r in rows:
        m = r.get("route") or r.get("model_requested") or "?"
        d = models.setdefault(m, {"count": 0, "ok": 0, "lat": []})
        d["count"] += 1
        if (r.get("status") or 0) < 400:
            d["ok"] += 1
        if r.get("latency_ms") is not None:
            d["lat"].append(r["latency_ms"])
    by_model = sorted(
        [{"model": m, "count": d["count"],
          "success_rate": round(d["ok"] / d["count"], 4) if d["count"] else 0.0,
          "avg_latency_ms": int(sum(d["lat"]) / len(d["lat"])) if d["lat"] else 0}
         for m, d in models.items()],
        key=lambda x: -x["count"])

    classes: dict = {}
    for r in rows:
        c = f"{(r.get('status') or 0) // 100}xx"
        classes[c] = classes.get(c, 0) + 1
    by_status_class = sorted(({"class": c, "count": n} for c, n in classes.items()),
                             key=lambda x: x["class"])

    start = (min((r["ts"] for r in rows), default=now - bucket)) if span is None else now - span
    nbuckets = max(1, min(int((now - start) / bucket) + 1, 500))
    buckets = []
    for i in range(nbuckets):
        b0 = start + i * bucket
        buckets.append({"t0": b0, "t1": b0 + bucket, "ok": 0, "err": 0})
    for r in rows:
        ts = r["ts"]
        for b in buckets:
            if b["t0"] <= ts < b["t1"]:
                if (r.get("status") or 0) >= 400:
                    b["err"] += 1
                else:
                    b["ok"] += 1
                break
    series = [{"bucket": b["t0"].replace(microsecond=0).isoformat(),
               "ok": b["ok"], "err": b["err"]} for b in buckets]

    return {
        "window": window,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "totals": {
            "requests": reqs, "errors": errors,
            "error_rate": round(errors / reqs, 4) if reqs else 0.0,
            "avg_latency_ms": avg, "p95_latency_ms": _p95(lat),
            "total_tokens": total_tokens, "models": len(models),
        },
        "by_model": by_model,
        "by_status_class": by_status_class,
        "series": series,
    }


# ---------- lifecycle ----------

_TRACE_DDL = """CREATE TABLE IF NOT EXISTS axonate_trace (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    user_email TEXT, model_requested TEXT, route TEXT, reason TEXT,
    latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
    total_tokens INTEGER, cost DOUBLE PRECISION, status INTEGER,
    prompt_text TEXT)"""


@app.on_event("startup")
async def _startup():
    global _policy, _pool
    with open(ROUTING_FILE) as f:
        _policy = yaml.safe_load(f)
    # Retry: on first boot the DB may be busy with LiteLLM's migrations, so the pool
    # connect or the CREATE TABLE can fail transiently. Back off and retry instead of
    # silently giving up (which previously left the trace table missing until a restart).
    for attempt in range(1, 11):
        try:
            if _pool is None:
                _pool = await asyncpg.create_pool(min_size=1, max_size=5, **PG)
            async with _pool.acquire() as conn:
                await conn.execute(_TRACE_DDL)
            print(f"[startup] trace table ready (attempt {attempt})", flush=True)
            return
        except Exception as e:
            print(f"[startup] trace DB not ready (attempt {attempt}/10): {e}", flush=True)
            await asyncio.sleep(min(attempt * 2, 15))
    print("[startup] trace DB unavailable after retries; continuing without trace", flush=True)
    _pool = None


@app.get("/health")
async def health():
    out = {"status": "ok", "service": "axonate-router", "auth_mode": auth_shim.AUTH_MODE}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{LITELLM_URL}/health/liveliness")
            out["litellm"] = "ok" if r.status_code < 500 else "degraded"
    except Exception:
        out["litellm"] = "unreachable"
        out["status"] = "degraded"
    out["trace_db"] = "ok" if _pool else "down"
    return out


@app.get("/route/explain")
async def route_explain(prompt: str):
    """Show the routing decision for a prompt WITHOUT forwarding. Debug/acceptance aid."""
    chosen, reason = _decide(prompt, _policy)
    return {"prompt_len": len(prompt), "route": chosen, "reason": reason,
            "fallback_order": _fallback_order(chosen, _policy)}


def _is_admin(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    mk = os.environ.get("LITELLM_MASTER_KEY", "")
    return bool(mk) and auth == f"Bearer {mk}"


@app.get("/trace")
async def trace_json(request: Request, limit: int = 100):
    """Read-only trace rows. Admin (master key) sees all; otherwise own rows only."""
    if _pool is None:
        raise HTTPException(503, "trace DB unavailable")
    limit = max(1, min(limit, 1000))
    if _is_admin(request):
        where, args = "", [limit]
    else:
        try:
            user, _ = auth_shim.resolve_identity(request.headers)
        except AuthError as e:
            raise HTTPException(401, str(e))
        where, args = "WHERE user_email=$2", [limit, user]
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT ts,user_email,model_requested,route,reason,latency_ms,
                       total_tokens,cost,status FROM axonate_trace
                {where} ORDER BY id DESC LIMIT $1""", *args)
    return {"rows": [dict(r) for r in rows]}


@app.get("/trace/stats")
async def trace_stats(request: Request, window: str = "24h"):
    """Aggregated trace metrics for the dashboard. Admin sees all; else own rows only."""
    if _pool is None:
        raise HTTPException(503, "trace DB unavailable")
    if window not in _WINDOWS:
        window = "24h"
    span, _ = _WINDOWS[window]
    now = datetime.now(timezone.utc)
    conds, args = [], []
    if span is not None:
        args.append(now - span)
        conds.append(f"ts >= ${len(args)}")
    if not _is_admin(request):
        try:
            user, _ = auth_shim.resolve_identity(request.headers)
        except AuthError as e:
            raise HTTPException(401, str(e))
        args.append(user)
        conds.append(f"user_email = ${len(args)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT ts,user_email,model_requested,route,reason,latency_ms,
                       total_tokens,cost,status FROM axonate_trace
                {where} ORDER BY ts DESC LIMIT 5000""", *args)
    return _stats_from_rows([dict(r) for r in rows], window, now)


@app.get("/trace/view")
async def trace_view(request: Request, limit: int = 100):
    """Minimal HTML trace table — audit + cost + debugging in one place (GOLIVE §5)."""
    data = await trace_json(request, limit)
    head = "<tr><th>time</th><th>user</th><th>requested</th><th>route</th><th>reason</th><th>ms</th><th>tokens</th><th>cost</th><th>status</th></tr>"
    body = "".join(
        f"<tr><td>{r['ts']}</td><td>{r['user_email']}</td><td>{r['model_requested']}</td>"
        f"<td>{r['route']}</td><td>{r['reason']}</td><td>{r['latency_ms']}</td>"
        f"<td>{r['total_tokens']}</td><td>{r['cost'] or ''}</td><td>{r['status']}</td></tr>"
        for r in data["rows"])
    html = (f"<html><head><title>axonate trace</title><style>"
            "body{font:13px monospace;padding:1rem}table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:3px 8px}th{background:#eee}</style></head>"
            f"<body><h3>axonate trace ({len(data['rows'])} rows)</h3>"
            f"<table>{head}{body}</table></body></html>")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.get("/v1/models")
async def models(request: Request):
    try:
        _, key = auth_shim.resolve_identity(request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{LITELLM_URL}/v1/models", headers={"Authorization": f"Bearer {key}"})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    # 1. identity
    try:
        user, key = auth_shim.resolve_identity(request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    # 2. rate limit
    if _rate_limited(user):
        raise HTTPException(status_code=429,
                            detail=f"rate limit exceeded ({RATE_LIMIT}/min). Try again shortly.")

    body = await request.json()
    requested = body.get("model", "")
    stream = bool(body.get("stream"))
    prompt = _last_user_text(body.get("messages", []))

    # 3. route
    if requested == "auto":
        chosen, reason = _decide(prompt, _policy)
        if await _near_limit(key):
            order = _fallback_order(chosen, _policy)
            for alt in order:
                if not await _near_limit(key):
                    break
            reason += " | near budget limit"
        order = _fallback_order(chosen, _policy)
    else:
        chosen, reason = requested, "explicit model"
        order = [requested]

    print(f"[route] user={user} requested={requested} -> {chosen} ({reason})", flush=True)

    # 4. forward with fallback
    started = time.time()
    last_err = None
    for attempt, backend in enumerate(order):
        fwd = dict(body, model=backend)
        try:
            if stream:
                return await _forward_stream(fwd, key, user, requested, backend, reason, prompt)
            resp = await _forward_once(fwd, key)
            latency = int((time.time() - started) * 1000)
            await _trace_from_response(resp, user, requested, backend, reason, latency, prompt)
            out = JSONResponse(resp["json"], status_code=resp["status"])
            out.headers["X-Router-Route"] = backend
            out.headers["X-Router-Reason"] = reason + (f" (fallback #{attempt})" if attempt else "")
            return out
        except (httpx.HTTPError, _UpstreamError) as e:
            last_err = e
            print(f"[fallback] {backend} failed ({e}); trying next", flush=True)
            continue

    raise HTTPException(status_code=502,
                        detail=f"all backends failed for '{requested}'. Last error: {last_err}")


class _UpstreamError(Exception):
    pass


async def _forward_once(body: dict, key: str) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        r = await c.post(f"{LITELLM_URL}/v1/chat/completions",
                         json=body, headers={"Authorization": f"Bearer {key}"})
    if r.status_code >= 500:
        raise _UpstreamError(f"upstream {r.status_code}: {r.text[:200]}")
    cost = r.headers.get("x-litellm-response-cost")
    return {"json": r.json(), "status": r.status_code, "cost": float(cost) if cost else None}


async def _forward_stream(body, key, user, requested, backend, reason, prompt):
    started = time.time()

    async def gen():
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
            async with c.stream("POST", f"{LITELLM_URL}/v1/chat/completions",
                                 json=body, headers={"Authorization": f"Bearer {key}"}) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk
        latency = int((time.time() - started) * 1000)
        await _trace({"user": user, "model_requested": requested, "route": backend,
                      "reason": reason, "latency_ms": latency, "pt": 0, "ct": 0, "tt": 0,
                      "cost": None, "status": 200,
                      "prompt_text": prompt if LOG_PROMPTS else None})

    resp = StreamingResponse(gen(), media_type="text/event-stream")
    resp.headers["X-Router-Route"] = backend
    resp.headers["X-Router-Reason"] = reason
    return resp


async def _trace_from_response(resp, user, requested, backend, reason, latency, prompt):
    usage = (resp["json"] or {}).get("usage", {}) if isinstance(resp["json"], dict) else {}
    await _trace({
        "user": user, "model_requested": requested, "route": backend, "reason": reason,
        "latency_ms": latency,
        "pt": usage.get("prompt_tokens", 0), "ct": usage.get("completion_tokens", 0),
        "tt": usage.get("total_tokens", 0), "cost": resp.get("cost"),
        "status": resp["status"], "prompt_text": prompt if LOG_PROMPTS else None,
    })


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                return "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c
    return ""
