"""Unit tests for ax pure helpers: slash parsing, code/file extraction, headers. No network."""
import os
import sys
from importlib.machinery import SourceFileLoader

ax = SourceFileLoader("ax", os.path.join(os.path.dirname(__file__), "..", "clients", "ax")).load_module()


def test_parse_slash_commands():
    assert ax.parse_slash("/model codex") == ("model", "codex")
    assert ax.parse_slash("/save out.py") == ("save", "out.py")
    assert ax.parse_slash("/exit") == ("exit", "")
    assert ax.parse_slash("/help") == ("help", "")


def test_parse_slash_plain_text():
    assert ax.parse_slash("write a regex") == (None, "write a regex")
    assert ax.parse_slash("  hi  ") == (None, "hi")


def test_extract_first_code():
    r = "blah\n```python\nprint(1)\n```\nmore"
    assert ax.extract_first_code(r) == "print(1)\n"
    assert ax.extract_first_code("no code here") == "no code here"


def test_file_markers():
    reply = "intro\n<<<FILE a/b.py>>>\nX=1\n<<<END>>>\nend"
    m = ax._FILE_RE.findall(reply)
    assert m == [("a/b.py", "X=1")]


def test_build_headers_ua_and_cf():
    h = ax.build_headers("sk-x", "cid", "sec")
    assert h["User-Agent"] == "axonate-ax/1.0"
    assert h["Authorization"] == "Bearer sk-x"
    assert h["CF-Access-Client-Id"] == "cid" and h["CF-Access-Client-Secret"] == "sec"
    assert "CF-Access-Client-Id" not in ax.build_headers("sk-x")   # cf needs both


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
