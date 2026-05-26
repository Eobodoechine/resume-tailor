"""
Shared pytest fixtures and import stubs.

Heavy dependencies (supabase, anthropic, weasyprint, httpx, jinja2) are
stubbed so route modules can be imported without the full dependency graph.
Pure-logic functions (_parse_experience, _strip_html, etc.) are tested directly.
Network/DB calls are mocked per-test with unittest.mock.patch.
"""
import sys
from unittest.mock import MagicMock
import pytest

# ── Stub heavy third-party imports (not installed in test env) ────────────────
# We stub only true external libs — NOT our own services — so that
# internal pure-logic functions (e.g. _parse_experience) remain callable.

_EXTERNAL_STUBS = {
    # httpx is a real installed package — do NOT stub it.  TestClient (starlette)
    # imports from httpx internally; a MagicMock stub causes a metaclass conflict
    # that makes the entire test_http_layer.py collection fail.
    "supabase":                    MagicMock(),
    "anthropic":                   MagicMock(),
    "weasyprint":                  MagicMock(),
    "pdfplumber":                  MagicMock(),
    "docx":                        MagicMock(),
    "jinja2":                      MagicMock(),
    "slowapi":                     MagicMock(),
    "slowapi.util":                MagicMock(),
    "slowapi.errors":              MagicMock(),
    "sentry_sdk":                  MagicMock(),
    "sentry_sdk.integrations":     MagicMock(),
    "sentry_sdk.integrations.fastapi":    MagicMock(),
    "sentry_sdk.integrations.starlette": MagicMock(),
}
for _mod, _stub in _EXTERNAL_STUBS.items():
    sys.modules.setdefault(_mod, _stub)

# Rate limiter stub — @limiter.limit("x/min") must be a no-op decorator
_limiter_stub = MagicMock()
_limiter_stub.limit = lambda *a, **kw: (lambda f: f)
_limiter_module = MagicMock()
_limiter_module.limiter = _limiter_stub
sys.modules["limiter"] = _limiter_module

# App config stub — list every attribute explicitly. Relying on MagicMock's
# auto-attr fallback would let real bugs slip past (e.g. a route reading
# COOKIE_SECURE as a MagicMock and silently treating it as truthy).
sys.modules.setdefault("config", MagicMock(
    SUPABASE_URL="http://fake",
    SUPABASE_ANON_KEY="fake-anon",
    SUPABASE_SERVICE_KEY="fake-service",
    ANTHROPIC_API_KEY="fake-key",
    CLAUDE_MODEL="claude-haiku-4-5-20251001",
    RESUME_BUCKET="resume-files",
    PDF_BUCKET="tailored-pdfs",
    ADMIN_EMAIL="admin@example.com",
    COOKIE_SECURE=False,
    COOKIE_MAX_AGE=60 * 60 * 24 * 7,
))

# Stub services that talk to external systems — NOT pdf_generator (pure logic)
sys.modules.setdefault("services.supabase_client", MagicMock())
sys.modules.setdefault("services.extractor", MagicMock())
sys.modules.setdefault("services.claude", MagicMock())


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_user():
    """A fake Supabase user object."""
    user = MagicMock()
    user.id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    user.email = "test@example.com"
    return user


@pytest.fixture
def valid_token():
    return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.valid.token"


@pytest.fixture
def auth_header(valid_token):
    return f"Bearer {valid_token}"
