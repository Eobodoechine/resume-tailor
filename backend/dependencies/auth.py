"""
Shared FastAPI auth dependency.

Replaces the _require_user() copy-paste that existed in every router.
Returns an AuthContext with both the verified user object and the raw JWT,
so downstream routes can use get_client(ctx.token) for RLS-respecting queries.

Auth strategy (in priority order):
  1. HttpOnly cookie `rt_session` — set by POST /api/auth/session (TD-09).
     Invisible to JS, XSS-safe. Preferred for browser clients.
  2. Authorization: Bearer <token> header — legacy / API clients.
     Kept for backward compatibility during cookie rollout.

Usage:
    from fastapi import Depends
    from dependencies.auth import require_user, AuthContext

    @router.get("/")
    def my_route(ctx: AuthContext = Depends(require_user)):
        client = get_client(ctx.token)   # RLS-respecting
        ...
"""
import logging
from dataclasses import dataclass
from fastapi import Header, HTTPException, Request
from services.supabase_client import get_user_from_token

COOKIE_NAME = "rt_session"
logger = logging.getLogger(__name__)


@dataclass
class AuthContext:
    """Verified auth state for a single request."""
    user: object   # supabase User object
    token: str     # raw JWT — pass to get_client() for RLS-respecting queries


def require_user(request: Request, authorization: str = Header(None)) -> AuthContext:
    """
    Validate the request and return AuthContext.

    Checks HttpOnly cookie first (more secure), falls back to Authorization
    header (backward compatible with existing frontend during cookie rollout).

    Raises 401 if neither is present or the token is invalid/expired.
    """
    path = request.url.path
    method = request.method
    logger.info(f"[auth] require_user  method={method}  path={path}")

    # ── 1. Try HttpOnly cookie ──────────────────────────────────────────────
    token = request.cookies.get(COOKIE_NAME)
    if token:
        logger.info(f"[auth] found cookie '{COOKIE_NAME}'  path={path}")
    else:
        logger.warning(f"[auth] no cookie '{COOKIE_NAME}'  path={path}")

    # ── 2. Fall back to Authorization header ───────────────────────────────
    if not token:
        if not authorization or not authorization.startswith("Bearer "):
            logger.error(f"[auth] 401 — no cookie and no Bearer header  path={path}")
            raise HTTPException(status_code=401, detail="Not authenticated")
        raw = authorization.split(" ", 1)[1]
        if not raw:
            logger.error(f"[auth] 401 — empty Bearer token  path={path}")
            raise HTTPException(status_code=401, detail="Not authenticated")
        token = raw
        logger.info(f"[auth] using Bearer token fallback  path={path}")

    # ── 3. Verify the token ─────────────────────────────────────────────────
    logger.info(f"[auth] verifying token with Supabase  path={path}")
    user = get_user_from_token(token)
    if not user:
        logger.error(f"[auth] 401 — Supabase rejected token  path={path}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    logger.info(f"[auth] OK  user={user.id}  path={path}")
    return AuthContext(user=user, token=token)
