"""
Tailor routes: generate a tailored resume from master + JD, save history, download PDF.
Also supports: fetching a JD from a URL, and inline refinement chat on a tailored resume.
"""
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Optional
import re
import ipaddress
import socket
import requests as http_requests
from html.parser import HTMLParser
from urllib.parse import urlparse
from services.supabase_client import get_admin_client, get_user_from_token
from services import claude as claude_service
from services.pdf_generator import generate_pdf
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
import anthropic

router = APIRouter(prefix="/api/tailor", tags=["tailor"])
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_JD_LENGTH = 12_000   # ~3,000 tokens
MAX_HISTORY_TURNS = 20


# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_user(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _safe_filename_part(value: str, fallback: str) -> str:
    sanitized = re.sub(r"[^\w\s\-]", "", value or "").strip().replace(" ", "_")
    return sanitized[:80] or fallback


# ── HTML stripping ────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self._fed: list[str] = []

    def handle_data(self, d: str):
        self._fed.append(d)

    def get_data(self) -> str:
        return " ".join(self._fed)


def _strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    text = stripper.get_data()
    return re.sub(r"\s+", " ", text).strip()


# ── Models ────────────────────────────────────────────────────────────────────

class TailorRequest(BaseModel):
    job_description: str = Field(..., max_length=MAX_JD_LENGTH)
    job_title: Optional[str] = Field("", max_length=200)
    company: Optional[str] = Field("", max_length=200)


class FetchJDRequest(BaseModel):
    url: str = Field(..., max_length=2000)


class RefineMessage(BaseModel):
    message: str = Field(..., max_length=4000)
    history: list[dict] = []


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/")
def tailor_resume(body: TailorRequest, authorization: str = Header(None)):
    """Tailor the master resume to a JD. Saves to history."""
    user = _require_user(authorization)
    admin = get_admin_client()

    master_result = admin.table("master_resumes").select("content").eq("user_id", str(user.id)).execute()
    if not master_result.data or not master_result.data[0]["content"]:
        raise HTTPException(status_code=400, detail="No master resume found. Upload files and synthesize first.")

    master_content = master_result.data[0]["content"]

    profile_result = admin.table("profiles").select("*").eq("id", str(user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    try:
        tailored_text = claude_service.tailor_resume(
            master_resume=master_content,
            job_description=body.job_description,
            profile=profile,
            job_title=body.job_title or "",
            company=body.company or ""
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI service error: {str(e)}")

    insert_result = admin.table("tailored_resumes").insert({
        "user_id": str(user.id),
        "job_title": body.job_title,
        "company": body.company,
        "job_description": body.job_description,
        "tailored_content": tailored_text,
    }).execute()

    record_id = insert_result.data[0]["id"] if insert_result.data else None

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
        pass  # DNS failure will be caught by requests.get() below


@router.post("/fetch-jd")
def fetch_jd(body: FetchJDRequest, authorization: str = Header(None)):
    """Fetch and extract plain text from a job posting URL."""
    _require_user(authorization)
    _validate_fetch_url(body.url)
    try:
        resp = http_requests.get(
            body.url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResumeTailor/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        text = _strip_html(resp.text)
        if len(text) < 100:
            raise HTTPException(
                status_code=400,
                detail="Page content too short — the URL may require a login or JavaScript to load."
            )
        return {"text": text[:MAX_JD_LENGTH]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch URL: {str(e)}")


# NOTE: /history must be before /{record_id}/... to prevent FastAPI matching
# the literal string "history" as a record_id path parameter.
@router.get("/history")
def get_history(authorization: str = Header(None)):
    """Return all tailored resume records for the user."""
    user = _require_user(authorization)
    admin = get_admin_client()
    result = admin.table("tailored_resumes") \
        .select("id, job_title, company, created_at") \
        .eq("user_id", str(user.id)) \
        .order("created_at", desc=True) \
        .execute()
    return result.data


@router.post("/{record_id}/refine")
def refine_tailored(record_id: str, body: RefineMessage, authorization: str = Header(None)):
    """
    Inline refinement chat for a specific tailored resume.
    Claude asks targeted questions and rewrites the resume when the user provides new info.
    """
    user = _require_user(authorization)
    admin = get_admin_client()

    result = admin.table("tailored_resumes") \
        .select("*") \
        .eq("id", record_id) \
        .eq("user_id", str(user.id)) \
        .execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Tailored resume not found")

    record = result.data[0]
    profile_result = admin.table("profiles").select("*").eq("id", str(user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    parts = [record.get("job_title", ""), "at" if record.get("company") else "", record.get("company", "")]
    role_label = " ".join(p for p in parts if p).strip() or "this role"

    system_prompt = f"""You are a resume coach helping {profile.get('full_name', 'the user')} refine their tailored resume for {role_label}.

Job Description:
{(record.get('job_description') or '')[:3000]}

Current tailored resume:
{record.get('tailored_content') or ''}

Your job:
1. Start by identifying the single biggest gap between this resume and the job description
2. Ask ONE targeted question at a time to surface better metrics, achievements, or alignment
3. When the user answers, incorporate their answer into the full tailored resume and confirm what changed
4. Then ask another sharpening question OR confirm the resume is strong

Focus on: missing metrics, weak verbs, skills the JD emphasizes that aren't prominent, or summary alignment.

If you produce an improved resume, output it at the END of your reply in this exact format:
UPDATE_TAILORED_RESUME:
<full updated resume text here>
END_UPDATE

Ask ONE question at a time. Be specific to this role and resume — not generic."""

    trimmed_history = body.history[-MAX_HISTORY_TURNS:]
    messages = trimmed_history + [{"role": "user", "content": body.message}]

    try:
        response = ai_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            system=system_prompt,
            messages=messages,
        )
    except Exception as e:
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
        }).eq("id", record_id).execute()
        visible_reply = reply[:update_start].strip()
    else:
        visible_reply = reply

    return {
        "reply": visible_reply,
        "updated_content": updated_content,
    }


@router.get("/{record_id}/pdf")
def download_pdf(record_id: str, authorization: str = Header(None)):
    """Generate and return a PDF for a tailored resume record."""
    user = _require_user(authorization)
    admin = get_admin_client()

    result = admin.table("tailored_resumes") \
        .select("*") \
        .eq("id", record_id) \
        .eq("user_id", str(user.id)) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Tailored resume not found")

    record = result.data[0]
    profile_result = admin.table("profiles").select("*").eq("id", str(user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    try:
        pdf_bytes = generate_pdf(record["tailored_content"], profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    company = _safe_filename_part(record.get("company", ""), "resume")
    role = _safe_filename_part(record.get("job_title", ""), "role")
    filename = f"{company}_{role}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )
