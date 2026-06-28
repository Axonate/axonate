# Response cache — design

**Date:** 2026-06-28
**Status:** approved (brainstorm)
**Track:** Permanent (router — cost/latency edge)

## Goal

Add an **exact-match, in-memory response cache** to the router so identical low-temperature
requests return instantly without a backend call — cutting cost and latency for retries, repeated
dev prompts, and identical questions. A real differentiator over generic gateways (which charge for
caching or don't route-aware cache at all). No new dependency, no Redis container (seam left for
future multi-replica scale), fully testable for the pure parts.

## Non-goals

- Semantic / embedding-based matching (future second layer; exact is the 80/20).
- Redis / cross-replica shared cache (in-memory; the router is single-instance on eva. Redis seam
  in `docker-compose.yml` documented for when it scales out).
- Caching high-temperature creative output by default (preserves variation).

## Cacheability rules

A request is cacheable when **all** hold:
- `cache.enabled` is true (policy).
- `temperature <= cache.max_temperature` (default 0.5). **Absent `temperature` → treated as high
  → not cacheable** (a client opts in by sending a low temperature or `"cache": true`).
- Not bypassed: `body.get("cache") is not False` **and** no `X-Axonate-No-Cache` request header.
- `body.get("cache") is True` forces cacheability regardless of temperature (explicit opt-in).

## Components

### 1. `cache_key(body) -> str` (pure)
sha256 hex of canonical JSON (`sort_keys=True`) of the cache-relevant subset:
`{model, messages, temperature, max_tokens, top_p}`. **Global** — no user identity in the key
(the response is a function of the prompt; identical prompt → identical key → best hit rate; the
lab is trusted). `model` is the **requested** model (e.g. `"auto"`), so a client's repeated call
hits regardless of which backend the scorer would pick now.

### 2. `is_cacheable(body, headers, policy) -> bool` (pure)
Implements the cacheability rules above. `headers` is any case-insensitive mapping.

### 3. In-memory store (module-level)
`_cache: dict[str, tuple[dict, float]]` = `key -> (response_json, expires_at)`.
- `cache_get(key, now) -> dict | None` — returns the stored response if present and not expired;
  expired entry is dropped and returns None.
- `cache_set(key, value, now, ttl, max_entries) -> None` — stores `(value, now+ttl)`; when over
  `max_entries`, evicts the soonest-to-expire entries first (simple bounded map).
- Pure given `now` passed in (testable without real time).

### 4. Integration in the `chat` handler
After identity + rate-limit, before routing:
- `cacheable = is_cacheable(body, request.headers, _policy)`; `ckey = cache_key(body)` when cacheable.
- **Hit** (`cacheable` and `cache_get(ckey)`):
  - Non-stream request → `JSONResponse(stored, 200)` with `X-Axonate-Cache: hit`,
    `X-Router-Route: cache`. Trace one row: `route="cache"`, `reason="cache hit"`, `latency_ms≈0`,
    tokens from the stored `usage`, `status=200`. **No backend call.**
  - Stream request → replay the stored completion as SSE: one `data: {delta}` chunk carrying the
    full `choices[0].message.content`, then `data: [DONE]`. Same headers + trace.
- **Miss** → route + forward as today, header `X-Axonate-Cache: miss`. On a 2xx response, if
  cacheable, `cache_set(ckey, response_json, ...)`.
  - Non-stream: store `resp["json"]`.
  - **Stream capture:** `_forward_stream` accumulates the streamed `delta.content` into a
    synthesized OpenAI completion object (`{choices:[{message:{role:"assistant",content:<full>}}],
    model, usage:{}}`) and `cache_set`s it on successful stream end (status < 500). So streams —
    the common path for `ax`/Claude Code — both populate and benefit from the cache.

### 5. Config — `cache:` block in `routing.yaml`
```yaml
cache:
  enabled: true
  ttl_seconds: 3600
  max_temperature: 0.5
  max_entries: 1000
```
`enabled: false` = rollback (cache fully bypassed).

### 6. Observability
Cache hits are traced with `route="cache"` / `reason="cache hit"`, so the existing dashboard and
`/trace/stats` already count them. A follow-up can add a "cache hit-rate / calls saved" card to the
dashboard (out of scope here; the data is captured).

## Data flow

```
request -> identity -> rate-limit
        -> cacheable? & key
        -> HIT:  return stored (or SSE-replay), header X-Axonate-Cache: hit, trace route=cache, NO backend
        -> MISS: score -> forward (stream or once) -> on 2xx & cacheable: cache_set
                 (stream path accumulates content -> synthesized completion -> cache_set on end)
```

## Error handling / edge cases

- `temperature` absent → not cacheable (documented; opt in via low temp or `cache:true`).
- Backend error / non-2xx → not cached (only successful responses stored).
- Streaming error mid-stream → no `cache_set` (the `finally`/status guard skips it).
- Cache disabled → `is_cacheable` returns False everywhere → exact legacy behavior.
- Store full → evict soonest-to-expire; never unbounded.
- A cached entry served as a stream produces a single content chunk (not token-by-token) — content
  identical, just not incrementally streamed; acceptable for a cache hit (and instant).
- Health/scoring untouched on a hit (no backend chosen) — a hit records `route="cache"`, which the
  scorer ignores (cache isn't a backend in `policy.backends`).

## Testing

Unit (`tests/test_cache.py`, no Docker):
- `cache_key`: identical body → identical key; different prompt/model/temperature/max_tokens →
  different key; key independent of unrelated fields (e.g. `stream`, `user`).
- `is_cacheable`: temp ≤ threshold cacheable; temp > threshold not; absent temp not; `cache:false`
  bypass; `X-Axonate-No-Cache` header bypass; `cache:true` forces; `enabled:false` never.
- store: set→get hit; expired (now past `expires_at`) → miss + dropped; eviction past `max_entries`.

Live (eva, when reachable): a low-temp prompt twice → 2nd response carries `X-Axonate-Cache: hit`,
near-zero latency, no new backend trace row for the model (a `route="cache"` row instead); streamed
repeat also hits; `cache:false` forces a fresh backend call.

## Decision notes

- In-memory (not Redis) — single router on eva; zero new deps/containers; resets on restart
  (acceptable — cache is an optimization, cold start just re-warms). Redis seam stays for scale.
- Global key (no per-user) — maximizes hit rate; safe for a trusted lab where the prompt fully
  determines the answer. If multi-tenant isolation is ever needed, add the user/email to the key
  (one-line change) at the cost of hit rate.
- Low-temp-only default — caches deterministic-ish work, preserves creative variation; per-request
  bypass + force flags give full control.
