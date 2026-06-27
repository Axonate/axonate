"""Unit tests for the token-expiry alert evaluation. No network."""
import os
import sys
from importlib.machinery import SourceFileLoader

_S = os.path.join(os.path.dirname(__file__), "..", "scripts", "check_tokens.py")
ct = SourceFileLoader("check_tokens", _S).load_module()


def test_all_ok_no_alert():
    ok, msg = ct.evaluate([{"model": "claude", "ok": True, "detail": "HTTP 200"},
                           {"model": "codex", "ok": True, "detail": "HTTP 200"}])
    assert ok is True
    assert msg == ""


def test_one_failure_alerts():
    ok, msg = ct.evaluate([{"model": "claude", "ok": False, "detail": "HTTP 500: not logged in"},
                           {"model": "codex", "ok": True, "detail": "HTTP 200"}])
    assert ok is False
    assert "claude" in msg
    assert "token expiry" in msg
    assert "codex" not in msg.split("\n")[1]   # only the failing model is listed


def test_empty_is_ok():
    ok, msg = ct.evaluate([])
    assert ok is True and msg == ""


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
