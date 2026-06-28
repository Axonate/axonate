# Anthropic /v1/messages translation — design

**Date:** 2026-06-28
**Status:** approved (brainstorm)
**Track:** Permanent (router — Claude Code support)

## Goal

Make the router's `/v1/messages` endpoint work by **translating** Anthropic Messages API ↔ OpenAI
chat-completions in the router, instead of forwarding to LiteLLM's `/v1/messages` (which 404s on the
pinned LiteLLM 1.89.3). This lets **Claude Code** and other Anthropic-shaped clients use the gateway,
version-independent of LiteLLM. Scope: **text + streaming** (the Anthropic SSE event protocol);
tool-use / image blocks are out of scope (the subscription `claude` CLI backend can't honor them
anyway — that's an API-key-backend future).

## Non-goals

- `tool_use` / `tool_result` / image content blocks (text only; documented limitation).
- Anthropic-only features (extended thinking, prompt caching headers, batch).
- Changing the existing OpenAI `/v1/chat/completions` behavior (it stays the source of truth; the
  shared core is extracted from it without behavior change).

## Components

### 1. `anthropic_to_openai(body) -> dict` (pure)
Anthropic Messages request → OpenAI chat request:
- Top-level `system` (string, or list of `{"type":"text","text":...}` blocks) → prepend one
  `{"role":"system","content":<joined text>}` message.
- Each `messages[i]`: `role` (`user`/`assistant`) preserved; `content` is a string → used as-is, or
  a list of blocks → join the `text` of `type=="text"` blocks (non-text blocks dropped).
- Field map: `max_tokens`→`max_tokens`, `stop_sequences`→`stop`, pass through `temperature`,
  `top_p`, `stream`, `model`. Drop `tools`, `tool_choice`, `metadata`, `system` (already folded).

### 2. `openai_to_anthropic(resp, model) -> dict` (pure)
OpenAI chat completion → Anthropic message response:
```json
{ "id": "msg_<id>", "type": "message", "role": "assistant", "model": <model>,
  "content": [{"type": "text", "text": <choices[0].message.content>}],
  "stop_reason": <mapped>, "stop_sequence": null,
  "usage": {"input_tokens": <prompt_tokens>, "output_tokens": <completion_tokens>} }
```
`stop_reason` map: `stop`→`end_turn`, `length`→`max_tokens`, `content_filter`→`end_turn`, default
`end_turn`. Missing/empty content → `""`.

### 3. `openai_sse_to_anthropic_events(deltas) -> iterator[bytes]` (pure generator)
Given the sequence of OpenAI content deltas (strings) for one response, yield the Anthropic SSE
**event frames** Claude Code expects, each as `event: <name>\ndata: <json>\n\n`:
1. `message_start` — `{"type":"message_start","message":{"id":"msg_…","type":"message","role":"assistant","model":<model>,"content":[],"stop_reason":null,"usage":{"input_tokens":0,"output_tokens":0}}}`
2. `content_block_start` — `{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`
3. per non-empty delta: `content_block_delta` — `{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":<delta>}}`
4. `content_block_stop` — `{"type":"content_block_stop","index":0}`
5. `message_delta` — `{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":<n>}}`
6. `message_stop` — `{"type":"message_stop"}`

The pure function takes the model + an iterable of delta strings and returns the ordered frames; the
handler feeds it from the live OpenAI stream (reusing `_sse_deltas` to extract deltas per chunk).

### 4. Shared completion core (small refactor of `chat`)
Extract `async def _run_completion(oai_body, key, user, headers, stream) -> tuple`:
- Contains the existing **cache lookup**, **routing** (`_decide_scored`/`_decide`), **fallback
  forward loop**, **health recording**, and **trace** — exactly today's `chat` logic.
- Returns `("hit_json", dict)` | `("json", dict, route, status)` | `("stream", response_or_iter, route)`
  so the caller formats the wire response.
- `chat` becomes a thin wrapper: parse body → `_run_completion(..., stream)` → format as OpenAI
  (current behavior, unchanged — guarded by existing tests + live re-verify).

### 5. Handler — `messages_passthrough` (rewrite)
```
identity (surface-aware) + rate-limit            # existing
oai = anthropic_to_openai(body)
kind, payload, ... = await _run_completion(oai, key, user, request.headers, stream)
non-stream -> return JSONResponse(openai_to_anthropic(payload, body["model"]))
stream     -> StreamingResponse over openai_sse_to_anthropic_events(model, live OpenAI deltas)
```
`model: "auto"` still scores; explicit model passes through (set Claude Code's model to an Axonate
name: `claude`/`codex`). The cache key is computed on the translated OpenAI body, so `/v1/messages`
and `/v1/chat/completions` share cache entries for equivalent requests.

## Data flow

```
Claude Code -> /v1/messages (Anthropic) -> anthropic_to_openai
            -> _run_completion (cache / score / forward / health / trace)  [OpenAI shape to LiteLLM]
            -> non-stream: openai_to_anthropic -> Anthropic JSON
            -> stream: OpenAI SSE deltas -> openai_sse_to_anthropic_events -> Anthropic SSE
```

## Error handling / edge cases

- Non-text content blocks → dropped with the text kept (a fully non-text message → empty content).
- Upstream error / non-2xx → surface an Anthropic-shaped error object `{"type":"error","error":{...}}`
  with the upstream status.
- Missing `max_tokens` (Anthropic requires it; OpenAI optional) → pass through if present, else omit.
- Streaming upstream failure mid-stream → the stream ends; best-effort (same as the OpenAI path).
- `_run_completion` refactor must preserve exact OpenAI `/v1/chat/completions` behavior — covered by
  existing tests + live re-verify before merge.

## Testing

Unit (`tests/test_anthropic.py`, no Docker):
- `anthropic_to_openai`: top-level `system` string + block-list → system message; content string vs
  text-block list → flattened; `stop_sequences`→`stop`; `tools` dropped; role preserved.
- `openai_to_anthropic`: content + usage mapping; `finish_reason` → `stop_reason` cases; empty content.
- `openai_sse_to_anthropic_events`: a delta sequence `["He","llo"]` → exact ordered frames
  (`message_start` … `content_block_delta`×2 … `message_stop`), each a valid `event:`/`data:` frame.

Live (eva): `/v1/messages` non-stream → valid Anthropic JSON (`content[0].text` has the reply);
streaming → a well-formed Anthropic event stream; then point Claude Code (`ANTHROPIC_BASE_URL=
https://api.clouddrove.in`, `ANTHROPIC_AUTH_TOKEN=<key>`, CF service-token headers) at it and confirm
a chat turn works.

## Decision notes

- Router-side translation (not LiteLLM's endpoint) → version-independent; works on the pinned
  LiteLLM and survives upgrades.
- Text-only → the realistic scope for a subscription backend; full tool-use is gated on a real
  Anthropic API key backend and is a separate future effort.
- The `_run_completion` extraction is the one risk (touches the working chat path); it's a pure
  move of existing logic, guarded by the current chat tests + a live re-verify of `/v1/chat/completions`.
