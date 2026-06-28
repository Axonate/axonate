"""Unit tests for the scoring router — task detection, health, scorer. No Docker, no network."""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
import router  # noqa: E402
from router import (  # noqa: E402
    detect_tasks, health_score, is_circuit_broken, score_backends, _health_record, _health,
)

POLICY = yaml.safe_load(open(os.path.join(os.path.dirname(__file__),
                                          "..", "services", "router", "routing.yaml")))


def setup_function(_):
    _health.clear()


# ---- task detection (word boundaries) ----

def test_detect_code_word_boundary():
    assert "code" in detect_tasks("please refactor this function and fix the bug", POLICY)
    # 'encode' must NOT trigger the 'code' tag
    assert "code" not in detect_tasks("encode this string to base64", POLICY)


def test_detect_reason_and_summarize():
    assert "reason" in detect_tasks("analyze the architecture trade-offs", POLICY)
    assert "summarize" in detect_tasks("summarize this document into bullets", POLICY)


def test_detect_empty():
    assert detect_tasks("hello there", POLICY) == set()


# ---- health + circuit breaker ----

def test_cold_backend_neutral_health():
    assert health_score("claude", POLICY) == POLICY["scoring"]["health_default"]
    assert is_circuit_broken("claude", POLICY) is False


def test_failures_trip_circuit_breaker():
    for _ in range(POLICY["scoring"]["circuit_break_failures"]):
        _health_record("claude", False, 500, POLICY["scoring"]["health_window"])
    assert is_circuit_broken("claude", POLICY) is True
    assert health_score("claude", POLICY) < 0.5   # all-fail -> low


def test_healthy_backend_high_score():
    for _ in range(5):
        _health_record("codex", True, 200, POLICY["scoring"]["health_window"])
    assert is_circuit_broken("codex", POLICY) is False
    assert health_score("codex", POLICY) > 0.7     # all-ok + fast -> high


# ---- scorer ----

def test_code_task_routes_codex():
    tasks = detect_tasks("refactor this function and fix the bug", POLICY)
    best, reason, ranked = score_backends(tasks, False, {}, POLICY)
    assert best == "codex"
    assert ranked[0] == "codex"


def test_near_budget_downgrades_to_cheapest():
    # a reasoning prompt would normally favor claude; near budget -> cheapest (minimax)
    tasks = detect_tasks("analyze the design trade-offs in depth", POLICY)
    best, reason, ranked = score_backends(tasks, True, {}, POLICY)
    assert best == "minimax"


def test_circuit_broken_backend_avoided():
    tasks = detect_tasks("refactor this function", POLICY)   # would pick codex
    for _ in range(POLICY["scoring"]["circuit_break_failures"]):
        _health_record("codex", False, 800, POLICY["scoring"]["health_window"])
    best, reason, ranked = score_backends(tasks, False,
                                          {b: health_score(b, POLICY) for b in POLICY["backends"]},
                                          POLICY)
    assert best != "codex"             # codex circuit-broken -> avoided
    assert ranked[-1] == "codex"       # broken backend ranked last


def test_ranked_is_full_and_deterministic():
    tasks = detect_tasks("hello", POLICY)
    best, reason, ranked = score_backends(tasks, False, {}, POLICY)
    assert set(ranked) == set(POLICY["backends"].keys())
    # cold + no task -> cheapest wins (minimax)
    assert best == "minimax"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            _health.clear()
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
