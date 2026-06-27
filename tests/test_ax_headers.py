"""Unit tests for ax's build_headers (CF service-token injection). No network."""
import os
import sys
from importlib.machinery import SourceFileLoader

_AX = os.path.join(os.path.dirname(__file__), "..", "clients", "ax")
ax = SourceFileLoader("ax", _AX).load_module()


def test_no_key_no_cf():
    h = ax.build_headers("")
    assert h == {"Content-Type": "application/json"}


def test_key_only():
    h = ax.build_headers("sk-abc")
    assert h["Authorization"] == "Bearer sk-abc"
    assert "CF-Access-Client-Id" not in h


def test_key_plus_cf_pair():
    h = ax.build_headers("sk-abc", "cid.access", "secret123")
    assert h["Authorization"] == "Bearer sk-abc"
    assert h["CF-Access-Client-Id"] == "cid.access"
    assert h["CF-Access-Client-Secret"] == "secret123"


def test_cf_requires_both():
    # only id, no secret -> no CF headers (avoid sending a half pair)
    assert "CF-Access-Client-Id" not in ax.build_headers("sk-abc", "cid.access", "")
    assert "CF-Access-Client-Id" not in ax.build_headers("sk-abc", "", "secret123")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
