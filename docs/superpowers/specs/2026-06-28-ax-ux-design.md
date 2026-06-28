# ax UX makeover — design

**Date:** 2026-06-28
**Status:** approved (brainstorm)
**Track:** Permanent (clients/ax)

## Goal

Make `ax` feel like a friendly assistant, not a flag-heavy CLI. Three changes: an **interactive
REPL** (default when you just run `ax`), a **`ax setup` wizard** (no hand-editing config), and
**cleaner output / simpler help**. Keep every existing capability (one-shot, sessions, `--save`,
model switch, models/status/usage) working — just make the common path "open ax and talk."

## Components

### 1. Interactive REPL (default for `ax` with no prompt on a TTY)
- `ax` (no args, stdin is a TTY) → opens the REPL instead of printing a static banner. A short
  intro line (gateway, model, "/help for commands") then a prompt: `axonate (claude) › `.
- Each entered line is sent to the gateway with **in-memory conversation history** (multi-turn);
  the reply streams back. Empty line is ignored. `Ctrl-D`/`Ctrl-C` exits cleanly.
- **REPL is conversational** — it does NOT use the one-shot "never ask clarifying questions"
  system prompt, because in a REPL the user *can* answer. It uses a light system prompt
  (`AXONATE_SYSTEM_REPL`, default: "You are a helpful assistant in an interactive terminal chat.
  Be concise.").
- **Slash commands** (handled locally, not sent to the model):
  - `/model [name]` — show or switch the session model (validated against `/v1/models`)
  - `/save [path]` — write the **last reply's** `<<<FILE>>>` blocks; if the last reply had none,
    write its first fenced code block to `path` (or a suggested name)
  - `/usage` — print `GET /v1/usage`
  - `/clear` — reset the in-memory conversation
  - `/help` — list commands
  - `/exit` / `/quit` — leave
- Optional `-s NAME` makes the REPL persist to that session file (load on start, save each turn).

### 2. `ax setup` wizard
- `ax setup` → interactive prompts (with defaults shown), writing `~/.config/axonate/config`
  (chmod 600): gateway URL (default `https://api.clouddrove.in`), API key, CF service-token id,
  CF service-token secret, default model. Secrets read without echo where practical.
- After writing, runs the `status` reachability check and prints ✓/✗ so a bad value surfaces
  immediately. Non-destructive: shows existing values as defaults; Enter keeps them.

### 3. Cleaner output + simpler help
- Drop the pre-answer `— model —` line (redundant with the footer). Keep one subtle footer
  `[claude · 1.3s]` (dim, stderr) — and add `--quiet`/`-q` to suppress even that.
- `ax` help: a short, grouped help. The advanced flags (`--raw`, `--system`, `--force`,
  `--no-stream`) move under an "advanced" group in `--help`; the banner/intro shows only the
  common path + commands.
- Commands list (`switch`, `models`, `status`, `usage`, `setup`) shown in `/help` and `ax --help`.

## Behavior matrix (what runs)

| Invocation | Behavior |
|---|---|
| `ax` (TTY, no prompt) | **REPL** |
| `ax` (piped stdin, no prompt) | read stdin as the prompt (one-shot) |
| `ax "text"` | one-shot (direct system prompt — no clarifying questions) |
| `ax setup` | wizard |
| `ax models` / `status` / `usage` | as today |
| `ax switch MODEL` | persist default model (as today) |
| `ax --save "..."` | one-shot, write `<<<FILE>>>` files (as today) |

## Data flow

REPL keeps a `messages` list in memory; each turn appends `{user}` then the streamed `{assistant}`.
Slash commands mutate local state (model, history) or call read endpoints (`/v1/usage`,
`/v1/models`) — never the chat endpoint. One-shot is unchanged (single request).

## Error handling / edge cases

- REPL: a failed request (401/403/524/network) prints the error + hint and returns to the prompt
  (doesn't crash the session).
- `Ctrl-C` mid-stream cancels the current answer, stays in the REPL; a second `Ctrl-C` at an empty
  prompt exits.
- `/save` with no prior reply, or a reply lacking files/code → a clear "nothing to save" note.
- `ax setup` with no TTY (piped) → error telling the user to run it interactively.
- Non-TTY `ax` with no prompt and no pipe → print short help (not a hung REPL).

## Testing

Unit (`tests/test_ax_repl.py`, no network): pure helpers only —
- slash-command parser: `/model codex` → `("model", "codex")`; `/save x.py` → `("save", "x.py")`;
  plain text → `(None, text)`; `/help`, `/exit` recognized.
- `save_files` already covered; add: extract first fenced ```code block``` for `/save path`.
- the REPL loop + wizard are I/O-bound → manual/live verification, not unit-tested.

Live (local): `ax` opens the REPL, a turn answers, `/model codex` switches, `/save` writes a file,
`/exit` leaves; `ax setup` writes a working config; `ax "one-shot"` and `ax --save` unchanged.

## Decision notes

- REPL conversational (allows follow-up questions); one-shot stays "answer directly." Two system
  prompts for two modes — matches how each is actually used.
- Keep `ax` a single stdlib script (no deps) — REPL via `input()`, streaming via the existing
  urllib path; `Ctrl-C` handling via `KeyboardInterrupt`.
- All current flags/commands preserved — this is additive UX, not a breaking redesign.
