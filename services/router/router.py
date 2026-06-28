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
import re
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
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
ROUTING_FILE = os.environ.get("ROUTING_FILE", "/app/routing.yaml")
PORTAL_DEFAULT_BUDGET = float(os.environ.get("PORTAL_DEFAULT_BUDGET", "50"))
LOG_PROMPTS = os.environ.get("LOG_PROMPTS", "false").lower() == "true"
RATE_LIMIT = int(os.environ.get("ROUTER_RATE_LIMIT", "60"))  # requests/min/user
REQUEST_TIMEOUT = float(os.environ.get("ROUTER_TIMEOUT", "300"))
API_HOST = os.environ.get("API_HOST", "api.clouddrove.in").lower()
APP_HOST = os.environ.get("APP_HOST", "app.clouddrove.in").lower()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
SERVICE_TOKEN_ID = os.environ.get("SERVICE_TOKEN_ID", "")

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


@app.middleware("http")
async def _api_path_gate(request: Request, call_next):
    surf = _surface(request)
    if surf == "api" and not request.url.path.startswith("/v1/"):
        from fastapi.responses import JSONResponse as _JR
        return _JR({"error": "not found on api host"}, status_code=403)
    if surf == "app" and request.url.path.startswith("/v1/"):
        from fastapi.responses import JSONResponse as _JR
        return _JR({"error": "use api.clouddrove.in for the API"}, status_code=403)
    return await call_next(request)


@app.get("/")
async def root(request: Request):
    from fastapi.responses import HTMLResponse, JSONResponse as _JR
    if _surface(request) == "app":
        return HTMLResponse(_PORTAL_HTML)
    return _JR({"service": "axonate-router", "ok": True})


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


# ---------- scoring router (pure, unit-tested): task-fit + cost/budget + live health ----------

# In-memory rolling health per backend: deque of (ok: bool, latency_ms: int), newest last.
_health: dict = {}


def _health_record(backend: str, ok: bool, latency_ms: int, window: int) -> None:
    """Record one completed call's outcome for the backend's rolling health window."""
    dq = _health.get(backend)
    if dq is None or dq.maxlen != window:
        dq = deque(dq or [], maxlen=window)
        _health[backend] = dq
    dq.append((bool(ok), int(latency_ms or 0)))


def detect_tasks(prompt: str, policy: dict) -> set:
    """Word-boundary task detection -> set of task tags (code/reason/summarize/...).
    Word boundaries mean 'code' no longer matches 'encode'/'decode'."""
    text = prompt.lower()
    tags = set()
    for tag, kws in policy.get("task_categories", {}).items():
        for kw in kws:
            kw = str(kw).lower().strip()
            if kw and re.search(r"\b" + re.escape(kw) + r"\b", text):
                tags.add(tag)
                break
    return tags


def health_score(backend: str, policy: dict) -> float:
    """Health in [0,1] from the rolling window: success-rate blended with speed.
    Cold/unknown backend -> the neutral default (so a fresh router doesn't starve a backend)."""
    sc = policy.get("scoring", {})
    dq = _health.get(backend)
    if not dq:
        return float(sc.get("health_default", 0.7))
    success = sum(1 for ok, _ in dq if ok) / len(dq)
    mean_latency = sum(lat for _, lat in dq) / len(dq)
    norm = float(sc.get("latency_norm_ms", 4000)) or 1.0
    speed = 1.0 - min(mean_latency / norm, 1.0)
    return round(0.7 * success + 0.3 * speed, 4)


def is_circuit_broken(backend: str, policy: dict) -> bool:
    """True when failures within the window reach circuit_break_failures."""
    sc = policy.get("scoring", {})
    dq = _health.get(backend)
    if not dq:
        return False
    fails = sum(1 for ok, _ in dq if not ok)
    return fails >= int(sc.get("circuit_break_failures", 3))


