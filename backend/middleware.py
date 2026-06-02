"""
Custom ASGI middleware.

SecurityHeadersMiddleware   — adds CSP, X-Content-Type-Options, etc.
SlidingSessionMiddleware    — refreshes the session cookie max_age on every
                              successful authenticated response so the session
                              window slides forward from last activity rather
                              than expiring a fixed time after login.

Both are implemented as pure ASGI middleware (scope/receive/send) rather than
BaseHTTPMiddleware so that chunks are passed through without buffering. This
keeps SSE streaming (POST /api/tailor/stream) working correctly and avoids
double-buffering large binary responses like PDF downloads.
"""
import logging

from dependencies.auth import COOKIE_NAME
from config import COOKIE_SECURE, COOKIE_MAX_AGE

logger = logging.getLogger(__name__)


# ── Header helpers ────────────────────────────────────────────────────────────

def _headers_get(headers: list, name: str) -> str:
    """Case-insensitive lookup over an ASGI header list; returns '' if absent."""
    needle = name.lower().encode("latin-1")
    for k, v in headers:
        if k.lower() == needle:
            return v.decode("latin-1")
    return ""


def _log_headers(label: str, path: str, headers: list) -> None:
    if "/pdf" in path:
        cd = _headers_get(headers, "content-disposition") or "(absent)"
        ct = _headers_get(headers, "content-type") or "(absent)"
        logger.info(
            "[middleware] %s  path=%s  Content-Disposition=%r  Content-Type=%r",
            label, path, cd, ct,
        )


def _parse_cookies(scope) -> dict:
    """Parse the Cookie request header from the ASGI scope."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"cookie":
            result = {}
            for part in value.decode("latin-1").split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    result[k.strip()] = v.strip()
            return result
    return {}


def _build_set_cookie_header(
    name: str,
    value: str,
    *,
    httponly: bool,
    samesite: str,
    secure: bool,
    max_age: int,
    path: str,
) -> bytes:
    parts = [
        f"{name}={value}",
        f"Path={path}",
        f"Max-Age={max_age}",
        f"SameSite={samesite.capitalize()}",
    ]
    if httponly:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts).encode("latin-1")


# ── Middleware ────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        _log_headers("BEFORE SecurityHeaders", path, list(scope.get("headers", [])))

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                _log_headers("AFTER  call_next (before SecurityHeaders adds)", path, headers)
                # Preview responses are loaded inside an iframe on the same origin.
                # All other routes use 'none' to block clickjacking.
                is_preview = "/api/tailor/" in path and path.endswith("/preview")
                frame_ancestors = "'self'" if is_preview else "'none'"
                headers.extend([
                    (
                        b"content-security-policy",
                        (
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
                            f"frame-ancestors {frame_ancestors}; "
                            "base-uri 'self'; "
                            "form-action 'self';"
                        ).encode("latin-1"),
                    ),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                ])
                _log_headers("AFTER  SecurityHeaders adds", path, headers)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


class SlidingSessionMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # CRITICAL: skip auth routes entirely. They own the cookie:
        #   - DELETE /api/auth/session writes Max-Age=0 to clear it.
        #     Without this opt-out we'd re-set it with the old token and
        #     undo the logout the user just performed.
        #   - POST /api/auth/session writes the NEW token (magic-link
        #     exchange). Without this opt-out we'd overwrite it with the
        #     stale token from request.cookies and silently keep the user
        #     on their old session.
        if path.startswith("/api/auth/"):
            await self.app(scope, receive, send)
            return

        session_token = _parse_cookies(scope).get(COOKIE_NAME)
        if not session_token:
            logger.debug(
                "[session-middleware] no session cookie  path=%s  method=%s",
                scope.get("path", ""),
                scope.get("method", ""),
            )
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                status = message.get("status", 200)
                headers = list(message.get("headers", []))
                _log_headers("AFTER  SlidingSession call_next", path, headers)

                # Only refresh on successful responses (skip 4xx/5xx)
                if 200 <= status < 400:
                    cookie_prefix = f"{COOKIE_NAME}=".lower().encode("latin-1")
                    # Don't override if the route already set or deleted this cookie.
                    # This is a secondary guard — the /api/auth/ path check above is
                    # the primary one. Belt-and-suspenders for any future auth-adjacent
                    # routes that manage the cookie directly.
                    already_set = any(
                        v.lower().startswith(cookie_prefix)
                        for k, v in headers
                        if k.lower() == b"set-cookie"
                    )
                    if not already_set:
                        headers.append((
                            b"set-cookie",
                            _build_set_cookie_header(
                                COOKIE_NAME,
                                session_token,
                                httponly=True,
                                samesite="lax",
                                secure=COOKIE_SECURE,
                                max_age=COOKIE_MAX_AGE,
                                path="/",
                            ),
                        ))
                        _log_headers("AFTER  SlidingSession set_cookie", path, headers)
                        message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)
