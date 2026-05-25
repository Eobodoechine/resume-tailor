"""
Master resume routes: synthesize, get, and update via gap-filling chat.
"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
from services.supabase_client import get_admin_client, get_user_from_token
from services import claude as claude_service
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

router = APIRouter(prefix="/api/master", tags=["master"])
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _require_user(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _get_profile(admin, user_id: str) -> dict:
    result = admin.table("profiles").select("*").eq("id", user_id).execute()
    return result.data[0] if result.data else {}


@router.get("/")
def get_master_resume(authorization: str = Header(None)):
    """Get the current master resume for the user."""
    user = _require_user(authorization)
    admin = get_admin_client()
    result = admin.table("master_resumes").select("*").eq("user_id", str(user.id)).execute()
    if not result.data:
        return {"content": None, "last_updated": None}
    return result.data[0]


@router.post("/synthesize")
def synthesize_master(authorization: str = Header(None)):
    """
    Re-synthesize the master resume from all uploaded resume files.
    Call this after uploading new files.
    """
    user = _require_user(authorization)
    admin = get_admin_client()

    # Pull all extracted texts
    files = admin.table("resume_files") \
        .select("extracted_text, filename") \
        .eq("user_id", str(user.id)) \
        .execute()

    if not files.data:
        raise HTTPException(status_code=400, detail="No resume files uploaded yet. Upload at least one file first.")

    texts = [f["extracted_text"] for f in files.data if f.get("extracted_text")]
    if not texts:
        raise HTTPException(status_code=400, detail="Could not extract text from uploaded files.")

    profile = _get_profile(admin, str(user.id))

    # Synthesize via Claude
    try:
        master_content = claude_service.synthesize_master_resume(texts, profile)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    # Upsert master resume
    existing = admin.table("master_resumes").select("id").eq("user_id", str(user.id)).execute()
    if existing.data:
        admin.table("master_resumes").update({
            "content": master_content,
            "last_updated": "now()"
        }).eq("user_id", str(user.id)).execute()
    else:
        admin.table("master_resumes").insert({
            "user_id": str(user.id),
            "content": master_content
        }).execute()

    return {"message": "Master resume synthesized", "preview": master_content[:500] + "..."}


MAX_HISTORY_TURNS = 20  # cap conversation history to prevent token bloat


class GapMessage(BaseModel):
    message: str                      # user's latest message
    history: list[dict] = []          # [{role: "user"|"assistant", content: "..."}]


@router.post("/gap-fill/chat")
def gap_fill_chat(body: GapMessage, authorization: str = Header(None)):
    """
    Conversational gap-filling. Claude reviews the master resume,
    asks targeted questions to surface missing achievements, and updates it.
    Returns Claude's next question or confirmation that the resume was updated.
    """
    user = _require_user(authorization)
    admin = get_admin_client()
    profile = _get_profile(admin, str(user.id))

    # Get current master resume
    master_result = admin.table("master_resumes").select("content").eq("user_id", str(user.id)).execute()
    master_content = master_result.data[0]["content"] if master_result.data else ""

    system_prompt = f"""You are a career coach and expert resume writer helping {profile.get('full_name', 'the user')} improve their master resume.

Your job:
1. Review their master resume carefully for gaps, vague bullet points, or missing context
2. Ask ONE targeted question at a time to surface real achievements, metrics, or missing experience
3. When the user answers, incorporate their answer into the master resume and confirm the update
4. Then either ask another gap-filling question OR tell them their resume is strong and no more questions are needed

Good questions to ask:
- "You led X — can you give me a specific metric? (e.g., cost saved, revenue driven, % improvement)"
- "I see you worked at X but no dates — what years were you there?"
- "Your skills list mentions Y — any projects where you directly applied that?"
- "Any awards, recognitions, or promotions not captured here?"

If the user provides new information, output it in this format at the END of your reply (this will be parsed automatically):
UPDATE_MASTER_RESUME:
<full updated master resume text here>
END_UPDATE

Ask ONE question at a time. Be conversational, not clinical. Be specific to their actual resume — don't ask generic questions.

Current master resume:
{master_content or "(No master resume yet — ask them to upload files first)"}"""

    # Trim history to last N turns to avoid context overflow
    trimmed_history = body.history[-MAX_HISTORY_TURNS:]
    messages = trimmed_history + [{"role": "user", "content": body.message}]

    try:
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    reply = response.content[0].text

    # Check if Claude included a master resume update
    # Use .find() instead of .index() to avoid ValueError if markers are missing
    updated_master = None
    update_start = reply.find("UPDATE_MASTER_RESUME:")
    update_end = reply.find("END_UPDATE")
    if update_start != -1 and update_end != -1 and update_end > update_start:
        content_start = update_start + len("UPDATE_MASTER_RESUME:")
        updated_master = reply[content_start:update_end].strip()

        # Save updated master
        existing = admin.table("master_resumes").select("id").eq("user_id", str(user.id)).execute()
        if existing.data:
            admin.table("master_resumes").update({
                "content": updated_master,
                "last_updated": "now()"
            }).eq("user_id", str(user.id)).execute()
        else:
            admin.table("master_resumes").insert({
                "user_id": str(user.id),
                "content": updated_master
            }).execute()

        # Strip the update block from the visible reply
        visible_reply = reply[:update_start].strip()
    else:
        visible_reply = reply

    return {
        "reply": visible_reply,
        "master_updated": updated_master is not None
    }
