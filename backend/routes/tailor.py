"""
Tailor routes: generate a tailored resume from master + JD, save history, download PDF.
Also supports: fetching a JD from a URL, and inline refinement chat on a tailored resume.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
import json as _json
import logging
import traceback

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
import asyncio
import re
import json
import time
import ipaddress
import socket
import uuid
import httpx
import anthropic
from html.parser import HTMLParser
from urllib.parse import urlparse
from dependencies.auth import require_user, AuthContext
from services.supabase_client import get_client, get_admin_client
from services import claude as claude_service
from services.resume_parser import text_to_resume_data
from renderers.registry import get_renderer
from config import CLAUDE_MODEL
from limiter import limiter

router = APIRouter(prefix="/api/tailor", tags=["tailor"])
ai_client = claude_service.client  # shared Anthropic client — no duplicate connection pool

MAX_JD_LENGTH = 12_000   # ~3,000 tokens
MAX_HISTORY_TURNS = 20
_API_TIMEOUT = claude_service.API_TIMEOUT

# Limit concurrent LibreOffice processes to 1.
# LibreOffice is CPU-heavy; on Render's free tier (single vCPU, ~512 MB RAM)
# two simultaneous conversions cause OOM. The Semaphore ensures only one
# PDF render runs at a time — others wait in the async queue rather than
# spawning a second LibreOffice process.
_pdf_semaphore = asyncio.Semaphore(1)


def _safe_filename_part(value: str, fallback: str) -> str:
    # Use `[ ]` (literal space) not `\s` — \s matches tabs/newlines which
    # would survive the strip() and appear in filenames.
    sanitized = re.sub(r"[^\w \-]", "", value or "").strip().replace(" ", "_")
    return sanitized[:80] or fallback


# ── HTML stripping ────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    """Strip HTML tags and return visible text only.

    Skips content inside <script>, <style>, <nav>, <header>, <footer>,
    and <noscript> tags — these contain code/chrome, not job description text.
    """
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "aside"}

    def __init__(self):
        super().__init__()
        self.reset()
        self._fed: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, d: str):
        if self._skip_depth == 0:
            self._fed.append(d)

    def get_data(self) -> str:
        return " ".join(self._fed)


def _strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    text = stripper.get_data()
    return re.sub(r"\s+", " ", text).strip()


def _extract_jsonld_job(html: str) -> str:
    """
    Try to pull structured job data from JSON-LD schema.org/JobPosting blocks.

    Many ATS platforms (Greenhouse, Lever, Workday, Jobvite) embed the full
    job description in a <script type="application/ld+json"> block even when
    the visible page is client-rendered. This lets us bypass the JS problem.
    """
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL
    )
    for match in pattern.finditer(html):
        try:
            data = json.loads(match.group(1).strip())
            # May be a single object or a @graph / list — search for a JobPosting
            if isinstance(data, list):
                # Many ATS platforms wrap multiple types in an array; find the job
                data = next(
                    (d for d in data
                     if isinstance(d, dict) and d.get("@type") in ("JobPosting", "jobPosting")),
                    None
                )
                if data is None:
                    continue
            if not isinstance(data, dict):
                continue
            if data.get("@type") not in ("JobPosting", "jobPosting"):
                continue

            parts: list[str] = []
            if data.get("title"):
                parts.append(f"Job Title: {data['title']}")
            if isinstance(data.get("hiringOrganization"), dict):
                org_name = data["hiringOrganization"].get("name")
                if org_name:
                    parts.append(f"Company: {org_name}")
            if data.get("description"):
                # Description may itself contain HTML — strip it
                desc_clean = _strip_html(data["description"])
                parts.append(desc_clean)
            if data.get("qualifications"):
                parts.append(f"Qualifications: {_strip_html(str(data['qualifications']))}")
            if data.get("responsibilities"):
                parts.append(f"Responsibilities: {_strip_html(str(data['responsibilities']))}")
            if parts:
                return "\n\n".join(parts)
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            continue
    return ""


# ── Models ────────────────────────────────────────────────────────────────────

class TailorRequest(BaseModel):
    job_description: str = Field(..., max_length=MAX_JD_LENGTH)
    job_title: Optional[str] = Field("", max_length=200)
    company: Optional[str] = Field("", max_length=200)
    max_roles: int = Field(3, ge=1, le=10, description="Max EXPERIENCE roles to include (default 3). Raise if the user explicitly asks for more.")


class FetchJDRequest(BaseModel):
    url: str = Field(..., max_length=2000)


class HistoryMessage(BaseModel):
    """
    A single turn in the refine-chat conversation.

    Restricts `role` to the two values Claude actually accepts as conversation
    turns.  Rejects "system", "tool", and arbitrary dict shapes — closing the
    prompt-injection surface where a caller could inject a system-level message
    into the conversation history forwarded to Claude.
    """
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=20000)


class RefineMessage(BaseModel):
    message: str = Field(..., max_length=20000)
    history: list[HistoryMessage] = Field(default=[], max_length=40)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/")
@limiter.limit("10/minute")
def tailor_resume(request: Request, body: TailorRequest, ctx: AuthContext = Depends(require_user)):
    """Tailor the master resume to a JD. Saves to history."""
    logger.info("[tailor] START  user=%s  company=%r  job_title=%r  jd_len=%d  max_roles=%d",
                ctx.user.id, body.company, body.job_title, len(body.job_description), body.max_roles)
    db = get_client(ctx.token)
    admin = get_admin_client()

    master_result = db.table("master_resumes").select("content").eq("user_id", str(ctx.user.id)).execute()
    if not master_result.data or not master_result.data[0]["content"]:
        logger.warning("[tailor] 400 no master resume  user=%s", ctx.user.id)
        raise HTTPException(status_code=400, detail="No master resume found. Upload files and synthesize first.")

    master_content = master_result.data[0]["content"]
    logger.debug("[tailor] master resume loaded  user=%s  chars=%d", ctx.user.id, len(master_content))

    profile_result = db.table("profiles").select("*").eq("id", str(ctx.user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    logger.info("[tailor] calling Claude  user=%s", ctx.user.id)
    try:
        t0 = time.monotonic()
        tailored_text = claude_service.tailor_resume(
            master_resume=master_content,
            job_description=body.job_description,
            profile=profile,
            job_title=body.job_title or "",
            company=body.company or "",
            max_roles=body.max_roles,
        )
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("[tailor] Claude OK  user=%s  output_chars=%d  ms=%d", ctx.user.id, len(tailored_text), ms)
    except anthropic.APITimeoutError:
        logger.error("[tailor] 504 Claude timeout  user=%s  company=%r  job_title=%r", ctx.user.id, body.company, body.job_title)
        raise HTTPException(status_code=504, detail="AI request timed out. Please try again.")
    except Exception as e:
        logger.error("[tailor] 502 Claude error  user=%s  error=%s", ctx.user.id, e)
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    # Use admin client for the insert — RLS insert policy may require service role
    insert_result = admin.table("tailored_resumes").insert({
        "user_id": str(ctx.user.id),
        "job_title": body.job_title,
        "company": body.company,
        "job_description": body.job_description,
        "tailored_content": tailored_text,
    }).execute()

    record_id = insert_result.data[0]["id"] if insert_result.data else None
    logger.info("[tailor] COMPLETE  user=%s  record_id=%s  company=%r  job_title=%r",
                ctx.user.id, record_id, body.company, body.job_title)

    return {
        "id": record_id,
        "tailored_content": tailored_text,
        "job_title": body.job_title,
        "company": body.company,
    }


def _validate_fetch_url(url: str):
    """Block SSRF: reject non-http(s) schemes, private IPs, and loopback addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Only http/https URLs are allowed.")
    hostname = parsed.hostname or ""
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL — no hostname found.")
    # Reject known internal hostnames
    blocked_hosts = {"localhost", "metadata.google.internal"}
    if hostname.lower() in blocked_hosts:
        raise HTTPException(status_code=400, detail="Internal URLs are not allowed.")
    # Resolve and block private/loopback IP ranges
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(status_code=400, detail="Internal URLs are not allowed.")
    except HTTPException:
        raise
    except Exception:
        pass  # DNS failure will be caught by httpx below


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@router.post("/fetch-jd")
@limiter.limit("30/minute")
async def fetch_jd(request: Request, body: FetchJDRequest, ctx: AuthContext = Depends(require_user)):
    """Fetch and extract plain text from a job posting URL.

    Strategy (in order):
    1. Fetch raw HTML with a browser-like User-Agent via async httpx (non-blocking).
    2. Try JSON-LD schema.org/JobPosting extraction — works for Greenhouse,
       Lever, Workday, Jobvite even when the page is client-rendered.
    3. Fall back to full HTML stripping (works for Indeed, company career pages).
    4. Raise a clear, actionable error if content is still too short.
    """
    logger.info("[fetch-jd] START  user=%s  url=%r", ctx.user.id, body.url)
    # _validate_fetch_url calls socket.gethostbyname() which is a blocking DNS
    # lookup. Run it in a thread pool so it doesn't stall the event loop.
    await asyncio.to_thread(_validate_fetch_url, body.url)
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(
                body.url,
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            resp.raise_for_status()
            raw_html = resp.text

        # Re-validate the final URL after redirects — a redirect chain could
        # land on an internal service even if the original URL was public.
        # Note: this catches redirect-based SSRF but not DNS rebinding (the TCP
        # connection is already established by this point). A full DNS-pinning
        # fix would require a custom httpx transport.
        final_url = str(resp.url)
        if final_url != body.url:
            logger.info("[fetch-jd] followed redirect  user=%s  original=%r  final=%r",
                        ctx.user.id, body.url, final_url)
            await asyncio.to_thread(_validate_fetch_url, final_url)

        fetch_ms = int((time.monotonic() - t0) * 1000)
        logger.info("[fetch-jd] fetched  user=%s  url=%r  status=%d  html_len=%d  ms=%d",
                    ctx.user.id, body.url, resp.status_code, len(raw_html), fetch_ms)

        # Strategy 1: JSON-LD structured data (handles JS-rendered ATS platforms)
        text = _extract_jsonld_job(raw_html)
        if len(text) >= 100:
            logger.info("[fetch-jd] extracted via JSON-LD  user=%s  chars=%d", ctx.user.id, len(text))
        else:
            # Strategy 2: Full HTML strip (server-rendered pages, company career sites)
            text = _strip_html(raw_html)
            logger.info("[fetch-jd] extracted via HTML strip  user=%s  chars=%d", ctx.user.id, len(text))

        if len(text) < 100:
            logger.warning("[fetch-jd] 400 extracted text too short  user=%s  url=%r  chars=%d",
                           ctx.user.id, body.url, len(text))
            raise HTTPException(
                status_code=400,
                detail=(
                    "Couldn't extract job content from that URL. "
                    "This usually means the page requires JavaScript or a login. "
                    "Try copying the job description text and pasting it directly."
                )
            )
        logger.info("[fetch-jd] COMPLETE  user=%s  final_chars=%d", ctx.user.id, min(len(text), MAX_JD_LENGTH))
        return {"text": text[:MAX_JD_LENGTH]}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        logger.warning("[fetch-jd] 400 timeout  user=%s  url=%r", ctx.user.id, body.url)
        raise HTTPException(status_code=400, detail="The page took too long to respond. Try pasting the job description text instead.")
    except httpx.HTTPStatusError as e:
        http_status = e.response.status_code
        logger.warning("[fetch-jd] 400 HTTP error  user=%s  url=%r  upstream_status=%d", ctx.user.id, body.url, http_status)
        if http_status == 403:
            raise HTTPException(status_code=400, detail="The site blocked the request (403 Forbidden). Paste the job description text instead.")
        raise HTTPException(status_code=400, detail=f"The site returned an error ({http_status}). Try pasting the text directly.")
    except Exception as e:
        logger.error("[fetch-jd] 400 unexpected error  user=%s  url=%r  error=%s", ctx.user.id, body.url, e)
        raise HTTPException(status_code=400, detail=f"Could not fetch URL: {str(e)}")


@router.post("/stream")
@limiter.limit("10/minute")
async def stream_tailor(request: Request, body: TailorRequest, ctx: AuthContext = Depends(require_user)):
    """
    Streaming variant of POST /api/tailor/.

    Returns an SSE stream of text chunks so the frontend can render the
    resume progressively instead of waiting 10-30s for a blocking response.
    Uses an async generator so the event loop is never blocked (TD-17).

    SSE event format:
        data: {"chunk": "text"}\n\n      — partial resume text
        data: {"done": true, "id": "…"}\n\n — stream complete, DB record ID included
        data: {"error": "msg"}\n\n        — Claude API error mid-stream
    """
    logger.info("[stream-tailor] START  user=%s  company=%r  job_title=%r  jd_len=%d  max_roles=%d",
                ctx.user.id, body.company, body.job_title, len(body.job_description), body.max_roles)
    db = get_client(ctx.token)
    admin = get_admin_client()

    # Wrap sync Supabase calls in to_thread() so they don't block the event loop (TD-17).
    master_result = await asyncio.to_thread(
        lambda: db.table("master_resumes").select("content").eq("user_id", str(ctx.user.id)).execute()
    )
    if not master_result.data or not master_result.data[0]["content"]:
        logger.warning("[stream-tailor] 400 no master resume  user=%s", ctx.user.id)
        raise HTTPException(status_code=400, detail="No master resume found. Upload files and synthesize first.")

    master_content = master_result.data[0]["content"]
    logger.debug("[stream-tailor] master resume loaded  user=%s  chars=%d", ctx.user.id, len(master_content))
    profile_result = await asyncio.to_thread(
        lambda: db.table("profiles").select("*").eq("id", str(ctx.user.id)).execute()
    )
    profile = profile_result.data[0] if profile_result.data else {}
    logger.debug("[stream-tailor] profile loaded  user=%s  has_profile=%s", ctx.user.id, bool(profile))

    # Capture local refs so the async generator closure doesn't hold the full request scope
    user_id   = str(ctx.user.id)
    job_title = body.job_title
    company   = body.company
    job_desc  = body.job_description
    max_roles = body.max_roles

    async def _generate():
        full_chunks: list[str] = []
        logger.info("[stream-tailor] Claude stream starting  user=%s", user_id)
        t0_stream = time.monotonic()
        try:
            async for chunk in claude_service.stream_tailor_resume_async(
                master_resume=master_content,
                job_description=job_desc,
                profile=profile,
                job_title=job_title or "",
                company=company or "",
                max_roles=max_roles,
            ):
                full_chunks.append(chunk)
                yield f"data: {_json.dumps({'chunk': chunk})}\n\n"
        except anthropic.APITimeoutError:
            logger.error("[stream-tailor] Claude timeout mid-stream  user=%s  chunks_so_far=%d",
                         user_id, len(full_chunks))
            yield f"data: {_json.dumps({'error': 'AI request timed out. Please try again.'})}\n\n"
            return
        except Exception as e:
            logger.error("[stream-tailor] Claude stream error  user=%s  chunks_so_far=%d  error=%s",
                         user_id, len(full_chunks), e)
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
            return

        stream_ms = int((time.monotonic() - t0_stream) * 1000)
        total_chars = sum(len(c) for c in full_chunks)
        logger.info("[stream-tailor] Claude stream complete  user=%s  chunks=%d  total_chars=%d  ms=%d",
                    user_id, len(full_chunks), total_chars, stream_ms)

        # Save completed resume to DB after streaming finishes.
        # asyncio.to_thread keeps the sync Supabase call off the event loop (TD-17).
        # Client-disconnect note: if the SSE client disconnects before all chunks
        # are yielded, Starlette may cancel this generator — the insert below will
        # not run and the tailored resume will not be saved.  This is acceptable
        # for a streaming endpoint; the user can re-trigger the stream to get a
        # fresh record.
        tailored_text = "".join(full_chunks)
        try:
            # asyncio.shield() prevents the DB insert from being cancelled if the
            # SSE client disconnects before this point. Without shield, a client
            # navigating away mid-stream would cancel the generator and silently
            # lose the completed resume from history.
            insert_result = await asyncio.shield(asyncio.to_thread(
                lambda: admin.table("tailored_resumes").insert({
                    "user_id":          user_id,
                    "job_title":        job_title,
                    "company":          company,
                    "job_description":  job_desc,
                    "tailored_content": tailored_text,
                }).execute()
            ))
            record_id = insert_result.data[0]["id"] if insert_result.data else None
            logger.info("[stream-tailor] DB insert OK  user=%s  record_id=%s  company=%r  job_title=%r",
                        user_id, record_id, company, job_title)
        except Exception as e:
            logger.error("[stream-tailor] DB insert FAILED (record lost)  user=%s  error=%s", user_id, e)
            record_id = None

        yield f"data: {_json.dumps({'done': True, 'id': record_id})}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx proxy buffering
        },
    )


