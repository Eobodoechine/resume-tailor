"""
Auth routes: request access, magic link login, logout.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from services.supabase_client import get_admin_client
from config import ADMIN_EMAIL

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AccessRequestBody(BaseModel):
    email: EmailStr
    full_name: str = ""
    reason: str = ""


class LoginBody(BaseModel):
    email: EmailStr


@router.post("/request-access")
def request_access(body: AccessRequestBody):
    """User submits their email to request access. Creates a pending record."""
    admin = get_admin_client()

    # Check if already approved / already requested
    existing = admin.table("access_requests").select("*").eq("email", body.email).execute()
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
        "email": body.email,
        "full_name": body.full_name,
        "reason": body.reason,
        "status": "pending"
    }).execute()

    return {"message": "Request received. You'll get an email when you're approved."}


@router.post("/login")
def login(body: LoginBody):
    """
    Send a magic link to an approved user.
    Checks access_requests table for approval before sending.
    """
    admin = get_admin_client()

    # Admin bypasses approval check
    if body.email == ADMIN_EMAIL:
        _send_magic_link(admin, body.email)
        return {"message": "Magic link sent. Check your email."}

    # Check approval
    result = admin.table("access_requests").select("status").eq("email", body.email).execute()
    if not result.data:
        raise HTTPException(status_code=403, detail="No access request found. Please request access first.")

    status = result.data[0]["status"]
    if status == "pending":
        raise HTTPException(status_code=403, detail="Your request is still pending approval.")
    elif status == "rejected":
        raise HTTPException(status_code=403, detail="Your access request was not approved.")

    _send_magic_link(admin, body.email)
    return {"message": "Magic link sent. Check your email."}


def _send_magic_link(admin_client, email: str):
    try:
        admin_client.auth.admin.generate_link({
            "type": "magiclink",
            "email": email,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send magic link: {str(e)}")
