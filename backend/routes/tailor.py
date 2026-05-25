"""
Tailor routes: generate a tailored resume from master + JD, save history, download PDF.
"""
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Optional
import re
from services.supabase_client import get_admin_client, get_user_from_token
from services import claude as claude_service
from services.pdf_generator import generate_pdf
from config import PDF_BUCKET
import uuid

router = APIRouter(prefix="/api/tailor", tags=["tailor"])

MAX_JD_LENGTH = 12_000  # ~3,000 tokens — enough for any real JD


def _require_user(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _safe_filename_part(value: str, fallback: str) -> str:
    """Sanitize a string for use in a Content-Disposition filename."""
    sanitized = re.sub(r"[^\w\s\-]", "", value or "").strip().replace(" ", "_")
    return sanitized[:80] or fallback


class TailorRequest(BaseModel):
    job_description: str = Field(..., max_length=MAX_JD_LENGTH)
    job_title: Optional[str] = Field("", max_length=200)
    company: Optional[str] = Field("", max_length=200)


@router.post("/")
def tailor_resume(body: TailorRequest, authorization: str = Header(None)):
    """
    Tailor the master resume to a job description.
    Saves the result to history and returns the tailored text + a record ID for PDF download.
    """
    user = _require_user(authorization)
    admin = get_admin_client()

    # Get master resume
    master_result = admin.table("master_resumes").select("content").eq("user_id", str(user.id)).execute()
    if not master_result.data or not master_result.data[0]["content"]:
        raise HTTPException(status_code=400, detail="No master resume found. Upload files and synthesize first.")

    master_content = master_result.data[0]["content"]

    # Get profile
    profile_result = admin.table("profiles").select("*").eq("id", str(user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    # Tailor via Claude
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

    # Save to history
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


# NOTE: /history must be registered BEFORE /{record_id}/pdf to prevent
# FastAPI matching "history" as a record_id in /{record_id}/pdf routes.
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


@router.get("/{record_id}/pdf")
def download_pdf(record_id: str, authorization: str = Header(None)):
    """Generate and return a PDF for a tailored resume record."""
    user = _require_user(authorization)
    admin = get_admin_client()

    # Fetch tailored resume
    result = admin.table("tailored_resumes") \
        .select("*") \
        .eq("id", record_id) \
        .eq("user_id", str(user.id)) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Tailored resume not found")

    record = result.data[0]

    # Get profile
    profile_result = admin.table("profiles").select("*").eq("id", str(user.id)).execute()
    profile = profile_result.data[0] if profile_result.data else {}

    # Generate PDF
    try:
        pdf_bytes = generate_pdf(record["tailored_content"], profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    # Sanitize filename components to prevent header injection
    company = _safe_filename_part(record.get("company", ""), "resume")
    role = _safe_filename_part(record.get("job_title", ""), "role")
    filename = f"{company}_{role}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )
