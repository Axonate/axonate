# Public Surfaces + Self-Service Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the router into a surface-aware gateway serving three Cloudflare hostnames — `api.clouddrove.in` (public, key-auth API), `app.clouddrove.in` (Google-gated self-service portal: key + setup + usage), `admin.clouddrove.in` (Access-gated LiteLLM UI) — so lab users self-onboard with no key handouts.

**Architecture:** The router picks behaviour from the inbound `Host` header. On `api.*` it serves only `/v1/*` and forwards the caller's own `sk-` key straight to LiteLLM (key-passthrough, LiteLLM meters per key). On `app.*` it verifies the Cloudflare Access JWT (existing `AUTH_MODE=cloudflare`) and serves the portal. `admin.*` bypasses the router (tunnel → LiteLLM `:4000`) and is pure Cloudflare config. Portal key actions call LiteLLM's admin API with the master key.

**Tech Stack:** Python 3.11+, FastAPI + httpx (existing), LiteLLM admin API (`/key/generate`, `/key/info`, `/user/info`, `/key/delete`), Cloudflare Access + service tokens. No new Python deps.

## Global Constraints

- Read-only privacy default: never render/store prompt or completion text — metadata only.
- Surface is keyed on `Host`: `API_HOST` (default `api.clouddrove.in`), `APP_HOST` (default `app.clouddrove.in`). Admin is not a router surface.
- `api.*` serves only `/v1/*`; any other path → 403. `api.*` auth = caller's `sk-` Bearer, validated + forwarded to LiteLLM (no remap to master key).
- `app.*` requires a verified Cloudflare Access JWT (`AUTH_MODE=cloudflare`); identity = the JWT email.
- Admin emails come from env `ADMIN_EMAILS` (comma-separated, lowercased); admin = all rows / all keys, user = own only.
- Portal key actions are scoped to the verified Access email — a user can only mint/rotate/see their own key.
- LiteLLM admin calls use `LITELLM_MASTER_KEY` as Bearer to `LITELLM_URL`.
- Tests run without Docker via `. .venv/bin/activate && python3 tests/<file>.py` (self-running `__main__` printing PASS/FAIL, exit 1 on fail). Integration steps run live against the eva stack (`ssh eva@192.168.13.121`, repo `/Users/eva/workspace/axonate`, master key `sk-master-change-me`, docker on PATH `/usr/local/bin`).

---

### Task 1: surface resolver + config

**Files:**
- Modify: `services/router/router.py` (add `API_HOST`, `APP_HOST`, `ADMIN_EMAILS` config near the other env reads; add `_surface(request)` and `_is_admin_email(email)` helpers near `_is_admin`)
- Test: `tests/test_surface.py` (new)

**Interfaces:**
- Produces:
  - `API_HOST: str`, `APP_HOST: str` (from env, defaults `api.clouddrove.in` / `app.clouddrove.in`).
  - `ADMIN_EMAILS: set[str]` (lowercased, parsed from comma-separated env `ADMIN_EMAILS`).
  - `_surface(request) -> str` — returns `"api"`, `"app"`, or `"other"` from the request `Host` header (host compared case-insensitively, port stripped).
  - `_is_admin_email(email: str) -> bool` — `email.lower() in ADMIN_EMAILS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_surface.py`:

```python
"""Unit tests for router surface resolution + admin-email check. No Docker."""
import os, sys
os.environ.setdefault("API_HOST", "api.clouddrove.in")
os.environ.setdefault("APP_HOST", "app.clouddrove.in")
os.environ.setdefault("ADMIN_EMAILS", "boss@clouddrove.com, Admin@Clouddrove.com")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
from router import _surface, _is_admin_email  # noqa: E402


class _Req:
    def __init__(self, host): self.headers = {"host": host}


def test_surface_api():
    assert _surface(_Req("api.clouddrove.in")) == "api"
    assert _surface(_Req("api.clouddrove.in:443")) == "api"     # port stripped
    assert _surface(_Req("API.clouddrove.in")) == "api"         # case-insensitive

def test_surface_app():
    assert _surface(_Req("app.clouddrove.in")) == "app"

def test_surface_other():
    assert _surface(_Req("admin.clouddrove.in")) == "other"
    assert _surface(_Req("127.0.0.1:4100")) == "other"
    assert _surface(_Req("")) == "other"

def test_admin_email():
    assert _is_admin_email("boss@clouddrove.com") is True
    assert _is_admin_email("ADMIN@clouddrove.com") is True       # case-insensitive
    assert _is_admin_email("user@clouddrove.com") is False


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"PASS {name}")
            except AssertionError as e: failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `. .venv/bin/activate && python3 tests/test_surface.py`
Expected: ImportError — `cannot import name '_surface'`.

- [ ] **Step 3: Add config + helpers to router.py**

Near the other `os.environ.get(...)` reads at the top of `services/router/router.py`, add:

```python
API_HOST = os.environ.get("API_HOST", "api.clouddrove.in").lower()
APP_HOST = os.environ.get("APP_HOST", "app.clouddrove.in").lower()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
```

Just below the existing `_is_admin` function, add:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python3 tests/test_surface.py`
Expected: all `PASS`, exit 0. Also `python3 tests/test_routing.py` and `python3 tests/test_stats.py` still PASS.

- [ ] **Step 5: Commit**

```bash
git add services/router/router.py tests/test_surface.py
git commit -m "Add router surface resolver (api/app by Host) + admin-email check"
```

---

### Task 2: api key-passthrough auth + path gating

**Files:**
- Modify: `services/router/router.py` (add `_api_identity`; gate `chat` and `/v1/models` by surface)
- Test: covered by live integration (LiteLLM `/key/info` needs the DB) — no unit test; the gating logic verified via curl on eva.

**Interfaces:**
- Consumes: `_surface` (Task 1), existing `LITELLM_URL`, `LITELLM_MASTER_KEY`, `httpx`, `HTTPException`.
- Produces: `async _api_identity(request) -> tuple[str, str]` — for `api.*`: reads the caller's `Authorization: Bearer <sk>`, validates it via LiteLLM `GET /key/info?key=<sk>` (master-key auth), returns `(user_email, caller_key)`; raises `HTTPException(401)` if missing/invalid. A small in-process cache (`_key_user: dict[str,str]`) memoizes key→email.

- [ ] **Step 1: Add `_api_identity` + key cache to router.py**

Add near the auth helpers:

```python
_key_user: dict = {}   # caller sk-key -> email (in-process cache)


async def _api_identity(request: Request) -> tuple:
    """api.* surface: the caller's own LiteLLM key is the identity.
    Validate it via LiteLLM /key/info and return (email, key). 401 if invalid."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing API key")
    key = auth.split(" ", 1)[1].strip()
    if key in _key_user:
        return _key_user[key], key
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
    _key_user[key] = email
    return email, key
```

- [ ] **Step 2: Gate `chat` by surface (path-gate + identity source)**

In the `chat` handler, replace the identity block (step 1 of the handler) so the `api` surface uses `_api_identity` and other surfaces use `resolve_identity`:

```python
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
```

(The rest of the handler is unchanged.)

- [ ] **Step 3: Gate `/v1/models` the same way**

Replace the `/v1/models` identity line (currently `_, key = auth_shim.resolve_identity(request.headers)`) with:

```python
    if _surface(request) == "api":
        _, key = await _api_identity(request)
    else:
        _, key = auth_shim.resolve_identity(request.headers)
```

- [ ] **Step 4: Add a request guard so `api.*` serves only `/v1/*`**

Add a middleware just after `app = FastAPI(...)` is created (find the `app = FastAPI(` line):

```python
@app.middleware("http")
async def _api_path_gate(request: Request, call_next):
    if _surface(request) == "api" and not request.url.path.startswith("/v1/"):
        from fastapi.responses import JSONResponse as _JR
        return _JR({"error": "not found on api host"}, status_code=403)
    return await call_next(request)
```

- [ ] **Step 5: Verify live on eva**

Sync + rebuild the router on eva, then test with a real key (mint a throwaway one):

```bash
rsync -az --exclude '.env' --exclude '.venv' --exclude '.superpowers' --exclude 'backups' \
  ./ eva@192.168.13.121:/Users/eva/workspace/axonate/
ssh eva@192.168.13.121 'export PATH=/usr/local/bin:$PATH; cd /Users/eva/workspace/axonate
  docker compose --profile lab up -d --build axonate-router && sleep 6
  MK=sk-master-change-me
  # mint a test key bound to an email
  K=$(curl -fsS -X POST $LITELLM_URL/key/generate -H "Authorization: Bearer $MK" -H "Content-Type: application/json" -d "{\"user_id\":\"tester@clouddrove.com\",\"max_budget\":5}" 2>/dev/null || \
      curl -fsS -X POST http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $MK" -H "Content-Type: application/json" -d "{\"user_id\":\"tester@clouddrove.com\",\"max_budget\":5}")
  echo "minted: ${K}" | head -c 120; echo
  KEY=$(echo "$K" | python3 -c "import sys,json;print(json.load(sys.stdin)[\"key\"])")
  # api surface: send Host: api.clouddrove.in + the caller key
  echo "=== api /v1/models with caller key ==="
  curl -fsS -H "Host: api.clouddrove.in" -H "Authorization: Bearer $KEY" http://127.0.0.1:4100/v1/models | python3 -c "import sys,json;print([m[\"id\"] for m in json.load(sys.stdin)[\"data\"]])"
  echo "=== api non-/v1 path -> 403 ==="
  curl -s -o /dev/null -w "%{http_code}\n" -H "Host: api.clouddrove.in" http://127.0.0.1:4100/trace/view
  echo "=== api bad key -> 401 ==="
  curl -s -o /dev/null -w "%{http_code}\n" -H "Host: api.clouddrove.in" -H "Authorization: Bearer sk-bogus" http://127.0.0.1:4100/v1/models'
```
Expected: models list prints; non-`/v1` path returns `403`; bad key returns `401`.

- [ ] **Step 6: Commit**

```bash
git add services/router/router.py
git commit -m "Add api.* key-passthrough auth + path gating to /v1/*"
```

---

### Task 3: portal key mint/rotate endpoints

**Files:**
- Modify: `services/router/router.py` (add `_litellm_admin` helper + `POST /portal/key` and `GET /portal/me`)
- Test: live integration on eva (LiteLLM admin API needs the DB).

**Interfaces:**
- Consumes: `_surface`, `_is_admin_email`, `auth_shim.resolve_identity`, `AuthError`, `LITELLM_URL`, `LITELLM_MASTER_KEY`.
- Produces:
  - `async _litellm_admin(method, path, **kw)` — httpx call to `LITELLM_URL+path` with master-key auth; raises `HTTPException(502)` on transport error.
  - `GET /portal/me` (app surface) → `{"email":..., "is_admin":bool, "has_key":bool, "spend":float, "max_budget":float}` for the verified Access email.
  - `POST /portal/key` (app surface) → mints (or rotates) the caller's key: deletes any existing key for `user_id=email`, generates a new one (`max_budget` = env `PORTAL_DEFAULT_BUDGET`, default 50), returns `{"key": "sk-..."}` once. Email comes from the verified Access JWT, never from the body.

- [ ] **Step 1: Add the admin helper + endpoints to router.py**

```python
PORTAL_DEFAULT_BUDGET = float(os.environ.get("PORTAL_DEFAULT_BUDGET", "50"))


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
    info = r.json() if r.status_code == 200 else {}
    keys = info.get("keys", []) if isinstance(info, dict) else []
    spend = sum((k.get("spend") or 0) for k in keys) if keys else 0.0
    budget = next((k.get("max_budget") for k in keys if k.get("max_budget") is not None), None)
    return {"email": email, "is_admin": _is_admin_email(email),
            "has_key": bool(keys), "spend": spend, "max_budget": budget}


@app.post("/portal/key")
async def portal_key(request: Request):
    """Generate or rotate THIS user's key (scoped to the verified Access email)."""
    email = await _portal_email(request)
    # revoke existing keys for this user
    info = await _litellm_admin("GET", "/user/info", params={"user_id": email})
    if info.status_code == 200:
        old = [k.get("token") or k.get("key") for k in (info.json().get("keys") or [])]
        old = [k for k in old if k]
        if old:
            await _litellm_admin("POST", "/key/delete", json={"keys": old})
    gen = await _litellm_admin("POST", "/key/generate",
                               json={"user_id": email, "max_budget": PORTAL_DEFAULT_BUDGET})
    if gen.status_code != 200:
        raise HTTPException(status_code=502, detail="key generation failed")
    return {"key": gen.json().get("key")}
```

