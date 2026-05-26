"""
Unit tests for middleware.py.

Tests are pure ASGI-level: we build minimal Starlette apps that mount only
the middleware under test, so there are no Supabase / auth dependencies.
"""
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

# ── Import middleware directly (conftest stubs config / dependencies.auth) ────
from middleware import SlidingSessionMiddleware, SecurityHeadersMiddleware

COOKIE_NAME = "rt_session"
SESSION_VAL = "fake-session-token"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_app(middleware_cls, route_path="/api/test", handler=None):
    """Return a minimal Starlette app wrapped with *middleware_cls*."""
    if handler is None:
        async def handler(request: Request):
            return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(route_path, handler)])
    app.add_middleware(middleware_cls)
    return app


def _client_with_cookie(app):
    """TestClient that sends the session cookie on every request."""
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set(COOKIE_NAME, SESSION_VAL)
    return client


# ─────────────────────────────────────────────────────────────────────────────
# SlidingSessionMiddleware
# ─────────────────────────────────────────────────────────────────────────────

class TestSlidingSessionMiddleware:

    def test_active_session_cookie_is_refreshed(self):
        """A normal authenticated request should receive a refreshed Set-Cookie."""
        app = _make_app(SlidingSessionMiddleware)
        client = _client_with_cookie(app)

        resp = client.get("/api/test")
        assert resp.status_code == 200

        set_cookies = resp.headers.get_list("set-cookie")
        session_cookies = [c for c in set_cookies if c.lower().startswith(f"{COOKIE_NAME}=")]
        assert session_cookies, "Expected a refreshed Set-Cookie for the session"
        # The cookie should carry the same token value
        assert SESSION_VAL in session_cookies[0]

    def test_logout_does_not_re_set_session(self):
        """DELETE /api/auth/session must NOT have the session cookie re-set by middleware."""

        async def logout_handler(request: Request):
            resp = Response("ok")
            resp.delete_cookie(COOKIE_NAME)
            return resp

        app = Starlette(routes=[Route("/api/auth/session", logout_handler, methods=["DELETE"])])
        app.add_middleware(SlidingSessionMiddleware)
        client = _client_with_cookie(app)

        resp = client.delete("/api/auth/session")
        assert resp.status_code == 200

        set_cookies = resp.headers.get_list("set-cookie")
        # The only Set-Cookie header should be the Max-Age=0 deletion from the route.
        # The middleware must not have added another header with the live token.
        live_cookies = [
            c for c in set_cookies
            if c.lower().startswith(f"{COOKIE_NAME}=") and SESSION_VAL in c
        ]
        assert not live_cookies, (
            "Middleware re-set the session cookie after logout — sliding window bug"
        )

    def test_unauthenticated_request_not_touched(self):
        """Requests without a session cookie should not get a Set-Cookie response."""
        app = _make_app(SlidingSessionMiddleware)
        client = TestClient(app)  # no cookie

        resp = client.get("/api/test")
        assert resp.status_code == 200
        set_cookies = resp.headers.get_list("set-cookie")
        session_cookies = [c for c in set_cookies if COOKIE_NAME in c.lower()]
        assert not session_cookies

    def test_error_response_not_refreshed(self):
        """Middleware should not refresh cookies on 4xx/5xx responses."""

        async def error_handler(request: Request):
            return JSONResponse({"error": "nope"}, status_code=403)

        app = _make_app(SlidingSessionMiddleware, handler=error_handler)
        client = _client_with_cookie(app)

        resp = client.get("/api/test")
        assert resp.status_code == 403
        set_cookies = resp.headers.get_list("set-cookie")
        live_cookies = [c for c in set_cookies if SESSION_VAL in c]
        assert not live_cookies

    def test_magic_link_exchange_not_overwritten(self):
        """POST /api/auth/session (magic-link exchange) must not have its new
        token overwritten by the middleware re-setting the old cookie."""

        new_token = "brand-new-token"

        async def login_handler(request: Request):
            resp = JSONResponse({"ok": True})
            resp.set_cookie(COOKIE_NAME, new_token, httponly=True)
            return resp

        app = Starlette(routes=[Route("/api/auth/session", login_handler, methods=["POST"])])
        app.add_middleware(SlidingSessionMiddleware)
        # Client sends the OLD cookie (as if already partially logged-in)
        client = _client_with_cookie(app)

        resp = client.post("/api/auth/session")
        assert resp.status_code == 200

        set_cookies = resp.headers.get_list("set-cookie")
        # Only the new token should appear; the old SESSION_VAL must not be re-set
        old_token_cookies = [c for c in set_cookies if SESSION_VAL in c]
        assert not old_token_cookies, (
            "Middleware re-set the old session cookie over the new magic-link token"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SecurityHeadersMiddleware
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurityHeadersMiddleware:

    def _get(self, path="/api/test"):
        app = _make_app(SecurityHeadersMiddleware, route_path=path)
        client = TestClient(app)
        return client.get(path)

    def test_csp_header_present(self):
        resp = self._get()
        assert "content-security-policy" in resp.headers

    def test_csp_denies_external_scripts(self):
        csp = resp = self._get().headers["content-security-policy"]
        assert "script-src 'self'" in csp

    def test_x_content_type_options(self):
        resp = self._get()
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_x_frame_options(self):
        resp = self._get()
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_referrer_policy(self):
        resp = self._get()
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
