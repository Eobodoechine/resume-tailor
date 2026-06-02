"""
Shared test configuration and fixtures.

CRITICAL ORDERING — three things must happen BEFORE any app module is imported:

  1. os.environ is populated with required vars so config._require() succeeds.
  2. limiter module is stubbed so @limiter.limit() decorators are no-ops —
     prevents 429s when the same endpoint is hit >N times across tests.
  3. renderers.fde_docx is injected into sys.modules so FDEDocxRenderer never
     tries to open the DOCX template file on disk (class attribute evaluated
     at import time would raise FileNotFoundError in CI / fresh checkout).

Everything else (supabase, anthropic clients) is lazy — no network calls on
import — so we can let those real modules load and patch them per-test.
"""
import os
import sys
from unittest.mock import MagicMock

# ── 1. Required env vars (BEFORE any app import) ─────────────────────────────
# Use `or` assignment instead of setdefault — setdefault is a no-op when the
# key exists as an empty string (e.g. ANTHROPIC_API_KEY= in the shell env),
# which causes config._require() to raise even though conftest "set" the var.
def _ensure_env(key: str, value: str) -> None:
    if not os.environ.get(key):
        os.environ[key] = value

_ensure_env("SUPABASE_URL",          "https://testproject.supabase.co")
_ensure_env("SUPABASE_ANON_KEY",     "test-anon-key-abc123")
_ensure_env("SUPABASE_SERVICE_KEY",  "test-service-key-xyz789")
# The Anthropic SDK accepts any non-empty string as api_key at init time.
# "sk-ant-" prefix matches what the SDK expects if it does a prefix check.
_ensure_env("ANTHROPIC_API_KEY",     "sk-ant-test0000000000000000000000000000000000")
_ensure_env("ADMIN_EMAIL",           "admin@test.com")
_ensure_env("ENV",                   "development")

# ── 2. Stub the rate-limiter before any route imports ────────────────────────
# @limiter.limit("5/minute") etc. would fire for every test call that shares
# the TestClient's loopback "127.0.0.1" key, causing 429s on tests 6+ that
# hit the same endpoint.  Making `limit()` a passthrough decorator disables
# rate-limiting entirely in the test environment.
# Must happen BEFORE any route is imported so the @decorator captures the stub.
_limiter_stub = MagicMock()
_limiter_stub.limit = lambda *a, **kw: (lambda f: f)   # no-op passthrough
_limiter_module = MagicMock()
_limiter_module.limiter = _limiter_stub
sys.modules.setdefault("limiter", _limiter_module)

# ── 3. Stub fde_docx before renderers/registry.py imports it ─────────────────
# FDEDocxRenderer has a class-level attribute:
#   _template_bytes: bytes = open(TEMPLATE_PATH, "rb").read()
# This executes at class-definition time (module import).  Without the stub,
# tests fail on any machine that doesn't have the template at the expected path.
_fde_renderer_cls = MagicMock(name="FDEDocxRenderer")
_fde_renderer_cls.return_value.render.return_value = b"%PDF-1.4 fake-test-pdf"
_fde_mock_mod = MagicMock(name="renderers.fde_docx")
_fde_mock_mod.FDEDocxRenderer = _fde_renderer_cls
sys.modules["renderers.fde_docx"] = _fde_mock_mod

# ── 4. Stub services.claude as a MagicMock before any route imports it ───────
# routes.tailor / routes.master do `from services import claude as claude_service`
# and `ai_client = claude_service.client`, binding the module by name at import.
# Endpoint tests configure it with `claude.tailor_resume.return_value = ...` and
# `routes.tailor.ai_client.messages.create.return_value...`. Without a stub the
# REAL services.claude (which constructs a live anthropic.Anthropic client at
# import) loads, and whether the stub is present becomes import-order dependent —
# the root cause of the order-dependent endpoint-test failures.
# test_claude_service.py and test_pdf_pipeline.py intentionally pop→import-real→
# restore this stub, so this is the documented contract.
_claude_stub = MagicMock(name="services.claude")
_claude_stub.API_TIMEOUT = 60.0
_claude_stub.client = MagicMock(name="anthropic.client")
_claude_stub.async_client = MagicMock(name="anthropic.async_client")
sys.modules["services.claude"] = _claude_stub

# ── Shared constants ──────────────────────────────────────────────────────────
import uuid
import pytest

TEST_USER_ID    = str(uuid.uuid4())
TEST_USER_EMAIL = "user@test.com"
ADMIN_EMAIL_STR = "admin@test.com"   # must match os.environ["ADMIN_EMAIL"] above
FAKE_JWT        = "eyJhbGciOiJIUzI1NiJ9.test.fakesig"
TEST_RECORD_ID  = str(uuid.uuid4())


# ── Helper factories ──────────────────────────────────────────────────────────

def make_user(email: str = TEST_USER_EMAIL, uid: str = None):
    """Build a minimal mock Supabase User object."""
    user       = MagicMock()
    user.id    = uuid.UUID(uid) if uid else uuid.UUID(TEST_USER_ID)
    user.email = email
    return user


def db_result(data=None, count: int = None):
    """Create a Supabase-style query result with .data and .count."""
    r        = MagicMock()
    r.data   = data if data is not None else []
    r.count  = count
    return r


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_user():
    return make_user()


@pytest.fixture()
def admin_user():
    return make_user(email=ADMIN_EMAIL_STR)


@pytest.fixture()
def authed_client(mock_user):
    """
    TestClient with require_user overridden so no real token verification occurs.

    Each test must patch the specific Supabase calls it exercises, e.g.:
        import routes.admin
        monkeypatch.setattr(routes.admin, "get_admin_client", lambda: admin_mock)

    Patching at the call site (the route module) rather than in
    services.supabase_client is necessary because routes import get_admin_client
    by name at load time — patching the source module after that has no effect.
    """
    from fastapi.testclient import TestClient
    from main import app
    from dependencies.auth import require_user, AuthContext

    ctx = AuthContext(user=mock_user, token=FAKE_JWT)
    app.dependency_overrides[require_user] = lambda: ctx

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture()
def admin_authed_client(admin_user):
    """
    Like authed_client but the authenticated user's email == ADMIN_EMAIL.
    require_admin in admin.py does an email-equality check first and returns
    early, so no profiles.is_admin DB query is needed for admin route access.
    """
    from fastapi.testclient import TestClient
    from main import app
    from dependencies.auth import require_user, AuthContext

    ctx = AuthContext(user=admin_user, token=FAKE_JWT)
    app.dependency_overrides[require_user] = lambda: ctx

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture()
def valid_token():
    return FAKE_JWT


@pytest.fixture()
def auth_header():
    return f"Bearer {FAKE_JWT}"
