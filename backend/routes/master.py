"""
Master resume routes: synthesize, get, and update via gap-filling chat.
"""
import logging
import time
import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
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
    logger.debug("[master-get] user=%s", ctx.user.id)
    db = get_client(ctx.token)
    result = db.table("master_resumes").select("*").eq("user_id", str(ctx.user.id)).execute()
    if not result.data:
        logger.debug("[master-get] no master resume found  user=%s", ctx.user.id)
        return {"content": None, "last_updated": None}
    chars = len(result.data[0].get("content") or "")
    logger.debug("[master-get] found  user=%s  chars=%d", ctx.user.id, chars)
    return result.data[0]


@router.get("/download")
@router.get("/download/{filename}")   # filename in URL path — Chrome derives save-as name from it
@limiter.limit("30/minute")
def download_master_resume(
    request: Request,
    filename: str = "",               # ignored server-side; present so Chrome reads it from the URL
    ctx: AuthContext = Depends(require_user),
):
    """Download the current master resume as a plain-text .txt file.

    The filename is embedded as the last URL segment (/download/Master_Resume.txt)
    so Chrome derives the save-as name from the path directly — Content-Disposition
    is stripped by BaseHTTPMiddleware before it reaches the browser (same fix as PDF route).
    """
    logger.info("[master-download] user=%s", ctx.user.id)
    db = get_client(ctx.token)
    result = db.table("master_resumes").select("content").eq("user_id", str(ctx.user.id)).execute()
    if not result.data or not result.data[0].get("content"):
        raise HTTPException(status_code=404, detail="No master resume found. Upload files and synthesize first.")
    content = result.data[0]["content"]
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
    )