def score_backends(tasks: set, near_budget: bool, health_map: dict, policy: dict):
    """Pure scorer. Returns (best_backend, reason, ranked_list).
      score(b) = w_task*task_fit + w_cost'*cost_fit + w_health*health
                 - circuit_break_penalty (if broken)
    cost weight is boosted when the task is easy/empty or the caller is near budget."""
    sc = policy.get("scoring", {})
    w = sc.get("weights", {})
    backends = policy.get("backends", {})
    costs = [b.get("cost", 1) for b in backends.values()] or [1]
    mn, mx = min(costs), max(costs)
    easy = (not tasks) or near_budget
    wcost = float(w.get("cost", 0.5)) * (float(sc.get("cost_easy_boost", 2.0)) if easy else 1.0)

    rows = []
    for name, meta in backends.items():
        strengths = set(meta.get("strengths", []))
        task_fit = (len(tasks & strengths) / len(tasks)) if tasks else 0.0
        cost = meta.get("cost", 1)
        cost_fit = (mx - cost) / (mx - mn) if mx != mn else 1.0
        health = float(health_map.get(name, health_score(name, policy)))
        score = float(w.get("task", 1.0)) * task_fit + wcost * cost_fit + float(w.get("health", 0.75)) * health
        broken = is_circuit_broken(name, policy)
        if broken:
            score -= float(sc.get("circuit_break_penalty", 100))
        rows.append({"name": name, "score": score, "cost": cost, "fit": task_fit, "broken": broken})

    if not rows:                                   # no backends configured -> policy default
        d = policy.get("default", "")
        return d, "no backends configured", [d] if d else []
    rows.sort(key=lambda r: (-r["score"], r["cost"], r["name"]))  # deterministic
    best = rows[0]
    if best["broken"]:
        reason = f"all backends degraded -> {best['name']} (least-bad)"
    elif near_budget:
        reason = f"near budget -> {best['name']} (cheapest healthy)"
    elif best["fit"] > 0:
        reason = f"{'/'.join(sorted(tasks))} task -> {best['name']}"
    else:
        reason = f"no strong signal -> {best['name']} (cheap + healthy)"
    return best["name"], reason, [r["name"] for r in rows]


def _decide_scored(prompt: str, near_budget: bool, policy: dict):
    """Scored 'auto' decision. Returns (backend, reason, ranked). Wraps the pure units with the
    live in-memory health snapshot."""
    tasks = detect_tasks(prompt, policy)
    health_map = {b: health_score(b, policy) for b in policy.get("backends", {})}
    return score_backends(tasks, near_budget, health_map, policy)


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
    """Show the routing decision for a prompt WITHOUT forwarding. Debug/acceptance + tuning aid."""
    if _policy.get("scoring", {}).get("enabled"):
        tasks = detect_tasks(prompt, _policy)
        chosen, reason, order = _decide_scored(prompt, False, _policy)
        return {"prompt_len": len(prompt), "mode": "scoring", "route": chosen, "reason": reason,
                "tasks": sorted(tasks), "fallback_order": order,
                "health": {b: health_score(b, _policy) for b in _policy.get("backends", {})},
                "circuit_broken": [b for b in _policy.get("backends", {}) if is_circuit_broken(b, _policy)]}
    chosen, reason = _decide(prompt, _policy)
    return {"prompt_len": len(prompt), "mode": "legacy", "route": chosen, "reason": reason,
            "fallback_order": _fallback_order(chosen, _policy)}


