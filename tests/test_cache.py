"""Unit tests for the response cache — key, cacheability, store. No Docker, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
from router import (  # noqa: E402
    cache_key, is_cacheable, cache_get, cache_set, _cache,
    _sse_deltas, _synth_completion, _completion_content,
)

POLICY = {"cache": {"enabled": True, "ttl_seconds": 100, "max_temperature": 0.5, "max_entries": 3}}


def setup_function(_):
    _cache.clear()


# ---- cache_key ----

def test_key_stable_and_ignores_irrelevant_fields():
    a = {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "temperature": 0,
         "stream": True, "user": "x"}
    b = {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "temperature": 0,
         "stream": False, "user": "y"}
    assert cache_key(a) == cache_key(b)            # stream/user don't affect the key


def test_key_sensitive_to_prompt_and_params():
    base = {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    assert cache_key(base) != cache_key({**base, "messages": [{"role": "user", "content": "yo"}]})
    assert cache_key(base) != cache_key({**base, "model": "codex"})
    assert cache_key(base) != cache_key({**base, "temperature": 0.4})


# ---- is_cacheable ----

def test_low_temp_cacheable_high_not():
    assert is_cacheable({"temperature": 0.2}, {}, POLICY) is True
    assert is_cacheable({"temperature": 0.9}, {}, POLICY) is False


def test_absent_temp_not_cacheable():
    assert is_cacheable({"messages": []}, {}, POLICY) is False


def test_bypass_and_force():
    assert is_cacheable({"temperature": 0, "cache": False}, {}, POLICY) is False
    assert is_cacheable({"temperature": 0.9, "cache": True}, {}, POLICY) is True   # force overrides temp
    assert is_cacheable({"temperature": 0}, {"X-Axonate-No-Cache": "1"}, POLICY) is False


def test_disabled_never_cacheable():
    off = {"cache": {"enabled": False, "max_temperature": 0.5}}
    assert is_cacheable({"temperature": 0, "cache": True}, {}, off) is False


# ---- store ----

def test_set_get_hit_and_expiry():
    cache_set("k", {"v": 1}, now=0.0, ttl=10, max_entries=3)
    assert cache_get("k", now=5.0) == {"v": 1}      # within TTL
    assert cache_get("k", now=11.0) is None         # expired -> dropped
    assert "k" not in _cache


def test_eviction_past_max_entries():
    cache_set("a", {}, now=0.0, ttl=100, max_entries=3)   # expires @100
    cache_set("b", {}, now=0.0, ttl=200, max_entries=3)   # @200
    cache_set("c", {}, now=0.0, ttl=300, max_entries=3)   # @300
    cache_set("d", {}, now=0.0, ttl=400, max_entries=3)   # @400 -> over cap, evict soonest (a)
    assert "a" not in _cache
    assert set(_cache.keys()) == {"b", "c", "d"}


# ---- stream capture helpers ----

def test_sse_deltas_extracts_content():
    chunk = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
             b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
             b'data: [DONE]\n\n')
    assert _sse_deltas(chunk) == "Hello"


def test_synth_roundtrip():
    comp = _synth_completion("Hello world", "claude")
    assert _completion_content(comp) == "Hello world"
    assert comp["choices"][0]["message"]["role"] == "assistant"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            _cache.clear()
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
