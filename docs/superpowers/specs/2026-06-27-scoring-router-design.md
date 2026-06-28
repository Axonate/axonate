# Scoring router — design

**Date:** 2026-06-27
**Status:** approved (brainstorm)
**Track:** Permanent (router — the differentiator)

## Goal

Replace the router's first-match-wins keyword routing for `model: auto` with a **scoring router**
that picks the backend by combining three signals: **task-fit** (does the model's strengths match
the detected task?), **cost/budget** (cheaper when the task is easy or the caller is near budget),
and **live health** (avoid backends that are recently failing or slow). This is Axonate's edge over
generic gateways, which load-balance but don't route by prompt intelligence + real-usage health.

Pure, declarative, no new deps, no added request latency (health is in-memory). Backward compatible:
clear prompts route as before; ambiguous or degraded conditions route smarter.

## Non-goals

- LLM-based task classification (a possible later layer; the `classifier` seam stays off).
- Persisting health across restarts (in-memory; the trace DB remains the dashboard's source of truth).
- Changing non-`auto` behavior — an explicit model name still passes through unchanged.

## Components (each a pure unit, unit-tested without Docker)

### 1. Task detection — `detect_tasks(prompt, policy) -> set[str]`
Classifies the prompt into task tags drawn from the policy's categories
(`code`, `reason`, `summarize`/`bulk`, `chat`, …). Uses **word-boundary** matching (regex `\b`)
so `code` no longer matches `encode`/`decode`, plus a length signal (short prompt → `chat`/easy).
Returns the set of detected tags (possibly empty). Replaces the brittle substring `keyword_rules`.

### 2. Health tracker — in-memory rolling window per backend
A module-level `_health: dict[str, deque]` of recent outcomes `(ok: bool, latency_ms: int)` per
backend, capped at `health_window` entries, updated in the existing `_trace` path (every completed
call). Exposes:
- `health_score(backend, policy) -> float` in `[0,1]` = success-rate component blended with a
  speed component (`1 - min(ewma_latency/latency_norm, 1)`). **Cold/unknown backend → returns the
  neutral default** (treated as healthy so a fresh router doesn't starve a backend).
- `is_circuit_broken(backend, policy) -> bool` = True when the recent failure count within the
  window ≥ `circuit_break_failures`. A broken backend gets a large score penalty (effectively
  skipped) until a success re-enters the window.

### 3. Scorer — `score_backends(tasks, near_budget, health_map, policy) -> (backend, reason, ranked)`
Pure. For each backend `b` in `policy.backends`:
```
task_fit  = overlap(tasks, b.strengths) normalized to [0,1]   # 0 when no detected task matches
cost_fit  = (max_cost - b.cost) / (max_cost - min_cost)        # cheapest -> 1, dearest -> 0
w_cost'   = w_cost * (cost_easy_boost if tasks easy/empty or near_budget else 1)
score(b)  = w_task*task_fit + w_cost'*cost_fit + w_health*health_map[b]
            - (circuit_break_penalty if is_circuit_broken(b) else 0)
```
Returns the argmax backend, a human `reason` naming the dominant factor (e.g.
`"code task -> codex (healthy)"`, `"near budget -> minimax (cheapest)"`,
`"claude circuit-broken -> codex"`), and `ranked` = all backends sorted by score desc (used as the
fallback order). Ties broken by lower cost then name (deterministic).

### 4. `routing.yaml` — declarative weights + knobs
Extends the existing file (keeps `backends`, `default`, `strengths`, `cost_quota`). Adds:
```yaml
scoring:
  enabled: true
  weights: { task: 1.0, cost: 0.5, health: 0.75 }
  cost_easy_boost: 2.0          # multiply cost weight when task easy/empty or caller near budget
  health_window: 20             # recent calls per backend kept for health
  latency_norm_ms: 4000         # latency at/above this -> speed component 0
  circuit_break_failures: 3     # >= this many failures in the window -> circuit-broken
  circuit_break_penalty: 100    # score penalty for a broken backend (so it's skipped)
  health_default: 0.7           # neutral health for cold/unknown backends
task_categories:                # word-boundary keyword sets -> task tags (replaces keyword_rules)
  code:      [code, function, refactor, debug, bug, compile, "stack trace", regex, def, class, import]
  reason:    [reason, analyze, "explain why", design, architecture, "trade-off", plan, prove]
  summarize: [summarize, summary, translate, bulk, extract, classify, rewrite, transform, list]
```
The existing `length_rule` (short→easy) and `cost_quota.near_limit_fraction` are reused. When
`scoring.enabled: false`, the router falls back to the legacy `_decide` (kept for safety/rollback).

## Integration in `router.py`

- `_decide(prompt, policy)` stays as the legacy path; a new `_decide_scored(prompt, near_budget,
  policy)` wraps task detection + health snapshot + scorer. The `chat` handler calls the scored
  path when `scoring.enabled`, else legacy. `near_budget` comes from the existing `_near_limit(key)`.
- `_fallback_order` returns the scorer's `ranked` list when scoring is on (best healthy alternatives
  first) instead of the cost-only sort.
- `_trace` (or the chat success/failure path) records `(ok, latency)` into `_health[backend]`.
- `/route/explain` reports the scored decision + per-backend scores (great for tuning + the demo).

## Data flow

```
auto request -> detect_tasks(prompt) -> {tags}
             -> near_budget = _near_limit(key)         (existing)
             -> health_map = {b: health_score(b)}      (in-memory)
             -> score_backends(...) -> (backend, reason, ranked)
             -> forward to backend; on failure walk `ranked`; record (ok,latency) into _health
```

## Error handling / edge cases

- Empty `tasks` (no signal) → `task_fit` 0 for all → cost + health decide (cheap, healthy wins) —
  matches today's "easy → cheap" bias.
- All backends circuit-broken → penalties cancel out (all equal) → argmax still returns one
  (the otherwise-highest) so the request is attempted, never dropped; fallback walks the rest.
- `near_budget` lookup failure → treated as not-near (no downgrade), per existing `_near_limit`.
- Unknown backend in `strengths`/categories → ignored; scorer only ranks `policy.backends`.
- `scoring.enabled: false` → exact legacy behavior (rollback path).

## Testing

Unit (no Docker), `tests/test_scoring.py`:
- `detect_tasks`: word-boundary correctness (`encode` not `code`; `"def "`/`def` detected);
  multi-tag prompts; empty.
- health: failures fill window → `is_circuit_broken` true; mixed → `health_score` between 0..1;
  cold backend → `health_default`.
- scorer: clear code prompt → codex; near_budget → cheapest (minimax); healthy-but-pricey vs
  cheap-but-unhealthy trade-off resolves sensibly; circuit-broken backend avoided; ranked order
  deterministic; all-broken still returns a backend.
- Keep `tests/test_routing.py` green by routing its prompts through the scored path (or assert the
  scored decision matches the documented expectations; update reasons as needed).

## Decision notes

- Health in-memory (not DB-queried per request) → zero added latency on the hot path; resets on
  restart (cold = neutral, self-heals within `health_window` calls).
- Weights/knobs live in `routing.yaml` so tuning needs no code change/redeploy — only a router
  restart. `scoring.enabled` is the rollback switch to the legacy keyword router.
