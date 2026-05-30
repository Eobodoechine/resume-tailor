"""
Auth routes: request access, magic link login, session cookie management, logout.
"""
import os
import logging
from fastapi import APIRouter, HTTPException, Response, Request
from pydantic import BaseModel, EmailStr
from services.supabase_client import get_admin_client, get_anon_client, get_user_from_token
from config import ADMIN_EMAIL, COOKIE_SECURE, COOKIE_MAX_AGE
from dependencies.auth import COOKIE_NAME
from limiter import limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class AccessRequestBody(BaseModel):
    email: EmailStr
    full_name: str = ""
    reason: str = ""


class LoginBody(BaseModel):
    email: EmailStr


@router.post("/request-access")
@limiter.limit("5/minute")
def request_access(request: Request, body: AccessRequestBody):
    """User submits their email to request access. Creates a pending record."""
    admin = get_admin_client()
    # Normalize email — Pydantic EmailStr does not lowercase, and the
    # access_requests.email UNIQUE constraint is case-sensitive at the DB level.
    email = body.email.lower()
    logger.info("[request-access] email=%s full_name=%r", email, body.full_name or "")

    # Check if already approved / already requested
    existing = admin.table("access_requests").select("*").eq("email", email).execute()
    if existing.data:
        status = existing.data[0]["status"]
        logger.info("[request-access] existing record found  email=%s  status=%s", email, status)
        if status == "approved":
            return {"message": "Your account is already approved. Check your email for a login link."}
        elif status == "pending":
            return {"message": "Your request is already pending. You'll hear back soon."}
        elif status == "rejected":
            logger.warning("[request-access] rejected user attempted re-request  email=%s", email)
            raise HTTPException(status_code=403, detail="Your access request was not approved.")

    # Insert new request
    admin.table("access_requests").insert({
        "email": email,
        "full_name": body.full_name,
        "reason": body.reason,
        "status": "pending"
    }).execute()
    logger.info("[request-access] new request inserted  email=%s", email)

    return {"message": "Request received. You'll get an email when you're approved."}


@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, body: LoginBody):
    """
    Send a magic link to an approved user.
    Checks access_requests table for approval before sending.
    """
    admin = get_admin_client()
    email = body.email.lower()
    logger.info("[login] attempt  email=%s", email)

    # Admin bypasses approval check
    if email == (ADMIN_EMAIL or "").lower():
        logger.info("[login] admin bypass  email=%s — sending magic link", email)
        _send_magic_link(admin, email)
        return {"message": "Magic link sent. Check your email."}

    # Check approval
    result = admin.table("access_requests").select("status").eq("email", email).execute()
    if not result.data:
        logger.warning("[login] 403 no access request found  email=%s", email)
        raise HTTPException(status_code=403, detail="No access request found. Please request access first.")

    status = result.data[0]["status"]
    if status == "pending":
        logger.warning("[login] 403 still pending  email=%s", email)
        raise HTTPException(status_code=403, detail="Your request is still pending approval.")
    elif status == "rejected":
        logger.warning("[login] 403 rejected  email=%s", email)
        raise HTTPException(status_code=403, detail="Your access request was not approved.")

    logger.info("[login] approved — sending magic link  email=%s", email)
    _send_magic_link(admin, email)
    return {"message": "Magic link sent. Check your email."}


class SessionBody(BaseModel):
    token: str


@router.post("/session")
@limiter.limit("10/minute")
def create_session(request: Request, body: SessionBody, response: Response):
    """
    Exchange a Supabase JWT for an HttpOnly session cookie (TD-09).

    The frontend calls this after extracting access_token from the magic link
    URL fragment. On success the browser receives an HttpOnly, SameSite=Lax
    cookie — the token is then invisible to JavaScript, mitigating XSS theft.

    Rate-limited to 10/minute to prevent token-stuffing attacks.
    """
    if not body.token:
        logger.warning("[create-session] 400 empty token received")
        raise HTTPException(status_code=400, detail="Token is required")

    user = get_user_from_token(body.token)
    if not user:
        logger.warning("[create-session] 401 token rejected by Supabase")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    email = (getattr(user, "email", None) or "").lower()
    logger.info("[create-session] token valid  user=%s  email=%s", user.id, email)

    # Double-check the user is approved before issuing a session cookie.
    # (Magic links bypass the login endpoint, so we enforce approval here too.)
    admin = get_admin_client()
    if email and email != (ADMIN_EMAIL or "").lower():
        ar = admin.table("access_requests").select("status").eq("email", email).execute()
        if not ar.data or ar.data[0]["status"] != "approved":
            logger.warning(
                "[create-session] 403 not approved  user=%s  email=%s  "
                "access_request_status=%s",
                user.id, email,
                ar.data[0]["status"] if ar.data else "no_record",
            )
            raise HTTPException(status_code=403, detail="Your account is not approved.")

    # SameSite=Lax allows the cookie on top-level navigations (magic link redirect)
    # while blocking CSRF from cross-origin requests.  COOKIE_SECURE is True in
    # production (HTTPS) and False in local dev (plain http).
    response.set_cookie(
        key=COOKIE_NAME,
        value=body.token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=COOKIE_MAX_AGE,
        path="/",
    )
    logger.info("[create-session] cookie set  user=%s  email=%s  secure=%s", user.id, email, COOKIE_SECURE)

    # Keep profiles.email in sync with auth.users in case the user changed their
    # email after initial signup — the trigger only fires once at creation (TD-14).
    if email:
        try:
            admin.table("profiles").update({"email": email}).eq("id", str(user.id)).execute()
            logger.debug("[create-session] profile email synced  user=%s  email=%s", user.id, email)
        except Exception as e:
            logger.warning("[create-session] profile email sync failed (non-fatal)  user=%s  error=%s", user.id, e)

    return {"message": "Session created"}


@router.delete("/session")
def delete_session(response: Response, request: Request):
    """
    Clear the HttpOnly session cookie (logout).
    Also works when no cookie is present — always returns 200.
    """
    had_cookie = bool(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite="lax", secure=COOKIE_SECURE)
    logger.info("[delete-session] logout  had_cookie=%s", had_cookie)
    return {"message": "Logged out"}


# Read APP_URL from env so magic-link redirects survive URL changes (TD-13)
APP_URL = os.getenv("APP_URL", "https://resume-tailor-ogop.onrender.com")


def _send_magic_link(admin_client, email: str):
    """
    Send a magic link via Supabase anon OTP.
    Supabase is configured with Gmail SMTP as custom SMTP provider,
    giving us 500 emails/day with no per-hour cap.
    """
    _send_via_supabase_otp(email)


def _send_via_supabase_otp(email: str):
    """
    Send a magic link via Supabase's built-in mailer.
    """
    logger.info("[otp] sending magic link via Supabase OTP  email=%s  redirect=%s/dashboard", email, APP_URL)
    try:
        anon = get_anon_client()
        anon.auth.sign_in_with_otp({
            "email": email,
            "options": {
                "email_redirect_to": f"{APP_URL}/dashboard",
                "should_create_user": False,
            }
        })
        logger.info("[otp] magic link sent OK  email=%s", email)
    except Exception as e:
        logger.error("[otp] FAILED to send magic link  email=%s  error=%s", email, e)
        raise HTTPException(status_code=500, detail=f"Failed to send magic link: {str(e)}")