# NOTE: /history must be before /{record_id}/... to prevent FastAPI matching
# the literal string "history" as a record_id path parameter.
@router.get("/history")
@limiter.limit("60/minute")
def get_history(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(require_user),
):
    """
    Return paginated tailored resume records for the user.

    Query params:
        limit  — page size (default 50, max 200)
        offset — number of records to skip (default 0)

    Response:
        {
            "items":    [...],   # records for this page
            "total":    N,       # total record count for the user
            "limit":    50,
            "offset":   0,
            "has_more": true     # true if more records exist beyond this page
        }
    """
    limit = min(max(1, limit), 200)   # clamp: 1 ≤ limit ≤ 200
    offset = max(0, offset)
    logger.info("[history] START  user=%s  limit=%d  offset=%d", ctx.user.id, limit, offset)

    db = get_client(ctx.token)

    # Get total count (separate query — Supabase returns count alongside data
    # only when count="exact" is passed; doing it separately keeps the query readable).
    count_result = db.table("tailored_resumes") \
        .select("id", count="exact") \
        .eq("user_id", str(ctx.user.id)) \
        .execute()
    # Use isinstance so test mocks (MagicMock, not None) don't slip past the guard.
    total = count_result.count if isinstance(count_result.count, int) else 0
    logger.debug("[history] count query returned total=%d  user=%s", total, ctx.user.id)

    result = db.table("tailored_resumes") \
        .select("id, job_title, company, created_at") \
        .eq("user_id", str(ctx.user.id)) \
        .order("created_at", desc=True) \
        .range(offset, offset + limit - 1) \
        .execute()

    # If count came back stale/zero but we still got items, reconcile so the
    # frontend's has_more / "Showing X of Y" pagination math stays sane.
    items = result.data or []
    seen = offset + len(items)
    if seen > total:
        total = seen

    has_more = seen < total
    logger.info("[history] COMPLETE  user=%s  items=%d  total=%d  has_more=%s  offset=%d",
                ctx.user.id, len(items), total, has_more, offset)

    return {
        "items":    items,
        "total":    total,
        "limit":    limit,
        "offset":   offset,
        "has_more": has_more,
    }


