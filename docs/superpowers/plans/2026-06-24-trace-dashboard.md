# Trace Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the router's bare `/trace/view` table with a self-contained dashboard (summary cards, Chart.js charts, per-model + filterable recent-calls tables) backed by a new `/trace/stats` aggregate endpoint, measuring volume/latency/status (not cost).

**Architecture:** A pure `_stats_from_rows(rows, window, now)` helper in `services/router/router.py` does all aggregation in Python (unit-testable, no SQL). `GET /trace/stats` fetches windowed rows and returns the helper's JSON. `GET /trace/view` is rewritten as one HTML+vanilla-JS page that fetches `/trace/stats` + `/trace` and renders. Same admin-all/user-own auth as `/trace`.

**Tech Stack:** Python 3.11+, FastAPI (existing), asyncpg (existing), Chart.js (client-side CDN, graceful fallback). No new Python dependencies.

## Global Constraints

- Read-only; localhost / Cloudflare-tunnel only; no new open ports.
- Auth: admin (master key, via existing `_is_admin(request)`) sees all rows; non-admin sees only `WHERE user_email = <resolved identity>` (via existing `auth_shim.resolve_identity(request.headers)`), mirroring `/trace`.
- Privacy: never render prompt/completion text — only columns `ts,user_email,model_requested,route,reason,latency_ms,total_tokens,cost,status`.
- `total_tokens`/`cost` are often `0`/`null` (subscription path) — treat as best-effort, never divide by them.
- No `axonate_trace` schema change; no litellm UI change; OpenAI-compatible boundaries untouched.
- Chart.js is a runtime CDN fetch — the page MUST fully work (cards + tables) when it fails; only charts degrade to a notice.
- Tests run without Docker via the repo pattern: `. .venv/bin/activate && python3 tests/<file>.py` (self-running `__main__` loop printing `PASS/FAIL`, exit 1 on failure).

---

### Task 1: `_stats_from_rows` pure aggregation helper + tests

**Files:**
- Modify: `services/router/router.py` (add imports + `_WINDOWS`, `_p95`, `_stats_from_rows` near the other module-level helpers, before the endpoints)
- Test: `tests/test_stats.py` (new)

