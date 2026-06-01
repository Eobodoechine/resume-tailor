"""
Admin routes: view and action on access requests.
Only accessible to the admin email defined in config OR users with is_admin=TRUE.
"""
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from datetime import datetime, timezone
from dependencies.auth import require_user, AuthContext
from services.supabase_client import get_admin_client
from config import ADMIN_EMAIL
from limiter import limiter

logger = logging.getLogger(__name__)

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
        logger.debug("[require_admin] OK (admin email)  user=%s", ctx.user.id)
        return ctx

    admin = get_admin_client()
    profile = admin.table("profiles").select("is_admin").eq("id", str(ctx.user.id)).execute()
    if profile.data and profile.data[0].get("is_admin"):
        logger.debug("[require_admin] OK (is_admin=True)  user=%s", ctx.user.id)
        return ctx

    logger.warning("[require_admin] 403 non-admin attempted access  user=%s  email=%s", ctx.user.id, email)
    raise HTTPException(status_code=403, detail="Admin access only")


_VALID_STATUSES = {"pending", "approved", "rejected", "all"}


@router.get("/requests")
@limiter.limit("60/minute")
def list_requests(request: Request, status: str = "pending", limit: int = 50, offset: int = 0, _ctx: AuthContext = Depends(require_admin)):
    """List access requests filtered by status: pending | approved | rejected | all"""
    if status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        )
    limit = min(max(1, limit), 200)
    offset = max(0, offset)
    logger.info("[admin] list_requests  by=%s  status=%s  limit=%d  offset=%d", _ctx.user.id, status, limit, offset)
    admin = get_admin_client()

    query = admin.table("access_requests").select("*").order("requested_at", desc=True)
    if status != "all":
        query = query.eq("status", status)

    result = query.range(offset, offset + limit - 1).execute()
    logger.info("[admin] list_requests returned %d row(s)  by=%s", len(result.data), _ctx.user.id)
    return result.data


class ApprovalBody(BaseModel):
    request_id: uuid.UUID


@router.post("/approve")
@limiter.limit("30/minute")
def approve_request(request: Request, body: ApprovalBody, _ctx: AuthContext = Depends(require_admin)):
    """
    Approve an access request.
    DB status is updated FIRST (idempotent), then invite is sent.
    If the invite fails the user is still approved and can log in via /login (TD-05).
    """
    logger.info("[admin] approve_request  request_id=%s  by=%s", body.request_id, _ctx.user.id)
    admin = get_admin_client()

    result = admin.table("access_requests").select("*").eq("id", str(body.request_id)).execute()
    if not result.data:
        logger.warning("[admin] approve_request 404  request_id=%s  by=%s", body.request_id, _ctx.user.id)
        raise HTTPException(status_code=404, detail="Request not found")

    req = result.data[0]
    email = req["email"]
    prev_status = req.get("status", "unknown")
    logger.info("[admin] approving  request_id=%s  email=%s  prev_status=%s  by=%s",
                body.request_id, email, prev_status, _ctx.user.id)

    # Update DB FIRST — approved status is the source of truth.
    # If the invite email fails below, the user is still marked approved
    # and can request a magic link via the login page.
    admin.table("access_requests").update({
        "status": "approved",
        "reviewed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", str(body.request_id)).execute()
    logger.info("[admin] DB status set to approved  request_id=%s  email=%s", body.request_id, email)

    # Send invite — failure is non-fatal since status is already approved
    try:
        admin.auth.admin.invite_user_by_email(email)
        logger.info("[admin] invite email sent  email=%s  by=%s", email, _ctx.user.id)
    except Exception as e:
        logger.error("[admin] invite email FAILED (user still approved)  email=%s  error=%s  by=%s",
                     email, e, _ctx.user.id, exc_info=True)
        return {
            "message": (
                f"Approved, but invite email failed: {str(e)}. "
                "The user can still log in via the login page."
            )
        }

    return {"message": f"Invite sent to {email}"}


@router.post("/reject")
@limiter.limit("30/minute")
def reject_request(request: Request, body: ApprovalBody, _ctx: AuthContext = Depends(require_admin)):
    """Mark an access request as rejected."""
    logger.info("[admin] reject_request  request_id=%s  by=%s", body.request_id, _ctx.user.id)
    admin = get_admin_client()

    result = admin.table("access_requests").select("id, email, status").eq("id", str(body.request_id)).execute()
    if not result.data:
        logger.warning("[admin] reject_request 404  request_id=%s  by=%s", body.request_id, _ctx.user.id)
        raise HTTPException(status_code=404, detail="Request not found")

    email = result.data[0].get("email", "unknown")
    logger.info("[admin] rejecting  request_id=%s  email=%s  by=%s", body.request_id, email, _ctx.user.id)
    admin.table("access_requests").update({
        "status": "rejected",
        "reviewed_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", str(body.request_id)).execute()
    logger.info("[admin] rejected OK  request_id=%s  email=%s  by=%s", body.request_id, email, _ctx.user.id)

    return {"message": "Request rejected"}


@router.get("/users")
def list_users(limit: int = 100, offset: int = 0, _ctx: AuthContext = Depends(require_admin)):
    """List all approved users."""
    limit = min(max(1, limit), 500)
    offset = max(0, offset)
    logger.info("[admin] list_users  by=%s  limit=%d  offset=%d", _ctx.user.id, limit, offset)
    admin = get_admin_client()
    result = admin.table("profiles").select("id, email, full_name, created_at").range(offset, offset + limit - 1).execute()
    logger.info("[admin] list_users returned %d row(s)  by=%s", len(result.data), _ctx.user.id)
    return result.data
