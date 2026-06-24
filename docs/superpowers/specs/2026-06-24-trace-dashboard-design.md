# Trace Dashboard — design

**Date:** 2026-06-24
**Status:** approved (brainstorm)
**Track:** Permanent (router observability)

## Goal

Replace the bare HTML table at the router's `/trace/view` with a single, self-contained
dashboard that surfaces usage analytics, live monitoring, and per-call audit/debug on one page.
The litellm UI is cost-centric and reads empty for the subscription path (CLI adapter reports
`$0`), so this dashboard measures **volume, latency, and status** — not cost.

## Scope

In: two router endpoints — a new `GET /trace/stats` (JSON aggregates) and a rewritten
`GET /trace/view` (the HTML+JS page). Charts via Chart.js loaded from a CDN, with graceful
fallback when the CDN is blocked. No new services, no new Python dependencies, no build step.

Out: changes to request routing, the `axonate_trace` schema, the litellm UI, or any client.
No prompt/response content is exposed (privacy default stays off).

## Constraints (carried from CLAUDE.md)

- Read-only. Localhost / Cloudflare-tunnel only — no new open ports.
- Auth: admin (master key) sees all rows; a non-admin identity sees only its own. Reuse the
  existing `_is_admin(request)` and `auth_shim.resolve_identity(headers)` logic already used by
  `/trace`.
- Privacy: never render prompt or completion text — only the existing trace columns.
- OpenAI-compatible boundaries untouched; this is additive observability.
- Offline/airgap caveat: Chart.js is a runtime CDN fetch. The page MUST stay fully functional
  (cards + tables) when that fetch fails; only the two charts degrade to a short "charts
  unavailable offline" note.

## Data source

The existing `axonate_trace` table, columns:
`ts, user_email, model_requested, route, reason, latency_ms, total_tokens, cost, status`.
No migration. `status` is the backend HTTP status (200, 401, 429, 5xx…). `total_tokens` and
`cost` are often `0`/`null` on the subscription path — the UI treats them as best-effort.

## Component 1 — `GET /trace/stats`

**Signature:** `GET /trace/stats?window=1h|24h|7d|all` (default `24h`). Returns JSON. Same
auth/visibility rules as `/trace` (admin → all rows; else `WHERE user_email=$user`). Window
filters on `ts >= now() - interval`.

**Response shape:**
```json
{
  "window": "24h",
  "generated_at": "2026-06-24T20:30:00Z",
  "totals": {
    "requests": 0,
    "errors": 0,            // status >= 400
    "error_rate": 0.0,      // errors / requests, 0 when requests==0
    "avg_latency_ms": 0,
    "p95_latency_ms": 0,
    "total_tokens": 0,
    "models": 0             // distinct route values seen
  },
  "by_model": [             // ordered by count desc
    {"model": "claude", "count": 0, "success_rate": 0.0, "avg_latency_ms": 0}
  ],
  "by_status_class": [      // 2xx / 4xx / 5xx buckets
    {"class": "2xx", "count": 0}
  ],
  "series": [               // time buckets for the line chart
    {"bucket": "2026-06-24T19:00:00Z", "ok": 0, "err": 0}
  ]
}
```

**Aggregation:** the endpoint fetches the windowed rows from `axonate_trace`
(`WHERE ts >= now() - interval [AND user_email=$user]`) and passes them to a pure
`_stats_from_rows(rows, window)` helper that computes everything in Python — totals, `p95`
(nearest-rank over sorted `latency_ms`), per-model `success_rate` (`count(status<400)/count(*)`)
and avg latency, status-class buckets, and the time `series`. Bucket width derives from the
window (1h→5-min, 24h→1-hour, 7d→6-hour, all→1-day). Empty result sets return zeros, never error.
Keeping aggregation in Python (not SQL) makes it unit-testable without a database; per-window row
volume is bounded.

## Component 2 — `GET /trace/view`

A single `HTMLResponse` (self-contained string in `router.py`, same as today). Structure:

1. **Header** — title, window selector (`1h / 24h / 7d / all`), auto-refresh toggle (off by
   default; 10s when on), "admin: all users" vs "you: own" badge derived from the stats call.
2. **Summary cards** — Requests · Error rate% · Avg latency · p95 latency · Total tokens ·
   Models, populated from `totals`.
3. **Charts** (Chart.js from CDN) — (a) line: requests over time, `ok` vs `err` from `series`;
   (b) donut: requests by model from `by_model`. If `window.Chart` is undefined after load
   (CDN blocked), replace the chart area with a one-line notice and keep everything else.
4. **Per-model table** — model · count · success% · avg ms, from `by_model`.
5. **Recent calls table** — fetched from `/trace?limit=100`: time, user, requested, route,
   reason, ms, tokens, status. Status cell color-coded (2xx green, 4xx amber, 5xx/▢ red).
   Client-side filters: model dropdown, status-class dropdown, free-text search; all filtering
   is in-browser over the fetched rows (no new server params).

**Client logic:** vanilla JS. On load and on window/refresh change: `fetch('/trace/stats?window=…')`
and `fetch('/trace?limit=100')` in parallel, render. Requests include credentials so the
Cloudflare/identity headers flow (same-origin). All numbers HTML-escaped before injection.

## Error handling

- `_pool is None` (trace DB down): `/trace/stats` returns `503` like `/trace`; the page shows a
  "trace DB unavailable" banner instead of cards.
- Non-admin without resolvable identity → `401` (mirrors `/trace`).
- CDN/Chart.js failure → charts area shows a notice; cards/tables unaffected.
- Division-by-zero guarded (error_rate/success_rate = 0 when denominator 0).

## Testing

- Unit (no Docker), extend `tests/`: a `_stats_from_rows(rows, window)` pure helper that the
  endpoint wraps, tested for: empty → all-zero; mixed statuses → correct error_rate and
  status-class buckets; per-model success_rate + avg latency; p95 on a known list; bucket
  assignment for each window. Keep SQL-independent by computing aggregates in the helper from
  fetched rows (the endpoint fetches the windowed rows, the helper aggregates) — simpler to test
  than asserting SQL, and the row volume per window is bounded.
- Manual/E2E: with the live poc stack, `GET /trace/stats` returns non-zero `by_model` for
  claude/codex; `/trace/view` renders cards, charts (and the offline fallback when Chart.js is
  blocked), and the color-coded filterable table.

## Decision note

Brainstorm chose Chart.js via CDN over zero-dependency inline charts. Accepted with the graceful
fallback above; the offline/airgap limitation is documented here and in the page's fallback
notice. Aggregation moved to a pure `_stats_from_rows` helper (fetch windowed rows → aggregate in
Python) rather than heavy SQL, to keep it unit-testable without a database.