@router.post("/{record_id}/refine")
@limiter.limit("20/minute")
def refine_tailored(
    request: Request,
    record_id: uuid.UUID,   # FastAPI validates and returns 422 for non-UUID input (TD-03)
    body: RefineMessage,
    ctx: AuthContext = Depends(require_user),
):
    """
    Inline refinement chat for a specific tailored resume.
    Claude asks targeted questions and rewrites the resume when the user provides new info.
    """
    logger.info("[refine] START  user=%s  record_id=%s  msg_len=%d  history_turns=%d",
                ctx.user.id, record_id, len(body.message), len(body.history))
    db = get_client(ctx.token)
    admin = get_admin_client()

    result = db.table("tailored_resumes") \
        .select("*") \
        .eq("id", str(record_id)) \
        .eq("user_id", str(ctx.user.id)) \
        .execute()
    if not result.data:
        logger.warning("[refine] 404 record not found  user=%s  record_id=%s", ctx.user.id, record_id)
        raise HTTPException(status_code=404, detail="Tailored resume not found")

    record = result.data[0]
    job_title_str = (record.get("job_title") or "").strip()
    company_str   = (record.get("company") or "").strip()
    logger.debug("[refine] record loaded  user=%s  record_id=%s  job_title=%r  company=%r",
                 ctx.user.id, record_id, job_title_str, company_str)

    profile_result = db.table("profiles").select("*").eq("id", str(ctx.user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    # Fetch master resume so Claude has full career context without the user having to paste it
    master_result = db.table("master_resumes").select("content").eq("user_id", str(ctx.user.id)).execute()
    master_content = master_result.data[0].get("content", "") if master_result.data else ""
    logger.debug("[refine] master resume loaded  user=%s  master_chars=%d", ctx.user.id, len(master_content))

    job_title = job_title_str
    company   = company_str
    if job_title and company:
        role_label = f"{job_title} at {company}"
    elif job_title:
        role_label = job_title
    elif company:
        role_label = f"the role at {company}"
    else:
        role_label = "this role"

    master_section = f"\nMaster resume (full career history for reference):\n{master_content[:12000]}\n" if master_content else ""

    system_prompt = f"""You are a resume coach helping {profile.get('full_name', 'the user')} refine their tailored resume for {role_label}.

Job Description:
{(record.get('job_description') or '')[:3000]}

Current tailored resume:
{record.get('tailored_content') or ''}
{master_section}
Your job:
1. Start by identifying the single biggest gap between this resume and the job description
2. Ask ONE targeted question at a time to surface better metrics, achievements, or alignment
3. When the user answers, incorporate their answer into the full tailored resume and confirm what changed
4. Then ask another sharpening question OR confirm the resume is strong

Focus on: missing metrics, weak verbs, skills the JD emphasizes that aren't prominent, or summary alignment.
You already have the user's full master resume above — never ask them to paste or upload it.

FILE UPLOAD WORKFLOW: The user can attach files using the paperclip button. When they say they "attached", "uploaded", or "just attached" something, it means they uploaded a file to their resume library and the master resume was automatically re-synthesized with that content. You won't see the file directly in chat — the new content is already incorporated into the master resume above. When this happens, acknowledge it and check the master resume for the new information rather than asking them to paste content.

If you produce an improved resume, output it at the END of your reply in this exact format:
UPDATE_TAILORED_RESUME:
<full updated resume text here>
END_UPDATE

Ask ONE question at a time. Be specific to this role and resume — not generic."""

    # Convert typed HistoryMessage models to plain dicts for the Anthropic SDK.
    # Slicing after validation ensures only role/content keys are forwarded.
    trimmed_history = [m.model_dump() for m in body.history[-MAX_HISTORY_TURNS:]]
    messages = trimmed_history + [{"role": "user", "content": body.message}]

    logger.info("[refine] calling Claude  user=%s  record_id=%s  total_messages=%d",
                ctx.user.id, record_id, len(messages))
    try:
        t0 = time.monotonic()
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            system=system_prompt,
            messages=messages,
            timeout=_API_TIMEOUT,
        )
        ms = int((time.monotonic() - t0) * 1000)
        logger.info("[refine] Claude OK  user=%s  record_id=%s  reply_chars=%d  ms=%d",
                    ctx.user.id, record_id, len(response.content[0].text), ms)
    except anthropic.APITimeoutError:
        logger.error("[refine] 504 Claude timeout  user=%s  record_id=%s", ctx.user.id, record_id)
        raise HTTPException(status_code=504, detail="AI request timed out. Please try again.")
    except Exception as e:
        logger.error("[refine] 502 Claude error  user=%s  record_id=%s  error=%s", ctx.user.id, record_id, e)
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    reply = response.content[0].text

    updated_content = None
    update_start = reply.find("UPDATE_TAILORED_RESUME:")
    update_end = reply.find("END_UPDATE")
    if update_start != -1 and update_end != -1 and update_end > update_start:
        content_start = update_start + len("UPDATE_TAILORED_RESUME:")
        updated_content = reply[content_start:update_end].strip()
        admin.table("tailored_resumes").update({
            "tailored_content": updated_content
        }).eq("id", str(record_id)).eq("user_id", str(ctx.user.id)).execute()
        visible_reply = reply[:update_start].strip()
        logger.info("[refine] resume updated in DB  user=%s  record_id=%s  updated_chars=%d",
                    ctx.user.id, record_id, len(updated_content))
    else:
        visible_reply = reply
        logger.debug("[refine] no UPDATE block in reply — resume unchanged  user=%s  record_id=%s",
                     ctx.user.id, record_id)

    logger.info("[refine] COMPLETE  user=%s  record_id=%s  reply_chars=%d  updated=%s",
                ctx.user.id, record_id, len(visible_reply), updated_content is not None)
    return {
        "reply": visible_reply,
        "updated_content": updated_content,
    }


@router.api_route("/{record_id}/pdf", methods=["GET", "HEAD"])
@router.api_route("/{record_id}/pdf/{filename}", methods=["GET", "HEAD"])
@limiter.limit("60/minute")
async def download_pdf(
    request: Request,
    record_id: uuid.UUID,   # FastAPI validates and returns 422 for non-UUID input (TD-03)
    filename: str = "",     # ignored server-side — present so Chrome reads it from the URL path
    ctx: AuthContext = Depends(require_user),
):
    """Generate and return a PDF for a tailored resume record.

    Accepts both GET and HEAD.  The frontend sends HEAD first to validate auth
    and reachability without triggering LibreOffice, then fires a direct anchor
    click for the real GET.  This avoids Chrome's ~5-second user-activation
    window that caused blob-URL downloads to save with UUID filenames.

    Renders via the FDE DOCX renderer (LibreOffice headless → PDF).
    Rate-limited because LibreOffice is CPU-heavy and a single user could
    otherwise spike the Render free instance by hammering this endpoint.
    """
    logger.info(f"[download_pdf] START  method={request.method}  record_id={record_id}  user={ctx.user.id}")

    logger.info(f"[download_pdf] querying tailored_resumes for record_id={record_id}")
    try:
        db = get_client(ctx.token)
        result = await asyncio.to_thread(
            lambda: db.table("tailored_resumes")
                .select("*")
                .eq("id", str(record_id))
                .eq("user_id", str(ctx.user.id))
                .execute()
        )
        logger.info(f"[download_pdf] DB query returned {len(result.data)} row(s)")
    except Exception as e:
        logger.error(f"[download_pdf] DB query FAILED: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if not result.data:
        logger.warning(f"[download_pdf] 404 — no record found for record_id={record_id} user={ctx.user.id}")
        raise HTTPException(status_code=404, detail="Tailored resume not found")

    record = result.data[0]
    logger.info(f"[download_pdf] record found: company={record.get('company')!r}  job_title={record.get('job_title')!r}")

    company = _safe_filename_part(record.get("company", ""), "tailored")
    role = _safe_filename_part(record.get("job_title", ""), "resume")
    filename = f"{company}_{role}.pdf"
    disposition = f"attachment; filename=\"{filename}\""
    logger.info(f"[download_pdf] filename resolved to: {filename}")

    # HEAD: validate ownership + return headers only — skip LibreOffice.
    if request.method == "HEAD":
        logger.info(f"[download_pdf] HEAD — returning 200 with headers only")
        return Response(
            content=b"",
            media_type="application/pdf",
            headers={"Content-Disposition": disposition},
        )

    logger.info(f"[download_pdf] GET — fetching profile for user={ctx.user.id}")
    try:
        profile_result = await asyncio.to_thread(
            lambda: db.table("profiles").select("*").eq("id", str(ctx.user.id)).execute()
        )
        profile = profile_result.data[0] if profile_result.data else {}
        logger.info(f"[download_pdf] profile fetched: has_data={bool(profile_result.data)}")
    except Exception as e:
        logger.error(f"[download_pdf] profile fetch FAILED: {e}\n{traceback.format_exc()}")
        profile = {}

    logger.info(f"[download_pdf] parsing tailored_content into resume_data")
    try:
        resume_data = await asyncio.to_thread(
            text_to_resume_data, record["tailored_content"], profile
        )
        logger.info(f"[download_pdf] resume_data parsed OK")
    except Exception as e:
        logger.error(f"[download_pdf] text_to_resume_data FAILED: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Resume parse failed: {str(e)}")

    logger.info(f"[download_pdf] calling renderer (LibreOffice) — acquiring semaphore")
    try:
        async with _pdf_semaphore:
            logger.info(f"[download_pdf] semaphore acquired — starting LibreOffice")
            pdf_bytes = await asyncio.to_thread(get_renderer().render, resume_data)
        logger.info(f"[download_pdf] renderer returned {len(pdf_bytes)} bytes")
    except Exception as e:
        logger.error(f"[download_pdf] renderer FAILED: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    logger.info(f"[download_pdf] returning PDF response  filename={filename}  size={len(pdf_bytes)}")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )
