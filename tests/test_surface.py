"""Unit tests for router surface resolution + admin-email check. No Docker."""
import os, sys
os.environ.setdefault("API_HOST", "api.clouddrove.in")
os.environ.setdefault("APP_HOST", "app.clouddrove.in")
os.environ.setdefault("ADMIN_EMAILS", "boss@clouddrove.com, Admin@Clouddrove.com")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "router"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "auth"))
from router import _surface, _is_admin_email  # noqa: E402


class _Req:
    def __init__(self, host): self.headers = {"host": host}


def test_surface_api():
    assert _surface(_Req("api.clouddrove.in")) == "api"
    assert _surface(_Req("api.clouddrove.in:443")) == "api"     # port stripped
    assert _surface(_Req("API.clouddrove.in")) == "api"         # case-insensitive

def test_surface_app():
    assert _surface(_Req("app.clouddrove.in")) == "app"

def test_surface_other():
    assert _surface(_Req("admin.clouddrove.in")) == "other"
    assert _surface(_Req("127.0.0.1:4100")) == "other"
    assert _surface(_Req("")) == "other"

def test_admin_email():
    assert _is_admin_email("boss@clouddrove.com") is True
    assert _is_admin_email("ADMIN@clouddrove.com") is True       # case-insensitive
    assert _is_admin_email("user@clouddrove.com") is False


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try: fn(); print(f"PASS {name}")
            except AssertionError as e: failed += 1; print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
