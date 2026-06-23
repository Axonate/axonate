#!/usr/bin/env python3
"""Axonate daily spend digest -> Slack (clean format).

Replaces LiteLLM's noisy native spend_reports. Pulls total spend + ceiling from LiteLLM and
24h traffic (calls/errors/latency, by model, by user) from the axonate_trace table, then posts
one tidy Slack Block Kit message.

Run: python3 scripts/spend_digest.py   (reads .env; schedule daily via cron — see docs/ALERTS.md)
Stdlib only. Uses `docker compose exec` for psql so no DB driver is needed on the host.
"""
import json
import os
import subprocess
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return env


E = load_env()
LITELLM = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
MASTER = E.get("LITELLM_MASTER_KEY", "")
WEBHOOK = E.get("SLACK_WEBHOOK_URL", "")


def psql(sql):
    """Run a query in the db container, return rows as list[list[str]] (tab-separated)."""
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "axonate-db", "psql", "-U",
         E.get("POSTGRES_USER", "axonate"), "-d", E.get("POSTGRES_DB", "axonate"),
         "-tAF", "\t", "-c", sql],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "") + ":/Applications/Docker.app/Contents/Resources/bin"},
    )
    return [r.split("\t") for r in out.stdout.strip().splitlines() if r]


def litellm_spend():
    try:
        req = urllib.request.Request(f"{LITELLM}/global/spend",
                                     headers={"Authorization": f"Bearer {MASTER}"})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return float(d.get("spend") or 0), d.get("max_budget")
    except Exception:
        return 0.0, None


def main():
    if not WEBHOOK:
        print("SLACK_WEBHOOK_URL not set; nothing to send")
        return

    spend, ceiling = litellm_spend()
    totals = psql("SELECT count(*), count(*) FILTER (WHERE status>=400), "
                  "coalesce(round(avg(latency_ms)),0) FROM axonate_trace WHERE ts > now()-interval '24 hours'")
    calls, errors, avg_ms = (totals[0] if totals else ["0", "0", "0"])
    by_model = psql("SELECT route, count(*) FROM axonate_trace WHERE ts > now()-interval '24 hours' "
                    "GROUP BY route ORDER BY 2 DESC LIMIT 6")
    by_user = psql("SELECT user_email, count(*) FROM axonate_trace WHERE ts > now()-interval '24 hours' "
                   "GROUP BY user_email ORDER BY 2 DESC LIMIT 6")

    pct = f" ({spend / float(ceiling) * 100:.0f}% of ceiling)" if ceiling else ""
    ceil_s = f" / ${float(ceiling):.2f}" if ceiling else ""
    models = "  ".join(f"`{m}` {n}" for m, n in by_model) or "—"
    users = "  ".join(f"{u} {n}" for u, n in by_user) or "—"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📊 Axonate — last 24h"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Spend*\n${spend:.2f}{ceil_s}{pct}"},
            {"type": "mrkdwn", "text": f"*Traffic*\n{calls} calls · {errors} errors · {avg_ms}ms avg"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*By model*\n{models}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*By user*\n{users}"}},
    ]
    payload = json.dumps({"blocks": blocks}).encode()
    req = urllib.request.Request(WEBHOOK, data=payload, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=10)
    print(f"digest sent: {resp.status} (spend=${spend:.2f}, calls={calls})")


if __name__ == "__main__":
    main()
