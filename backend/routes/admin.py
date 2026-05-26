"""
Admin routes: view and action on access requests.
Only accessible to the admin email defined in config OR users with is_admin=TRUE.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from dependencies.auth import require_user, AuthContext
from services.supabase_client import get_admin_client
from config import ADMIN_EMAIL

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(ctx: AuthContext = Depends(require_user)) -> AuthContext:
    """
    Gate admin endpoints. Reuses the cookie+Bearer auth from require_user
    so the admin UI works with HttpOnly cookies (TD-09).

    Allows either:
      1. user.email == ADMIN_EMAIL (env), case-insensitive, or
      2. profiles.is_admin == TRUE
    """
    email = (getattr(ctx.user, "email", "") or "").lower()
    if email and email == (ADMIN_EMAIL or "").lower():
        return ctx

    admin = get_admin_client()
    profile = admin.table("profiles").select("is_admin").eq("id", str(ctx.user.id)).execute()
    if profile.data and profile.data[0].get("is_admin"):
        return ctx

    raise HTTPException(status_code=403, detail="Admin access only")


@router.get("/requests")
def list_requests(status: str = "pending", _ctx: AuthContext = Depends(require_admin)):
    """List access requests filtered by status: pending | approved | rejected | all"""
    admin = get_admin_client()

    query = admin.table("access_requests").select("*").order("requested_at", desc=True)
    if status != "all":
        query = query.eq("status", status)

    result = query.execute()
    return result.data


class ApprovalBody(BaseModel):
    request_id: str


@router.post("/approve")
def approve_request(body: ApprovalBody, _ctx: AuthContext = Depends(require_admin)):
    """
    Approve an access request.
    Updates status to 'approved' and sends a magic link invite to the user.
    """
    admin = get_admin_client()

    result = admin.table("access_requests").select("*").eq("id", body.request_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    req = result.data[0]
    email = req["email"]

    try:
        admin.auth.admin.invite_user_by_email(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send invite: {str(e)}")

    admin.table("access_requests").update({
        "status": "approved",
        "reviewed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", body.request_id).execute()

    return {"message": f"Invite sent to {email}"}


@router.post("/reject")
def reject_request(body: ApprovalBody, _ctx: AuthContext = Depends(require_admin)):
    """Mark an access request as rejected."""
    admin = get_admin_client()

    result = admin.table("access_requests").select("id").eq("id", body.request_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    admin.table("access_requests").update({
        "status": "rejected",
        "reviewed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", body.request_id).execute()

    return {"message": "Request rejected"}


@router.get("/users")
def list_users(_ctx: AuthContext = Depends(require_admin)):
    """List all approved users."""
    admin = get_admin_client()
    result = admin.table("profiles").select("id, email, full_name, created_at").execute()
    return result.data