- [ ] **Step 2: Verify live on eva (dev mode shortcut)**

Because `app.*` requires a real Access JWT, test the endpoints' LiteLLM integration directly against LiteLLM on eva first (proves the admin calls work), then the full Access path is verified in Task 5 on the deployed host. Run on eva:

```bash
ssh eva@192.168.13.121 'export PATH=/usr/local/bin:$PATH; cd /Users/eva/workspace/axonate
  docker compose --profile lab up -d --build axonate-router && sleep 6
  MK=sk-master-change-me
  echo "=== generate for portaltest@clouddrove.com ==="
  curl -fsS -X POST http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $MK" -H "Content-Type: application/json" -d "{\"user_id\":\"portaltest@clouddrove.com\",\"max_budget\":50}" | python3 -c "import sys,json;d=json.load(sys.stdin);print(\"key:\",d[\"key\"][:14]+\"...\")"
  echo "=== user/info shows the key ==="
  curl -fsS "http://127.0.0.1:4000/user/info?user_id=portaltest@clouddrove.com" -H "Authorization: Bearer $MK" | python3 -c "import sys,json;d=json.load(sys.stdin);print(\"keys:\",len(d.get(\"keys\",[])))"'
```
Expected: a key is generated; `user/info` reports ≥1 key. (This confirms the LiteLLM admin API shape the endpoints depend on. If LiteLLM's field names differ — e.g. `token` vs `key` — adjust `_portal`/`portal_key` accordingly and note it in the report.)

- [ ] **Step 3: Commit**

```bash
git add services/router/router.py
git commit -m "Add portal key mint/rotate + /portal/me (LiteLLM admin integration)"
```

---

### Task 4: portal page (`app.*`)

**Files:**
- Modify: `services/router/router.py` (add `GET /portal` page; serve it as the `app.*` root)

**Interfaces:**
- Consumes: `_surface`, `/portal/me`, `/portal/key`, the existing `/trace/stats` + `/trace`, and env `SERVICE_TOKEN_ID` / `SERVICE_TOKEN_HINT` (the shared CF service-token id + a hint string to display; the secret is shown to the user out-of-band, never logged).
- Produces: `GET /portal` (and `/` when host is `app.*`) → the portal HTML.

- [ ] **Step 1: Add the portal page + root routing**

Add a module-level `_PORTAL_HTML` string and the handler. The page (vanilla JS) calls `/portal/me`, offers Generate/Rotate (calls `/portal/key`, shows the `sk-` once with copy + ready-to-paste `ax`/VS Code/chat snippets using `https://api.clouddrove.in/v1`), and embeds the existing dashboard for usage (admins see all via the master-key path is not available in-browser, so the portal usage is the per-user `/trace/view` which already scopes by identity).

```python
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
async function me(){
  const r=await fetch('/portal/me',{credentials:'same-origin'});
  if(!r.ok){$('#who').textContent='not authenticated';return;}
  const d=await r.json();
  $('#who').textContent=d.email+(d.is_admin?' (admin)':'');
  if(d.has_key){
    $('#keyState').innerHTML='You have a key. Spend: $'+(d.spend||0).toFixed(2)+(d.max_budget!=null?(' / $'+d.max_budget):'');
    $('#gen').style.display='none';$('#rot').style.display='';
  }else{
    $('#keyState').textContent='No key yet.';
  }
}
function snippets(key){
  const base='https://api.clouddrove.in/v1';
  return 'ax CLI:\n'+
    '  export AXONATE_URL=https://api.clouddrove.in\n'+
    '  export AXONATE_KEY='+key+'\n\n'+
    'VS Code (Continue/Cline) — OpenAI-compatible provider:\n'+
    '  apiBase: '+base+'\n  apiKey:  '+key+'\n  model:   claude   (or codex, auto)\n\n'+
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
```

- [ ] **Step 2: Serve the portal at `/` for the app surface**

Add (or extend) a root handler so `app.clouddrove.in/` shows the portal while other hosts are unaffected. Add near the other routes:

```python
@app.get("/")
async def root(request: Request):
    from fastapi.responses import HTMLResponse, JSONResponse as _JR
    if _surface(request) == "app":
        return HTMLResponse(_PORTAL_HTML)
    return _JR({"service": "axonate-router", "ok": True})
```

- [ ] **Step 3: Verify the page serves**

```bash
rsync -az --exclude '.env' --exclude '.venv' --exclude '.superpowers' --exclude 'backups' ./ eva@192.168.13.121:/Users/eva/workspace/axonate/
ssh eva@192.168.13.121 'export PATH=/usr/local/bin:$PATH; cd /Users/eva/workspace/axonate
  docker compose --profile lab up -d --build axonate-router && sleep 6
  echo "=== app root serves portal ==="
  curl -fsS -H "Host: app.clouddrove.in" http://127.0.0.1:4100/ | grep -o "Axonate — your access" | head -1
  echo "=== /portal serves ==="
  curl -fsS http://127.0.0.1:4100/portal | grep -c "YOUR API KEY"'
```
Expected: prints `Axonate — your access` and `1`. (Full Google-login + mint flow is verified end-to-end in Task 5 on the deployed host, since it needs a real Access JWT.)

- [ ] **Step 4: Commit**

```bash
git add services/router/router.py
git commit -m "Add app.* self-service portal page (key + setup snippets + usage link)"
```

---

### Task 5: Cloudflare config + onboarding docs

**Files:**
- Create: `docs/SURFACES.md`
- Modify: `.env.example` (document the new vars), `config/poc.env.example` if it lists router vars

**Interfaces:**
- Consumes: everything above; documents the Cloudflare-side setup the code expects.
- Produces: the operator runbook for the three hostnames + the new env vars.

- [ ] **Step 1: Add the new env vars to `.env.example`**

Append to `.env.example` (under the router section):

```bash
# --- public surfaces (clouddrove.in) ---
API_HOST=api.clouddrove.in            # public OpenAI API surface (key-auth)
APP_HOST=app.clouddrove.in            # Google-gated self-service portal
ADMIN_EMAILS=                         # comma-separated admin emails (see everyone / all keys)
PORTAL_DEFAULT_BUDGET=50              # default per-user budget minted by the portal (USD)
SERVICE_TOKEN_ID=                     # shared CF Access service-token client id (display in portal)
SERVICE_TOKEN_HINT=                   # human hint for where to get the service-token secret
```

- [ ] **Step 2: Write `docs/SURFACES.md`**

```markdown
# Public surfaces — api / app / admin on clouddrove.in

Three Cloudflare hostnames front the lab gateway on eva (one tunnel). Host the gateway on
`clouddrove.in` (isolated from the company prod domain); restrict login to your chosen
`@clouddrove.com` accounts.

| Hostname | Tunnel service | Edge auth | Who |
|---|---|---|---|
| `api.clouddrove.in` | `http://axonate-router:4100` | WAF + rate-limit + **service-token** | tools (ax / VS Code / chat apps) |
| `app.clouddrove.in` | `http://axonate-router:4100` | **Access — Google SSO** | every allowed user (self-serve key + usage) |
| `admin.clouddrove.in` | `http://axonate-litellm:4000` | **Access — Google SSO, admins only** | admins (full LiteLLM UI) |

## 1. Tunnel public hostnames

In Zero Trust → Networks → Tunnels → your tunnel → **Public Hostname**, add three:
- `api.clouddrove.in` → HTTP → `axonate-router:4100`
- `app.clouddrove.in` → HTTP → `axonate-router:4100`
- `admin.clouddrove.in` → HTTP → `axonate-litellm:4000`

Each save auto-creates the DNS record.

## 2. Access applications (Google IdP)

- **app**: self-hosted Access app for `app.clouddrove.in`, identity provider Google, policy =
  allow the chosen `@clouddrove.com` emails. Copy the app **AUD** → `.env CF_ACCESS_AUD`.
- **admin**: self-hosted Access app for `admin.clouddrove.in`, Google, policy = admin emails only.
- Team domain (Zero Trust → Settings) → `.env CF_ACCESS_TEAM_DOMAIN`.
- `.env`: `AUTH_MODE=cloudflare`. (The router verifies the Access JWT on `app.*`.)

## 3. Service token for the API

Zero Trust → Access → **Service Auth → Create Service Token**. On `api.clouddrove.in`'s Access app,
add a policy of type **Service Auth** allowing that token (alongside/instead of the human policy).
Put the **Client ID** in `.env SERVICE_TOKEN_ID` (shown in the portal); distribute the **Client
Secret** to users out-of-band. Tools send both as `CF-Access-Client-Id` / `CF-Access-Client-Secret`
headers.

## 4. Router env (`.env`)

```
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
CF_ACCESS_AUD=<app-aud>
API_HOST=api.clouddrove.in
APP_HOST=app.clouddrove.in
ADMIN_EMAILS=you@clouddrove.com
PORTAL_DEFAULT_BUDGET=50
SERVICE_TOKEN_ID=<service-token-client-id>
```

Apply: `make up PROFILE=lab`.

## 5. Onboard a person

1. Add their Google email to the `app.clouddrove.in` Access policy (and `ADMIN_EMAILS` if admin).
2. They open `https://app.clouddrove.in`, Google-login, click **Generate my key**, copy it + the
   shown setup snippet. For API/tool use they also need the shared service-token secret (from you).
3. Done — no key handouts; revoke by removing the email + deleting their key in `admin.*`.

## 6. Verify

- `https://api.clouddrove.in/v1/models` **without** the service-token headers → blocked at the edge.
  With service-token headers + a valid `sk-` key → returns models.
- `https://app.clouddrove.in` without Google login → Access prompt. After login → portal; Generate
  → a working key; usage shows your own rows.
- `https://admin.clouddrove.in` → Access (admin email) → LiteLLM login → admin UI. Non-admin email
  is denied by the Access policy.
```

- [ ] **Step 3: Verify the doc + env render**

Run: `grep -c 'clouddrove.in' docs/SURFACES.md` (expect ≥ 6) and `grep -c 'API_HOST' .env.example` (expect ≥ 1).
Expected: both non-zero.

- [ ] **Step 4: Commit**

```bash
git add docs/SURFACES.md .env.example
git commit -m "Document three public surfaces + onboarding (docs/SURFACES.md)"
```

---

## Self-Review

- **Spec coverage:** surface-aware routing by Host → Task 1; `api.*` key-passthrough + `/v1/*`-only gate → Task 2; portal self-serve key mint/rotate scoped to Access email → Task 3; portal page with setup snippets + usage → Task 4; three-hostname Cloudflare config, service token, Access apps (app + admin), onboarding → Task 5; admin.* = pure CF config (no code) → Task 5; security layers (edge service-token, key validation, budget caps) → Tasks 2+5; privacy (no prompt text) → portal/usage reuse the metadata-only trace. Live E2E from the spec → Task 5 §6.
- **Placeholder scan:** none — full code for helpers, endpoints, portal HTML, and the doc.
- **Type consistency:** `_surface(request)→str`, `_is_admin_email(email)→bool`, `_api_identity(request)→(email,key)`, `_litellm_admin(method,path,**kw)`, `_portal_email(request)→str` are defined once and used consistently; `/portal/me` keys (`email,is_admin,has_key,spend,max_budget`) match what the portal JS reads; `/portal/key` returns `{key}` which the JS reads as `d.key`. LiteLLM field-name caveat (`token` vs `key`) is called out in Task 3 Step 2 to adjust on live verification.
