"""
Master resume routes: synthesize, get, and update via gap-filling chat.
"""
import logging
import time
import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime, timezone
from dependencies.auth import require_user, AuthContext
from services.supabase_client import get_client, get_admin_client
from services import claude as claude_service
from limiter import limiter
from config import CLAUDE_MODEL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/master", tags=["master"])
ai_client = claude_service.client  # shared Anthropic client — no duplicate connection pool


def _get_profile(db, user_id: str) -> dict:
    result = db.table("profiles").select("*").eq("id", user_id).execute()
    return result.data[0] if result.data else {}


@router.get("/")
@limiter.limit("60/minute")
def get_master_resume(request: Request, ctx: AuthContext = Depends(require_user)):
    """Get the current master resume for the user."""
    db = get_client(ctx.token)
    result = db.table("master_resumes").select("*").eq("user_id", str(ctx.user.id)).execute()
    if not result.data:
        return {"content": None, "last_updated": None}
    return result.data[0]


@router.post("/synthesize")
@limiter.limit("5/minute")
def synthesize_master(request: Request, ctx: AuthContext = Depends(require_user)):
    """
    Re-synthesize the master resume from all uploaded resume files.
    Call this after uploading new files.
    """
    db = get_client(ctx.token)
    admin = get_admin_client()

    # Pull all extracted texts
    files = db.table("resume_files") \
        .select("extracted_text, filename") \
        .eq("user_id", str(ctx.user.id)) \
        .execute()

    if not files.data:
        raise HTTPException(status_code=400, detail="No resume files uploaded yet. Upload at least one file first.")

    texts = [f["extracted_text"] for f in files.data if f.get("extracted_text")]
    if not texts:
        raise HTTPException(status_code=400, detail="Could not extract text from uploaded files.")

    profile = _get_profile(db, str(ctx.user.id))

    # Synthesize via Claude
    try:
        master_content = claude_service.synthesize_master_resume(texts, profile)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    # Upsert master resume — use admin client for upsert to avoid RLS insert policy issues
    existing = admin.table("master_resumes").select("id").eq("user_id", str(ctx.user.id)).execute()
    if existing.data:
        admin.table("master_resumes").update({
            "content": master_content,
            "last_updated": datetime.now(timezone.utc).isoformat()
        }).eq("user_id", str(ctx.user.id)).execute()
    else:
        admin.table("master_resumes").insert({
            "user_id": str(ctx.user.id),
            "content": master_content
        }).execute()

    return {"message": "Master resume synthesized", "preview": master_content[:500] + "..."}


MAX_HISTORY_TURNS = 20  # cap conversation history to prevent token bloat


class GapHistoryMessage(BaseModel):
    """
    A single turn in the gap-fill conversation.
    Mirrors HistoryMessage from tailor.py — Literal role closes the
    prompt-injection surface where a caller could inject role:"system"
    into the conversation array forwarded to Claude.
    """
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=20000)


class GapMessage(BaseModel):
    message: str = Field(..., max_length=20000)
    history: list[GapHistoryMessage] = Field(default=[], max_length=40)


@router.post("/gap-fill/chat")
@limiter.limit("20/minute")
def gap_fill_chat(request: Request, body: GapMessage, ctx: AuthContext = Depends(require_user)):
    """
    Conversational gap-filling. Claude reviews the master resume,
    asks targeted questions to surface missing achievements, and updates it.
    Returns Claude's next question or confirmation that the resume was updated.
    """
    db = get_client(ctx.token)
    admin = get_admin_client()
    profile = _get_profile(db, str(ctx.user.id))

    # Get current master resume
    master_result = db.table("master_resumes").select("content").eq("user_id", str(ctx.user.id)).execute()
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

    # Trim history to last N turns to avoid context overflow.
    # Convert typed models to plain dicts for the Anthropic SDK — slicing after
    # validation guarantees only role/content keys are forwarded.
    trimmed_history = [m.model_dump() for m in body.history[-MAX_HISTORY_TURNS:]]
    messages = trimmed_history + [{"role": "user", "content": body.message}]

    t0 = time.monotonic()
    try:
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=messages,
            timeout=claude_service.API_TIMEOUT,
        )
    except anthropic.APITimeoutError:
        raise HTTPException(status_code=504, detail="AI request timed out. Please try again.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")
    logger.info(
        "claude[gap-fill] user=%s input=%d output=%d ms=%d",
        str(ctx.user.id)[:8],
        response.usage.input_tokens,
        response.usage.output_tokens,
        int((time.monotonic() - t0) * 1000),
    )

    reply = response.content[0].text

    # Check if Claude included a master resume update
    updated_master = None
    update_start = reply.find("UPDATE_MASTER_RESUME:")
    update_end = reply.find("END_UPDATE")
    if update_start != -1 and update_end != -1 and update_end > update_start:
        content_start = update_start + len("UPDATE_MASTER_RESUME:")
        updated_master = reply[content_start:update_end].strip()

        # Save updated master — use admin client for upsert
        existing = admin.table("master_resumes").select("id").eq("user_id", str(ctx.user.id)).execute()
        if existing.data:
            admin.table("master_resumes").update({
                "content": updated_master,
                "last_updated": datetime.now(timezone.utc).isoformat()
            }).eq("user_id", str(ctx.user.id)).execute()
        else:
            admin.table("master_resumes").insert({
                "user_id": str(ctx.user.id),
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
