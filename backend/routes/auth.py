"""
Auth routes: request access, magic link login, session cookie management, logout.
"""
from fastapi import APIRouter, HTTPException, Response, Request
from pydantic import BaseModel, EmailStr
import httpx
from services.supabase_client import get_admin_client, get_anon_client, get_user_from_token
from config import ADMIN_EMAIL, COOKIE_SECURE, COOKIE_MAX_AGE, RESEND_API_KEY
from dependencies.auth import COOKIE_NAME
from limiter import limiter

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

    # Check if already approved / already requested
    existing = admin.table("access_requests").select("*").eq("email", email).execute()
    if existing.data:
        status = existing.data[0]["status"]
        if status == "approved":
            return {"message": "Your account is already approved. Check your email for a login link."}
        elif status == "pending":
            return {"message": "Your request is already pending. You'll hear back soon."}
        elif status == "rejected":
            raise HTTPException(status_code=403, detail="Your access request was not approved.")

    # Insert new request
    admin.table("access_requests").insert({
        "email": email,
        "full_name": body.full_name,
        "reason": body.reason,
        "status": "pending"
    }).execute()

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

    # Admin bypasses approval check
    if email == (ADMIN_EMAIL or "").lower():
        _send_magic_link(admin, email)
        return {"message": "Magic link sent. Check your email."}

    # Check approval
    result = admin.table("access_requests").select("status").eq("email", email).execute()
    if not result.data:
        raise HTTPException(status_code=403, detail="No access request found. Please request access first.")

    status = result.data[0]["status"]
    if status == "pending":
        raise HTTPException(status_code=403, detail="Your request is still pending approval.")
    elif status == "rejected":
        raise HTTPException(status_code=403, detail="Your access request was not approved.")

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
        raise HTTPException(status_code=400, detail="Token is required")

    user = get_user_from_token(body.token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Double-check the user is approved before issuing a session cookie.
    # (Magic links bypass the login endpoint, so we enforce approval here too.)
    admin = get_admin_client()
    email = (getattr(user, "email", None) or "").lower()
    if email and email != (ADMIN_EMAIL or "").lower():
        ar = admin.table("access_requests").select("status").eq("email", email).execute()
        if not ar.data or ar.data[0]["status"] != "approved":
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
    return {"message": "Session created"}


@router.delete("/session")
def delete_session(response: Response):
    """
    Clear the HttpOnly session cookie (logout).
    Also works when no cookie is present — always returns 200.
    """
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite="lax", secure=COOKIE_SECURE)
    return {"message": "Logged out"}


APP_URL = "https://resume-tailor-ogop.onrender.com"


def _send_magic_link(admin_client, email: str):
    """
    Send a magic link email.

    Strategy (in order of preference):
    1. Resend HTTP API — if RESEND_API_KEY is set, generate a Supabase magic link
       URL via the admin API and deliver it through Resend's reliable HTTP endpoint.
       This bypasses Supabase's SMTP layer entirely, giving us Resend's free-tier
       3,000 emails/month with no 2/hour cap.
    2. Supabase anon OTP — fallback when RESEND_API_KEY is absent. Uses Supabase's
       built-in mailer (2/hour cap on free plan).
    """
    if RESEND_API_KEY:
        _send_via_resend(admin_client, email)
    else:
        _send_via_supabase_otp(email)


def _send_via_resend(admin_client, email: str):
    """
    Generate a Supabase magic link URL then deliver it via Resend's HTTP API.
    The link URL contains the access_token that the frontend exchanges for a session.
    """
    try:
        # Step 1: Generate a magic link URL via the admin API
        link_resp = admin_client.auth.admin.generate_link({
            "type": "magiclink",
            "email": email,
            "options": {"redirect_to": f"{APP_URL}/dashboard"},
        })
        magic_url = link_resp.properties.action_link
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate magic link: {str(e)}")

    try:
        # Step 2: Send it via Resend's HTTP API
        html_body = f"""
        <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;">
          <h2 style="color:#1a2e4a;margin-bottom:8px;">Sign in to Resume Tailor</h2>
          <p style="color:#555;margin-bottom:24px;">Click the button below to sign in. This link expires in 1 hour.</p>
          <a href="{magic_url}"
             style="display:inline-block;background:#1a2e4a;color:white;text-decoration:none;
                    padding:12px 24px;border-radius:6px;font-weight:600;">
            Sign In
          </a>
          <p style="color:#999;font-size:12px;margin-top:24px;">
            If you didn't request this, you can safely ignore this email.
          </p>
        </div>
        """
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Resume Tailor <onboarding@resend.dev>",
                "to": [email],
                "subject": "Your sign-in link for Resume Tailor",
                "html": html_body,
            },
            timeout=10.0,
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to send magic link email: {resp.text}"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send magic link: {str(e)}")


def _send_via_supabase_otp(email: str):
    """
    Fallback: send a magic link via Supabase's built-in mailer (2/hour on free plan).
    """
    try:
        anon = get_anon_client()
        anon.auth.sign_in_with_otp({
            "email": email,
            "options": {
                "email_redirect_to": f"{APP_URL}/dashboard",
                "should_create_user": True,
            }
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send magic link: {str(e)}")
