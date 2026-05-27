"""
Custom ASGI middleware.

SecurityHeadersMiddleware   — adds CSP, X-Content-Type-Options, etc.
SlidingSessionMiddleware    — refreshes the session cookie max_age on every
                              successful authenticated response so the session
                              window slides forward from last activity rather
                              than expiring a fixed time after login.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from dependencies.auth import COOKIE_NAME
from config import COOKIE_SECURE, COOKIE_MAX_AGE


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            # 'unsafe-inline' required: every page has inline onclick= handlers
            # and <script> blocks. Remove once all JS moves to app.js with
            # addEventListener and a nonce-based CSP is in place.
            "script-src 'self' https://cdnjs.cloudflare.com 'unsafe-inline'; "
            # 'unsafe-inline' for styles: inline style= attributes on HTML elements.
            "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        return response


class SlidingSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # CRITICAL: skip auth routes entirely. They own the cookie:
        #   - DELETE /api/auth/session writes Max-Age=0 to clear it.
        #     Without this opt-out we'd re-set it with the old token and
        #     undo the logout the user just performed.
        #   - POST /api/auth/session writes the NEW token (magic-link
        #     exchange). Without this opt-out we'd overwrite it with the
        #     stale token from request.cookies and silently keep the user
        #     on their old session.
        if request.url.path.startswith("/api/auth/"):
            return response

        session_token = request.cookies.get(COOKIE_NAME)
        if not session_token:
            return response

        # Only refresh on successful responses (skip 4xx/5xx)
        if not (200 <= response.status_code < 400):
            return response

        # Don't override if the route already set or deleted this cookie.
        # This is a secondary guard — the /api/auth/ path check above is
        # the primary one. Belt-and-suspenders for any future auth-adjacent
        # routes that manage the cookie directly.
        already_set = any(
            v.lower().startswith(f"{COOKIE_NAME.lower()}=")
            for v in response.headers.getlist("set-cookie")
        )
        if already_set:
            return response

        response.set_cookie(
            key=COOKIE_NAME,
            value=session_token,
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
            max_age=COOKIE_MAX_AGE,
            path="/",
        )
        return response