@router.post("/synthesize")
@limiter.limit("5/minute")
def synthesize_master(request: Request, ctx: AuthContext = Depends(require_user)):
    """
    Re-synthesize the master resume from all uploaded resume files.
    Call this after uploading new files.
    """
    logger.info("[synthesize] START  user=%s", ctx.user.id)
    db = get_client(ctx.token)
    admin = get_admin_client()

    # Pull all extracted texts
    files = db.table("resume_files") \
        .select("extracted_text, filename") \
        .eq("user_id", str(ctx.user.id)) \
        .execute()

    if not files.data:
        logger.warning("[synthesize] 400 no resume files  user=%s", ctx.user.id)
        raise HTTPException(status_code=400, detail="No resume files uploaded yet. Upload at least one file first.")

    texts = [f["extracted_text"] for f in files.data if f.get("extracted_text")]
    logger.info("[synthesize] found %d file(s) with extracted text (of %d total)  user=%s",
                len(texts), len(files.data), ctx.user.id)
    if not texts:
        logger.warning("[synthesize] 400 no extracted text in any file  user=%s", ctx.user.id)
        raise HTTPException(status_code=400, detail="Could not extract text from uploaded files.")

    profile = _get_profile(db, str(ctx.user.id))
    logger.debug("[synthesize] profile loaded  user=%s  has_profile=%s", ctx.user.id, bool(profile))

    # Synthesize via Claude
    logger.info("[synthesize] calling Claude  user=%s  file_count=%d", ctx.user.id, len(texts))
    try:
        t0 = time.monotonic()
        master_content = claude_service.synthesize_master_resume(texts, profile)
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("[synthesize] Claude OK  user=%s  output_chars=%d  ms=%d",
                    ctx.user.id, len(master_content), ms)
    except anthropic.APITimeoutError:
        logger.error("[synthesize] 504 Claude timeout  user=%s", ctx.user.id)
        raise HTTPException(status_code=504, detail="AI request timed out. Please try again.")
    except Exception as e:
        logger.error("[synthesize] 502 Claude error  user=%s  error=%s", ctx.user.id, e, exc_info=True)
        raise HTTPException(status_code=502, detail="The AI service had an error. Please try again.")

    # Upsert master resume — single round trip, no TOCTOU race if two requests
    # hit synthesize concurrently (both would have hit the empty-branch and tried
    # to insert, with one failing on the unique constraint).
    logger.info("[synthesize] upserting to DB  user=%s  chars=%d", ctx.user.id, len(master_content))
    try:
        upsert_result = admin.table("master_resumes").upsert({
            "user_id":      str(ctx.user.id),
            "content":      master_content,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id").execute()
        rows_affected = len(upsert_result.data) if upsert_result.data else 0
        logger.info("[synthesize] DB upsert OK  user=%s  rows=%d", ctx.user.id, rows_affected)
    except Exception as e:
        logger.error("[synthesize] DB upsert FAILED  user=%s  error=%s", ctx.user.id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save master resume. Please try again.")

    logger.info("[synthesize] COMPLETE  user=%s", ctx.user.id)
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
    logger.info("[gap-fill] START  user=%s  msg_len=%d  history_turns=%d",
                ctx.user.id, len(body.message), len(body.history))
    db = get_client(ctx.token)
    admin = get_admin_client()
    profile = _get_profile(db, str(ctx.user.id))
    logger.debug("[gap-fill] profile loaded  user=%s  has_profile=%s", ctx.user.id, bool(profile))

    # Get current master resume
    master_result = db.table("master_resumes").select("content").eq("user_id", str(ctx.user.id)).execute()
    master_content = master_result.data[0]["content"] if master_result.data else ""
    logger.debug("[gap-fill] master resume loaded  user=%s  chars=%d", ctx.user.id, len(master_content))

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

CRITICAL — when you output an UPDATE block:
- Do NOT paste, repeat, or preview any part of the resume text in your visible reply
- Just confirm what changed in 1-2 short sentences (e.g. "Got it — I've added that $298K DCAS win to your UPS role.")
- Then immediately ask your next question
- The user can download the master resume any time; they don't need to see it in chat

Ask ONE question at a time. Be conversational, not clinical. Be specific to their actual resume — don't ask generic questions.

Current master resume:
{master_content or "(No master resume yet — ask them to upload files first)"}"""

    # Trim history to last N turns to avoid context overflow.
    # Convert typed models to plain dicts for the Anthropic SDK — slicing after
    # validation guarantees only role/content keys are forwarded.
    trimmed_history = [m.model_dump() for m in body.history[-MAX_HISTORY_TURNS:]]
    messages = trimmed_history + [{"role": "user", "content": body.message}]

    logger.info("[gap-fill] calling Claude  user=%s  total_messages=%d", ctx.user.id, len(messages))
    t0 = time.monotonic()
    try:
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,  # raised from 2000 — full master resume in UPDATE block can exceed 1500 tokens
            system=system_prompt,
            messages=messages,
            timeout=claude_service.API_TIMEOUT,
        )
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("[gap-fill] Claude OK  user=%s  input_tokens=%d  output_tokens=%d  ms=%d",
                    ctx.user.id, response.usage.input_tokens, response.usage.output_tokens, ms)
    except anthropic.APITimeoutError:
        logger.error("[gap-fill] 504 Claude timeout  user=%s", ctx.user.id)
        raise HTTPException(status_code=504, detail="AI request timed out. Please try again.")
    except Exception as e:
        logger.error("[gap-fill] 502 Claude error  user=%s  error=%s", ctx.user.id, e, exc_info=True)
        raise HTTPException(status_code=502, detail="The AI service had an error. Please try again.")

    reply = response.content[0].text
    # Log whether output was truncated — if output_tokens == max_tokens the reply
    # was cut mid-sentence and the END_UPDATE marker may be missing
    if response.usage.output_tokens >= 4000:
        logger.warning(
            "[gap-fill] output hit max_tokens — reply likely truncated  "
            "user=%s  output_tokens=%d  reply_chars=%d  reply_tail=%r",
            ctx.user.id, response.usage.output_tokens, len(reply), reply[-100:],
        )

    # Check if Claude included a master resume update
    updated_master = None
    update_start = reply.find("UPDATE_MASTER_RESUME:")
    update_end = reply.find("END_UPDATE")
    logger.info(
        "[gap-fill] UPDATE block scan  user=%s  reply_chars=%d  "
        "UPDATE_MASTER_RESUME_pos=%d  END_UPDATE_pos=%d",
        ctx.user.id, len(reply), update_start, update_end,
    )
    if update_start != -1 and update_end != -1 and update_end > update_start:
        content_start = update_start + len("UPDATE_MASTER_RESUME:")
        updated_master = reply[content_start:update_end].strip()
        logger.info(
            "[gap-fill] UPDATE block found  user=%s  updated_chars=%d",
            ctx.user.id, len(updated_master),
        )

        # Save updated master — single upsert avoids TOCTOU race
        try:
            upsert_result = admin.table("master_resumes").upsert({
                "user_id":      str(ctx.user.id),
                "content":      updated_master,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="user_id").execute()
            rows = len(upsert_result.data) if upsert_result.data else 0
            logger.info("[gap-fill] DB upsert OK  user=%s  rows=%d", ctx.user.id, rows)
        except Exception as e:
            logger.error("[gap-fill] DB upsert FAILED  user=%s  error=%s", ctx.user.id, e, exc_info=True)
            # Don't 500 — the reply is still useful even if the save failed

        # Strip the update block from the visible reply
        visible_reply = reply[:update_start].strip()
    else:
        visible_reply = reply
        if update_start == -1:
            logger.warning(
                "[gap-fill] no UPDATE_MASTER_RESUME marker in reply — master resume NOT updated  "
                "user=%s  reply_chars=%d  reply_head=%r",
                ctx.user.id, len(reply), reply[:120],
            )
        elif update_end == -1:
            logger.warning(
                "[gap-fill] UPDATE_MASTER_RESUME found but END_UPDATE missing — "
                "likely truncated  user=%s  update_start=%d  reply_chars=%d",
                ctx.user.id, update_start, len(reply),
            )
        elif update_end <= update_start:
            logger.warning(
                "[gap-fill] END_UPDATE appears BEFORE UPDATE_MASTER_RESUME — "
                "marker order wrong  user=%s  update_start=%d  update_end=%d",
                ctx.user.id, update_start, update_end,
            )

    logger.info("[gap-fill] COMPLETE  user=%s  reply_chars=%d  master_updated=%s",
                ctx.user.id, len(visible_reply), updated_master is not None)
    return {
        "reply": visible_reply,
        "master_updated": updated_master is not None
    }