def _is_admin(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    mk = os.environ.get("LITELLM_MASTER_KEY", "")
    return bool(mk) and auth == f"Bearer {mk}"


def _surface(request: Request) -> str:
    """Which public surface served this request, by Host header."""
    host = request.headers.get("host", "").split(":")[0].lower()
    if host == API_HOST:
        return "api"
    if host == APP_HOST:
        return "app"
    return "other"


def _is_admin_email(email: str) -> bool:
    return email.lower() in ADMIN_EMAILS


_key_user: dict = {}   # caller sk-key -> (email, cached_at) (in-process cache)
_KEY_CACHE_TTL = 60    # seconds; cap revocation latency so revoked keys stop working


async def _api_identity(request: Request) -> tuple:
    """api.* surface: the caller's own LiteLLM key is the identity.
    Validate it via LiteLLM /key/info and return (email, key). 401 if invalid."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing API key")
    key = auth.split(" ", 1)[1].strip()
    hit = _key_user.get(key)
    if hit and (time.time() - hit[1]) < _KEY_CACHE_TTL:
        return hit[0], key
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{LITELLM_URL}/key/info",
                            params={"key": key},
                            headers={"Authorization": f"Bearer {os.environ.get('LITELLM_MASTER_KEY','')}"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"key validation failed: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid API key")
    email = (r.json().get("info", {}) or {}).get("user_id") or f"key:{key[-6:]}"
    _key_user[key] = (email, time.time())
    return email, key


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
async def trace_view():
    """Self-contained dashboard: cards + charts + filterable tables (GOLIVE §5).
    Fetches /trace/stats and /trace client-side. Charts via Chart.js CDN with
    graceful fallback; all other content works offline."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Axonate trace</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0f1117;--card:#1a1d27;--line:#2a2f3a;--fg:#e6e8ee;--mut:#8a90a0;--ok:#3fb950;--warn:#d29922;--err:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;padding:1rem 1.25rem;border-bottom:1px solid var(--line)}
h1{font-size:1.1rem;margin:0}.badge{font-size:.75rem;color:var(--mut);border:1px solid var(--line);border-radius:99px;padding:2px 10px}
select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 9px;font:inherit}
main{padding:1.25rem;max-width:1100px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1.25rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.85rem 1rem}
.card .k{color:var(--mut);font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:1.5rem;font-weight:600;margin-top:.2rem}
.grid2{display:grid;grid-template-columns:2fr 1fr;gap:1rem;margin-bottom:1.25rem}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem}
.panel h2{font-size:.8rem;color:var(--mut);text-transform:uppercase;margin:0 0 .6rem}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600}.s2xx{color:var(--ok)}.s4xx{color:var(--warn)}.s5xx,.s0xx{color:var(--err)}
.filters{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.6rem}.filters input{flex:1;min-width:120px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
.note{color:var(--mut);font-size:.8rem;padding:1.5rem;text-align:center}
</style></head><body>
<header>
  <h1>Axonate trace</h1>
  <span id="scope" class="badge">…</span>
  <span style="flex:1"></span>
  <label>window <select id="window"><option>1h</option><option selected>24h</option><option>7d</option><option>all</option></select></label>
  <label><input type="checkbox" id="auto"> auto-refresh</label>
  <button id="refresh">refresh</button>
</header>
<main>
  <div id="banner"></div>
  <div class="cards" id="cards"></div>
  <div class="grid2">
    <div class="panel"><h2>Requests over time</h2><div id="lineWrap"><canvas id="line" height="120"></canvas></div></div>
    <div class="panel"><h2>By model</h2><div id="donutWrap"><canvas id="donut" height="120"></canvas></div></div>
  </div>
  <div class="panel" style="margin-bottom:1.25rem"><h2>Per model</h2><table id="modelTbl"><thead><tr><th>model</th><th>count</th><th>success%</th><th>avg ms</th></tr></thead><tbody></tbody></table></div>
  <div class="panel"><h2>Recent calls</h2>
    <div class="filters">
      <select id="fModel"><option value="">all models</option></select>
      <select id="fStatus"><option value="">all status</option><option value="2">2xx</option><option value="4">4xx</option><option value="5">5xx</option></select>
      <input id="fSearch" placeholder="search reason / user…">
    </div>
    <table id="callsTbl"><thead><tr><th>time</th><th>user</th><th>requested</th><th>route</th><th>reason</th><th>ms</th><th>tokens</th><th>status</th></tr></thead><tbody></tbody></table>
  </div>
</main>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4" onerror="window.__noChart=true"></script>
<script>
const $=s=>document.querySelector(s); let calls=[]; let lineChart, donutChart;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cls=st=>'s'+Math.floor((st||0)/100)+'xx';
async function load(){
  const w=$('#window').value;
  try{
    const [stats,trace]=await Promise.all([
      fetch('/trace/stats?window='+encodeURIComponent(w),{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}),
      fetch('/trace?limit=100',{credentials:'same-origin'}).then(r=>r.ok?r.json():{rows:[]})
    ]);
    $('#banner').innerHTML='';
    render(stats); calls=trace.rows||[]; populateModelFilter(); renderCalls();
  }catch(e){ $('#banner').innerHTML='<div class="note">trace DB unavailable ('+esc(e.message)+')</div>'; }
}
function card(k,v){return '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>';}
function render(s){
  const t=s.totals;
  $('#scope').textContent='window: '+s.window;
  $('#cards').innerHTML=[card('Requests',t.requests),card('Error rate',(t.error_rate*100).toFixed(1)+'%'),
    card('Avg latency',t.avg_latency_ms+' ms'),card('p95 latency',t.p95_latency_ms+' ms'),
    card('Tokens',t.total_tokens),card('Models',t.models)].join('');
  $('#modelTbl tbody').innerHTML=s.by_model.map(m=>'<tr><td>'+esc(m.model)+'</td><td>'+m.count+'</td><td>'+(m.success_rate*100).toFixed(0)+'%</td><td>'+m.avg_latency_ms+'</td></tr>').join('')||'<tr><td colspan=4 class="note">no data</td></tr>';
  drawCharts(s);
}
function drawCharts(s){
  if(window.__noChart||typeof Chart==='undefined'){
    $('#lineWrap').innerHTML='<div class="note">charts unavailable offline</div>';
    $('#donutWrap').innerHTML='<div class="note">charts unavailable offline</div>'; return;
  }
  const labels=s.series.map(b=>b.bucket.slice(5,16).replace('T',' '));
  lineChart&&lineChart.destroy(); donutChart&&donutChart.destroy();
  lineChart=new Chart($('#line'),{type:'line',data:{labels,datasets:[
    {label:'ok',data:s.series.map(b=>b.ok),borderColor:'#3fb950',tension:.3},
    {label:'err',data:s.series.map(b=>b.err),borderColor:'#f85149',tension:.3}]},
    options:{plugins:{legend:{labels:{color:'#8a90a0'}}},scales:{x:{ticks:{color:'#8a90a0'}},y:{ticks:{color:'#8a90a0'}}}}});
  donutChart=new Chart($('#donut'),{type:'doughnut',data:{labels:s.by_model.map(m=>m.model),
    datasets:[{data:s.by_model.map(m=>m.count),backgroundColor:['#58a6ff','#3fb950','#d29922','#f85149','#a371f7','#79c0ff']}]},
    options:{plugins:{legend:{labels:{color:'#8a90a0'}}}}});
}
function populateModelFilter(){
  const set=[...new Set(calls.map(c=>c.route).filter(Boolean))];
  $('#fModel').innerHTML='<option value="">all models</option>'+set.map(m=>'<option>'+esc(m)+'</option>').join('');
}
function renderCalls(){
  const fm=$('#fModel').value, fs=$('#fStatus').value, q=$('#fSearch').value.toLowerCase();
  const rows=calls.filter(c=>(!fm||c.route===fm)&&(!fs||String(Math.floor((c.status||0)/100))===fs)
    &&(!q||((c.reason||'')+' '+(c.user_email||'')).toLowerCase().includes(q)));
  $('#callsTbl tbody').innerHTML=rows.map(c=>'<tr><td>'+esc(c.ts).slice(0,19).replace('T',' ')+'</td><td>'+esc(c.user_email)+'</td><td>'+esc(c.model_requested)+'</td><td>'+esc(c.route)+'</td><td>'+esc(c.reason)+'</td><td>'+esc(c.latency_ms)+'</td><td>'+esc(c.total_tokens)+'</td><td class="'+cls(c.status)+'">'+esc(c.status)+'</td></tr>').join('')||'<tr><td colspan=8 class="note">no rows</td></tr>';
}
$('#window').onchange=load; $('#refresh').onclick=load;
$('#fModel').onchange=renderCalls; $('#fStatus').onchange=renderCalls; $('#fSearch').oninput=renderCalls;
let timer=null;
$('#auto').onchange=e=>{ if(e.target.checked){timer=setInterval(load,10000)}else{clearInterval(timer)} };
load();
</script></body></html>"""


async def _litellm_admin(method: str, path: str, **kw):
    mk = os.environ.get("LITELLM_MASTER_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            return await c.request(method, f"{LITELLM_URL}{path}",
                                   headers={"Authorization": f"Bearer {mk}"}, **kw)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"litellm admin call failed: {e}")


async def _portal_email(request: Request) -> str:
    """Verified Access email for app.* surface, or 401/403."""
    if _surface(request) != "app":
        raise HTTPException(status_code=403, detail="portal only on app host")
    try:
        email, _ = auth_shim.resolve_identity(request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return email.lower()


@app.get("/portal/me")
async def portal_me(request: Request):
    email = await _portal_email(request)
    r = await _litellm_admin("GET", "/user/info", params={"user_id": email})
    if r.status_code == 200:
        info = r.json()
        keys = info.get("keys", []) if isinstance(info, dict) else []
        spend = sum((k.get("spend") or 0) for k in keys) if keys else 0.0
        budget = next((k.get("max_budget") for k in keys if k.get("max_budget") is not None), None)
        has_key = bool(keys)
    else:
        # user record not yet in LiteLLM DB — check key list directly
        kl = await _litellm_admin("GET", "/key/list", params={"user_id": email})
        kl_data = kl.json() if kl.status_code == 200 else {}
        has_key = bool(kl_data.get("keys"))
        spend = 0.0
        budget = None
    return {"email": email, "is_admin": _is_admin_email(email),
            "has_key": has_key, "spend": spend, "max_budget": budget,
            "service_token_id": SERVICE_TOKEN_ID}


@app.get("/portal")
async def portal_page(request: Request):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_PORTAL_HTML)


_PORTAL_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Axonate — your access</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#0f1117;color:#e6e8ee;font:14px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:1.5rem}
h1{font-size:1.2rem}.card{background:#1a1d27;border:1px solid #2a2f3a;border-radius:10px;padding:1rem;margin:1rem 0}
button{background:#238636;color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button.sec{background:#30363d}
code,pre{background:#0d1117;border:1px solid #2a2f3a;border-radius:6px;padding:.4rem .6rem;display:block;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,monospace;font-size:12.5px}
.k{color:#8a90a0;font-size:.8rem}.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
a{color:#58a6ff}.warn{color:#d29922}
</style></head><body><main>
<h1>Axonate — your access</h1>
<div id="who" class="k">…</div>

<div class="card">
  <div class="k">YOUR API KEY</div>
  <div id="keyState">…</div>
  <div class="row" style="margin-top:.6rem">
    <button id="gen">Generate my key</button>
    <button id="rot" class="sec" style="display:none">Rotate key</button>
  </div>
  <p class="warn" id="once" style="display:none">Shown once — copy it now. We can't show it again (use Rotate if lost).</p>
  <pre id="keyOut" style="display:none"></pre>
</div>

<div class="card">
  <div class="k">SETUP — base URL <code>https://api.clouddrove.in/v1</code></div>
  <div class="k" style="margin-bottom:.4rem">api.clouddrove.in also requires the Cloudflare service-token headers below; get the Client Secret from your admin.</div>
  <div id="setup" class="k">Generate a key to see ready-to-paste setup.</div>
</div>

<div class="card">
  <div class="k">YOUR USAGE</div>
  <p><a href="/trace/view">Open usage dashboard →</a></p>
</div>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let SVC = '';
async function me(){
  const r=await fetch('/portal/me',{credentials:'same-origin'});
  if(!r.ok){$('#who').textContent='not authenticated';return;}
  const d=await r.json();
  SVC = d.service_token_id || '';
  $('#who').textContent=d.email+(d.is_admin?' (admin)':'');
  if(d.has_key){
    $('#keyState').innerHTML='You have a key. Spend: $'+Number(d.spend||0).toFixed(2)+(d.max_budget!=null?(' / $'+d.max_budget):'');
    $('#gen').style.display='none';$('#rot').style.display='';
  }else{
    $('#keyState').textContent='No key yet.';
  }
}
function snippets(key){
  const base='https://api.clouddrove.in/v1';
  const svcId=esc(SVC)||'YOUR_SERVICE_TOKEN_ID';
  return 'NOTE: api.clouddrove.in also requires Cloudflare service-token headers;\n'+
    'get the Client Secret from your admin.\n\n'+
    'curl example:\n'+
    '  curl '+base+'/chat/completions \\\n'+
    '    -H "Authorization: Bearer '+key+'" \\\n'+
    '    -H "CF-Access-Client-Id: '+svcId+'" \\\n'+
    '    -H "CF-Access-Client-Secret: <ask admin>" \\\n'+
    '    -d \'{"model":"auto","messages":[{"role":"user","content":"hi"}]}\'\n\n'+
    'ax CLI:\n'+
    '  export AXONATE_URL=https://api.clouddrove.in\n'+
    '  export AXONATE_KEY='+key+'\n'+
    '  export AXONATE_CF_CLIENT_ID='+svcId+'\n'+
    '  export AXONATE_CF_CLIENT_SECRET=<ask admin>\n\n'+
    'Claude Code (Anthropic /v1/messages):\n'+
    '  export ANTHROPIC_BASE_URL=https://api.clouddrove.in\n'+
    '  export ANTHROPIC_AUTH_TOKEN='+key+'\n'+
    '  export ANTHROPIC_CUSTOM_HEADERS="CF-Access-Client-Id: '+svcId+'\\nCF-Access-Client-Secret: <ask admin>"\n'+
    '  # then set the model to an Axonate name: claude / codex\n\n'+
    'VS Code (Continue/Cline) — OpenAI-compatible provider:\n'+
    '  apiBase: '+base+'\n  apiKey:  '+key+'\n  model:   claude   (or codex, auto)\n'+
    '  requestOptions.headers:\n'+
    '    CF-Access-Client-Id: '+svcId+'\n'+
    '    CF-Access-Client-Secret: <ask admin>\n\n'+
    'Chat app (Open WebUI / Jan) — OpenAI connection:\n'+
    '  Base URL: '+base+'\n  API key:  '+key+'\n  Model:    claude / codex / auto';
}
async function gen(){
  const r=await fetch('/portal/key',{method:'POST',credentials:'same-origin'});
  if(!r.ok){alert('failed to generate key');return;}
  const d=await r.json();
  $('#keyOut').style.display='';$('#once').style.display='';
  $('#keyOut').textContent=d.key;
  $('#setup').innerHTML='<pre>'+esc(snippets(d.key))+'</pre>';
  me();
}
$('#gen').onclick=gen; $('#rot').onclick=()=>{ if(confirm('Rotate? Your old key stops working.')) gen(); };
me();
</script></body></html>"""


@app.post("/portal/key")
async def portal_key(request: Request):
    """Generate or rotate THIS user's key (scoped to the verified Access email)."""
    email = await _portal_email(request)
    # revoke existing keys for this user (works even without a user record)
    kl = await _litellm_admin("GET", "/key/list", params={"user_id": email})
    if kl.status_code == 200:
        old = [k for k in (kl.json().get("keys") or []) if k]
        if old:
            await _litellm_admin("POST", "/key/delete", json={"keys": old})
    gen = await _litellm_admin("POST", "/key/generate",
                               json={"user_id": email, "max_budget": PORTAL_DEFAULT_BUDGET})
    if gen.status_code != 200:
        raise HTTPException(status_code=502, detail="key generation failed")
    return {"key": gen.json().get("key")}


@app.get("/v1/models")
async def models(request: Request):
    try:
        if _surface(request) == "api":
            _, key = await _api_identity(request)
        else:
            _, key = auth_shim.resolve_identity(request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{LITELLM_URL}/v1/models", headers={"Authorization": f"Bearer {key}"})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    # 1. identity — api.* uses the caller's own key; otherwise resolve via auth_shim
    try:
        if _surface(request) == "api":
            user, key = await _api_identity(request)
        else:
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
        near = await _near_limit(key)
        if _policy.get("scoring", {}).get("enabled"):
            chosen, reason, order = _decide_scored(prompt, near, _policy)   # ranked list = fallback order
        else:
            chosen, reason = _decide(prompt, _policy)
            order = _fallback_order(chosen, _policy)
            if near:
                reason += " | near budget limit"
    else:
        chosen, reason = requested, "explicit model"
        order = [requested]

    print(f"[route] user={user} requested={requested} -> {chosen} ({reason})", flush=True)

    # 4. forward with fallback (record verified outcomes into the in-memory health window)
    started = time.time()
    last_err = None
    hwin = int(_policy.get("scoring", {}).get("health_window", 20))
    for attempt, backend in enumerate(order):
        fwd = dict(body, model=backend)
        t0 = time.time()
        try:
            if stream:
                # streaming connects lazily inside the generator; health for streams is best-effort
                return await _forward_stream(fwd, key, user, requested, backend, reason, prompt)
            resp = await _forward_once(fwd, key)
            latency = int((time.time() - started) * 1000)
            _health_record(backend, resp["status"] < 500, int((time.time() - t0) * 1000), hwin)
            await _trace_from_response(resp, user, requested, backend, reason, latency, prompt)
            out = JSONResponse(resp["json"], status_code=resp["status"])
            out.headers["X-Router-Route"] = backend
            out.headers["X-Router-Reason"] = reason + (f" (fallback #{attempt})" if attempt else "")
            return out
        except (httpx.HTTPError, _UpstreamError) as e:
            _health_record(backend, False, int((time.time() - t0) * 1000), hwin)
            last_err = e
            print(f"[fallback] {backend} failed ({e}); trying next", flush=True)
            continue

    raise HTTPException(status_code=502,
                        detail=f"all backends failed for '{requested}'. Last error: {last_err}")


@app.post("/v1/messages")
async def messages_passthrough(request: Request):
    """Anthropic /v1/messages passthrough — lets Claude Code / Anthropic-shaped clients use the
    gateway. Surface-aware identity (api.* = caller key; else auth_shim); forwards to LiteLLM,
    which serves the Anthropic format. No `auto` routing here — the client sends a concrete model
    (set it to an Axonate model name: claude/codex/minimax)."""
    try:
        if _surface(request) == "api":
            user, key = await _api_identity(request)
        else:
            user, key = auth_shim.resolve_identity(request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if _rate_limited(user):
        raise HTTPException(status_code=429,
                            detail=f"rate limit exceeded ({RATE_LIMIT}/min). Try again shortly.")
    body = await request.json()
    model = body.get("model", "")
    stream = bool(body.get("stream"))
    started = time.time()

    if stream:
        async def gen():
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
                async with c.stream("POST", f"{LITELLM_URL}/v1/messages",
                                     json=body, headers={"Authorization": f"Bearer {key}"}) as r:
                    async for chunk in r.aiter_bytes():
                        yield chunk
            latency = int((time.time() - started) * 1000)
            await _trace({"user": user, "model_requested": model, "route": model,
                          "reason": "anthropic /v1/messages", "latency_ms": latency,
                          "pt": 0, "ct": 0, "tt": 0, "cost": None, "status": 200,
                          "prompt_text": None})
        resp = StreamingResponse(gen(), media_type="text/event-stream")
        resp.headers["X-Router-Route"] = model
        return resp

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
        r = await c.post(f"{LITELLM_URL}/v1/messages", json=body,
                         headers={"Authorization": f"Bearer {key}"})
    latency = int((time.time() - started) * 1000)
    try:
        j = r.json()
    except Exception:
        j = {"error": "non-json upstream response"}
    usage = j.get("usage", {}) if isinstance(j, dict) else {}
    pt, ct = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    await _trace({"user": user, "model_requested": model, "route": model,
                  "reason": "anthropic /v1/messages", "latency_ms": latency,
                  "pt": pt, "ct": ct, "tt": pt + ct, "cost": None,
                  "status": r.status_code, "prompt_text": None})
    out = JSONResponse(j, status_code=r.status_code)
    out.headers["X-Router-Route"] = model
    return out


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

    hwin = int(_policy.get("scoring", {}).get("health_window", 20))

    async def gen():
        ok, status = True, 200
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as c:
                async with c.stream("POST", f"{LITELLM_URL}/v1/chat/completions",
                                     json=body, headers={"Authorization": f"Bearer {key}"}) as r:
                    status = r.status_code
                    if status >= 500:
                        ok = False
                    async for chunk in r.aiter_bytes():
                        yield chunk
        except Exception:
            ok = False
            raise
        finally:
            # record the streamed call's outcome so health-based routing works for streams too
            _health_record(backend, ok, int((time.time() - started) * 1000), hwin)
        latency = int((time.time() - started) * 1000)
        await _trace({"user": user, "model_requested": requested, "route": backend,
                      "reason": reason, "latency_ms": latency, "pt": 0, "ct": 0, "tt": 0,
                      "cost": None, "status": status,
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
