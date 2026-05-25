"""
Admin routes: view and action on access requests.
Only accessible to the admin email defined in config.
"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from datetime import datetime
from services.supabase_client import get_admin_client, get_user_from_token
from config import ADMIN_EMAIL

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    if user.email != ADMIN_EMAIL:
        # Also allow if is_admin flag set in profiles
        admin = get_admin_client()
        profile = admin.table("profiles").select("is_admin").eq("id", str(user.id)).execute()
        if not profile.data or not profile.data[0].get("is_admin"):
            raise HTTPException(status_code=403, detail="Admin access only")
    return user


@router.get("/requests")
def list_requests(status: str = "pending", authorization: str = Header(None)):
    """List access requests filtered by status: pending | approved | rejected | all"""
    _require_admin(authorization)
    admin = get_admin_client()

    query = admin.table("access_requests").select("*").order("requested_at", desc=True)
    if status != "all":
        query = query.eq("status", status)

    result = query.execute()
    return result.data


class ApprovalBody(BaseModel):
    request_id: str


@router.post("/approve")
def approve_request(body: ApprovalBody, authorization: str = Header(None)):
    """
    Approve an access request.
    Updates status to 'approved' and sends a magic link invite to the user.
    """
    _require_admin(authorization)
    admin = get_admin_client()

    # Get the request
    result = admin.table("access_requests").select("*").eq("id", body.request_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    req = result.data[0]
    email = req["email"]

    # Send Supabase magic link invite
    try:
        admin.auth.admin.invite_user_by_email(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send invite: {str(e)}")

    # Update status
    admin.table("access_requests").update({
        "status": "approved",
        "reviewed_at": datetime.utcnow().isoformat()
    }).eq("id", body.request_id).execute()

    return {"message": f"Invite sent to {email}"}


@router.post("/reject")
def reject_request(body: ApprovalBody, authorization: str = Header(None)):
    """Mark an access request as rejected."""
    _require_admin(authorization)
    admin = get_admin_client()

    result = admin.table("access_requests").select("id").eq("id", body.request_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    admin.table("access_requests").update({
        "status": "rejected",
        "reviewed_at": datetime.utcnow().isoformat()
    }).eq("id", body.request_id).execute()

    return {"message": "Request rejected"}


@router.get("/users")
def list_users(authorization: str = Header(None)):
    """List all approved users."""
    _require_admin(authorization)
    admin = get_admin_client()
    result = admin.table("profiles").select("id, email, full_name, created_at").execute()
    return result.data
