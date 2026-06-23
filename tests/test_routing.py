"""Unit tests for the router decision logic — no Docker, no network.
Run: python3 -m pytest tests/ -q   (or: python3 tests/test_routing.py)
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
from router import _decide, _fallback_order  # noqa: E402

POLICY = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "services", "router", "routing.yaml")))


def test_code_prompt_routes_to_codex():
    route, _ = _decide("please refactor this function and fix the bug in the loop", POLICY)
    assert route == "codex"


def test_bulk_prompt_routes_to_minimax():
    route, _ = _decide("summarize this long document into bullet points for me please", POLICY)
    assert route == "minimax"


def test_reasoning_prompt_routes_to_claude():
    route, _ = _decide("analyze the architecture trade-offs and explain why this design wins", POLICY)
    assert route == "claude"


def test_short_prompt_biases_cheap():
    route, reason = _decide("hi", POLICY)
    assert route == "minimax"  # cheap backend for easy/short


def test_default_when_no_match():
    route, reason = _decide("xyzzy " * 60, POLICY)  # long, no keywords
    assert route == POLICY["default"]


def test_fallback_order_chosen_first_then_by_cost():
    order = _fallback_order("claude", POLICY)
    assert order[0] == "claude"
    assert set(order) == {"claude", "codex", "minimax"}
    # remaining sorted by ascending cost -> minimax (cost 1) before codex (cost 3)
    assert order.index("minimax") < order.index("codex")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
