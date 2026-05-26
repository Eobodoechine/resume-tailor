"""
Profile routes: read and update user profile info.
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from dependencies.auth import require_user, AuthContext
from services.supabase_client import get_client, get_admin_client
from limiter import limiter

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    website: Optional[str] = None


@router.get("/")
@limiter.limit("60/minute")
def get_profile(request: Request, ctx: AuthContext = Depends(require_user)):
    """
    Return the user's profile. If no row exists yet (e.g. user predates the
    handle_new_user trigger), return an empty shape rather than 404 so the
    dashboard's Promise.all doesn't reject the whole batch.
    """
    db = get_client(ctx.token)
    result = db.table("profiles").select("*").eq("id", str(ctx.user.id)).execute()
    if not result.data:
        return {
            "id": str(ctx.user.id),
            "email": getattr(ctx.user, "email", "") or "",
            "full_name": "",
            "phone": "",
            "location": "",
            "linkedin_url": "",
            "website": "",
        }
    return result.data[0]


@router.patch("/")
@limiter.limit("20/minute")
def update_profile(request: Request, body: ProfileUpdate, ctx: AuthContext = Depends(require_user)):
    """
    Upsert the user's profile. Filters out both None and empty strings so
    leaving a field blank in the form doesn't wipe previously-saved data.
    """
    admin = get_admin_client()
    user_id = str(ctx.user.id)

    # model_dump (Pydantic v2) — body.dict() is deprecated.
    updates = {k: v for k, v in body.model_dump().items() if v is not None and v != ""}
    if not updates:
        return {"message": "No changes"}

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Upsert — handles the case where a profile row doesn't exist yet.
    existing = admin.table("profiles").select("id").eq("id", user_id).execute()
    if existing.data:
        admin.table("profiles").update(updates).eq("id", user_id).execute()
    else:
        updates["id"] = user_id
        updates["email"] = getattr(ctx.user, "email", "") or ""
        admin.table("profiles").insert(updates).execute()

    return {"message": "Profile updated"}