**Interfaces:**
- Produces:
  - `_WINDOWS: dict[str, tuple[timedelta|None, timedelta]]` — keys `"1h"`,`"24h"`,`"7d"`,`"all"`; value `(span, bucket)` where `span` is the lookback (None for `"all"`) and `bucket` is the series bucket width.
  - `_p95(values: list[int]) -> int` — nearest-rank 95th percentile; `0` for empty.
  - `_stats_from_rows(rows: list[dict], window: str, now: datetime) -> dict` — rows have keys `ts`(tz-aware datetime), `model_requested`, `route`, `latency_ms`, `total_tokens`, `status`. Returns the spec JSON shape (`window, generated_at, totals, by_model, by_status_class, series`).
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stats.py`:

```python
"""Unit tests for the trace-stats aggregation helper — no Docker, no network.
Run: python3 tests/test_stats.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
from router import _stats_from_rows, _p95  # noqa: E402

NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _row(minutes_ago, status, route, latency, tokens=0):
    return {
        "ts": NOW - timedelta(minutes=minutes_ago),
        "model_requested": route,
        "route": route,
        "latency_ms": latency,
        "total_tokens": tokens,
        "status": status,
    }


def test_empty_rows_all_zero():
    s = _stats_from_rows([], "24h", NOW)
    assert s["totals"]["requests"] == 0
    assert s["totals"]["error_rate"] == 0.0
    assert s["totals"]["p95_latency_ms"] == 0
    assert s["by_model"] == []
    assert s["window"] == "24h"


def test_totals_and_error_rate():
    rows = [_row(1, 200, "claude", 100), _row(2, 200, "claude", 200),
            _row(3, 401, "minimax", 50), _row(4, 500, "codex", 80)]
    s = _stats_from_rows(rows, "24h", NOW)
    assert s["totals"]["requests"] == 4
    assert s["totals"]["errors"] == 2          # 401 + 500
    assert s["totals"]["error_rate"] == 0.5
    assert s["totals"]["avg_latency_ms"] == 107  # (100+200+50+80)/4 = 107.5 -> int 107
    assert s["totals"]["models"] == 3


def test_by_model_success_rate_and_latency():
    rows = [_row(1, 200, "claude", 100), _row(2, 500, "claude", 300),
            _row(3, 200, "codex", 40)]
    s = _stats_from_rows(rows, "24h", NOW)
    by = {m["model"]: m for m in s["by_model"]}
    assert by["claude"]["count"] == 2
    assert by["claude"]["success_rate"] == 0.5
    assert by["claude"]["avg_latency_ms"] == 200
    assert by["codex"]["success_rate"] == 1.0
    # ordered by count desc -> claude first
    assert s["by_model"][0]["model"] == "claude"


def test_status_classes():
    rows = [_row(1, 200, "a", 10), _row(2, 404, "a", 10), _row(3, 503, "a", 10), _row(4, 200, "a", 10)]
    s = _stats_from_rows(rows, "24h", NOW)
    classes = {c["class"]: c["count"] for c in s["by_status_class"]}
    assert classes == {"2xx": 2, "4xx": 1, "5xx": 1}


def test_p95_nearest_rank():
    assert _p95([]) == 0
    assert _p95([100]) == 100
    # 20 values 1..20 -> ceil(0.95*20)=19 -> index 18 -> value 19
    assert _p95(list(range(1, 21))) == 19


def test_series_buckets_assign_rows():
    # 24h window -> 1h buckets; rows at 1 and 2 minutes ago land in the last bucket
    rows = [_row(1, 200, "a", 10), _row(2, 500, "a", 10)]
    s = _stats_from_rows(rows, "24h", NOW)
    assert sum(b["ok"] for b in s["series"]) == 1
    assert sum(b["err"] for b in s["series"]) == 1
    assert len(s["series"]) >= 1


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `. .venv/bin/activate && python3 tests/test_stats.py`
Expected: FAIL/ImportError — `cannot import name '_stats_from_rows' from 'router'`.

- [ ] **Step 3: Add the helper to router.py**

In `services/router/router.py`, change the datetime-free imports to include datetime. The file currently has `import time` in its stdlib block — add directly below it:

```python
from datetime import datetime, timedelta, timezone
import math
```

Then, after the `_rate_limited` helper and before the `# ---------- lifecycle ----------` comment, add:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `. .venv/bin/activate && python3 tests/test_stats.py`
Expected: all `PASS`, exit 0. Also confirm no regression: `python3 tests/test_routing.py` still all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/router/router.py tests/test_stats.py
git commit -m "Add pure trace-stats aggregation helper + unit tests"
```

---

### Task 2: `GET /trace/stats` endpoint

**Files:**
- Modify: `services/router/router.py` (add the endpoint next to `/trace`, after `trace_json`)

**Interfaces:**
- Consumes: `_WINDOWS`, `_stats_from_rows` (Task 1); existing `_pool`, `_is_admin`, `auth_shim.resolve_identity`, `AuthError`, `HTTPException`.
- Produces: `GET /trace/stats?window=1h|24h|7d|all` → the `_stats_from_rows` JSON. Consumed by Task 3's page.

- [ ] **Step 1: Add the endpoint**

In `services/router/router.py`, immediately after the `trace_json` function (the `@app.get("/trace")` handler that ends with `return {"rows": ...}`), add:

```python
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
```

- [ ] **Step 2: Verify against the live stack (no unit test — needs the DB)**

The poc stack is running with real claude/codex trace rows. Run:

```bash
MK=sk-master-change-me
curl -fsS -H "Authorization: Bearer $MK" "http://127.0.0.1:4100/trace/stats?window=all" | python3 -m json.tool | head -30
```
Expected: JSON with `totals.requests` > 0, a `by_model` array including `claude`/`codex`, `by_status_class`, and a `series` array. No 500.

If the router image is built from source (it is, via compose), recreate it to pick up the change:
```bash
docker compose --profile poc up -d --build axonate-router && sleep 5
```

- [ ] **Step 3: Verify the window param + bad-value fallback**

```bash
MK=sk-master-change-me
curl -fsS -H "Authorization: Bearer $MK" "http://127.0.0.1:4100/trace/stats?window=bogus" | python3 -c "import sys,json; print('window=', json.load(sys.stdin)['window'])"
```
Expected: `window= 24h` (bad value falls back to default).

- [ ] **Step 4: Commit**

```bash
git add services/router/router.py
git commit -m "Add GET /trace/stats aggregate endpoint"
```

---

### Task 3: Rewrite `GET /trace/view` as the dashboard page

**Files:**
- Modify: `services/router/router.py` (replace the body of the existing `trace_view` handler, ~lines 218-234)

**Interfaces:**
- Consumes: `/trace/stats` (Task 2) and the existing `/trace` endpoint, both fetched client-side. The handler itself only returns a static HTML string (no server-side data fetch, so it cannot fail on DB state — the page handles errors).
- Produces: the dashboard at `GET /trace/view`.

- [ ] **Step 1: Replace the `trace_view` handler**

In `services/router/router.py`, replace the entire existing `trace_view` function (from `@app.get("/trace/view")` through its `return HTMLResponse(html)`) with:

```python
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
  const labels=s.series.map(b=>b.bucket.slice(11,16));
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
```

- [ ] **Step 2: Recreate the router and verify the page serves**

```bash
docker compose --profile poc up -d --build axonate-router && sleep 5
curl -fsS "http://127.0.0.1:4100/trace/view" | grep -o '<h1>Axonate trace</h1>'
curl -fsS "http://127.0.0.1:4100/trace/view" | grep -c 'id="cards"'
```
Expected: prints `<h1>Axonate trace</h1>` and `1` (the cards container is present).

- [ ] **Step 3: Verify in a browser**

Open `http://127.0.0.1:4100/trace/view`. Expected: summary cards populated (requests > 0), the requests-over-time line + by-model donut render, the per-model table lists claude/codex, and the recent-calls table is color-coded (200=green) and filters by model/status/search. Toggle auto-refresh — it reloads every 10s. (Optional offline check: block the CDN — charts show "charts unavailable offline", everything else still works.)

- [ ] **Step 4: Commit**

```bash
git add services/router/router.py
git commit -m "Rewrite /trace/view as a self-contained dashboard"
```

---

## Self-Review

- **Spec coverage:** `/trace/stats` shape + Python-helper aggregation + windows/buckets/p95 → Task 1+2; admin-all/user-own auth reuse → Task 2 (mirrors `/trace`); cards/charts/per-model/recent-calls + filters + auto-refresh + window selector → Task 3; Chart.js CDN with graceful offline fallback → Task 3 `drawCharts` + `onerror`; privacy (no prompt text) → only the safe columns are fetched/rendered; 503 on DB-down → Task 2 endpoint + Task 3 banner; no schema/dep/port change → respected. Spec testing section (pure helper unit tests + live E2E) → Task 1 Step 1 tests + Task 2/3 live curls.
- **Placeholder scan:** none — full code for helper, endpoint, and page.
- **Type consistency:** `_stats_from_rows(rows, window, now)`, `_p95(values)`, `_WINDOWS` names match across Task 1 (defined), Task 1 tests (imported), and Task 2 (called). The page reads exactly the keys Task 1 emits (`totals.{requests,errors,error_rate,avg_latency_ms,p95_latency_ms,total_tokens,models}`, `by_model[].{model,count,success_rate,avg_latency_ms}`, `series[].{bucket,ok,err}`) and the `/trace` row keys (`ts,user_email,model_requested,route,reason,latency_ms,total_tokens,status`) used by the existing `trace_json`.
