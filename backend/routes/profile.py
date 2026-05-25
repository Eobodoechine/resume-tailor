"""
Profile routes: read and update user profile info.
"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
from services.supabase_client import get_admin_client, get_user_from_token

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    website: Optional[str] = None


def _require_user(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


@router.get("/")
def get_profile(authorization: str = Header(None)):
    user = _require_user(authorization)
    admin = get_admin_client()
    result = admin.table("profiles").select("*").eq("id", str(user.id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result.data[0]


@router.patch("/")
def update_profile(body: ProfileUpdate, authorization: str = Header(None)):
    user = _require_user(authorization)
    admin = get_admin_client()
    updates = {k: v for k, v in body.dict().items() if v is not None}
    updates["updated_at"] = "now()"
    admin.table("profiles").update(updates).eq("id", str(user.id)).execute()
    return {"message": "Profile updated"}
