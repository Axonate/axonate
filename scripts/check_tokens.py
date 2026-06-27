#!/usr/bin/env python3
"""Probe the subscription-backed models (claude/codex) through LiteLLM and alert on expiry.

The recurring risk for a subscription-backed lab is OAuth **token expiry** (not restart): the
shared claude/codex login times out and every dependent user breaks. Run this on eva via cron
(e.g. every 30 min); it pings each subscription model and posts to SLACK_WEBHOOK_URL when one
stops answering, so you re-login before people notice. Stdlib only — no venv needed.

Env:
  LITELLM_URL         default http://127.0.0.1:4000
  LITELLM_MASTER_KEY  master key (internal probe)
  SLACK_WEBHOOK_URL   where to alert (no alert if unset)
  CHECK_MODELS        comma list, default 'claude,codex'
Exit 0 = all healthy, 1 = at least one model failed.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def evaluate(results):
    """results: list of {'model','ok','detail'}. Returns (all_ok: bool, slack_message: str)."""
    bad = [r for r in results if not r["ok"]]
    if not bad:
        return True, ""
    lines = ["⚠️ Axonate: subscription model(s) not answering — likely token expiry. "
             "Re-login on eva."]
    for r in bad:
        lines.append(f"• {r['model']}: {r['detail']}")
    lines.append("Fix on eva: `docker compose --profile lab exec axonate-adapter claude "
                 "setup-token` then `make set-claude-token` (or `make login-codex`).")
    return False, "\n".join(lines)


def probe(url, key, model):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 8}).encode()
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return {"model": model, "ok": r.status == 200, "detail": f"HTTP {r.status}"}
    except urllib.error.HTTPError as e:
        return {"model": model, "ok": False, "detail": f"HTTP {e.code}: {e.read().decode()[:140]}"}
    except Exception as e:  # noqa: BLE001 — any failure means the model isn't answering
        return {"model": model, "ok": False, "detail": str(e)[:140]}


def post_slack(webhook, text):
    if not webhook:
        return
    req = urllib.request.Request(webhook, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"slack post failed: {e}", file=sys.stderr)


def main():
    url = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    models = [m.strip() for m in os.environ.get("CHECK_MODELS", "claude,codex").split(",") if m.strip()]
    results = [probe(url, key, m) for m in models]
    ok, msg = evaluate(results)
    for r in results:
        print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['model']}: {r['detail']}")
    if not ok:
        post_slack(os.environ.get("SLACK_WEBHOOK_URL", ""), msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
