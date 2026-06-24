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
