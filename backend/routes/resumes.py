"""
Resume file upload routes.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pathlib import Path
from dependencies.auth import require_user, AuthContext
from services.supabase_client import get_client, get_admin_client
from services.extractor import extract_text
from config import RESUME_BUCKET
from limiter import limiter
import logging
import uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB hard cap
MAX_FILES_PER_USER = 100              # generous cap — prevents unbounded storage growth

# Allowed MIME types for resume files
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}

# Magic bytes for file type validation (TD-07)
# Reading the Content-Type header alone is bypassable — any client can lie.
# Checking the actual file bytes prevents disguised uploads.
_PDF_MAGIC = b"%PDF"
_DOCX_MAGIC = b"PK\x03\x04"   # ZIP/OOXML container (DOCX, XLSX, PPTX)


def _check_magic_bytes(data: bytes, ext: str) -> bool:
    """Return True if the file's leading bytes match the declared extension."""
    if ext == "pdf":
        return data[:4] == _PDF_MAGIC
    elif ext in ("docx", "doc"):
        # .doc (legacy) is OLE2 compound; DOCX is ZIP. We accept both but
        # check DOCX magic — legacy .doc is rare and may not have consistent
        # magic across all versions, so we allow it through on ext alone.
        if ext == "docx":
            return data[:4] == _DOCX_MAGIC
        return True  # .doc — trust the extension, OLE2 magic varies
    return False


@router.get("/")
@limiter.limit("60/minute")
def list_resumes(request: Request, ctx: AuthContext = Depends(require_user)):
    """List all uploaded resume files for the current user."""
    db = get_client(ctx.token)
    result = db.table("resume_files") \
        .select("id, filename, file_type, uploaded_at") \
        .eq("user_id", str(ctx.user.id)) \
        .order("uploaded_at", desc=True) \
        .execute()
    return result.data


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_user)
):
    """Upload a new resume file. Extracts text and stores in Supabase."""
    admin = get_admin_client()

    # Enforce per-user file cap before accepting the upload
    count_result = admin.table("resume_files") \
        .select("id", count="exact") \
        .eq("user_id", str(ctx.user.id)) \
        .execute()
    existing_count = count_result.count if count_result.count is not None else len(count_result.data)
    if existing_count >= MAX_FILES_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=f"You've reached the maximum of {MAX_FILES_PER_USER} uploaded files. Delete some before uploading more."
        )

    # Validate file extension
    raw_filename = file.filename or "resume"
    # Strip path components to prevent path traversal attacks
    safe_filename = Path(raw_filename).name
    ext = safe_filename.lower().rsplit(".", 1)[-1] if "." in safe_filename else ""
    if ext not in ("pdf", "docx", "doc"):
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported")

    # Validate MIME type (defense-in-depth alongside extension check)
    content_type = file.content_type or ""
    if content_type and content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type}")

    # Enforce file size limit BEFORE reading the entire payload into memory
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")

    # Validate magic bytes — prevents disguised file uploads (TD-07)
    if not _check_magic_bytes(file_bytes, ext):
        raise HTTPException(
            status_code=400,
            detail=f"File content does not match the declared extension (.{ext}). Upload a real PDF or DOCX file."
        )

    # Extract text
    try:
        extracted = extract_text(file_bytes, safe_filename)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not read file: {str(e)}")

    # Upload to Supabase Storage — uuid in path prevents collisions; safe_filename strips traversal
    storage_path = f"{ctx.user.id}/{uuid.uuid4()}/{safe_filename}"
    admin.storage.from_(RESUME_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": content_type or "application/octet-stream"}
    )

    # Save metadata to DB — use admin client since storage returns service-level metadata
    admin.table("resume_files").insert({
        "user_id": str(ctx.user.id),
        "filename": safe_filename,
        "file_path": storage_path,
        "file_type": ext,
        "extracted_text": extracted
    }).execute()

    return {"message": f"'{safe_filename}' uploaded successfully", "extracted_length": len(extracted)}


@router.delete("/{file_id}")
def delete_resume(file_id: str, ctx: AuthContext = Depends(require_user)):
    """Delete a resume file."""
    db = get_client(ctx.token)
    admin = get_admin_client()

    # Confirm ownership via RLS-respecting client
    result = db.table("resume_files").select("file_path").eq("id", file_id).eq("user_id", str(ctx.user.id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = result.data[0]["file_path"]

    # Delete from storage — log failure but continue so DB record is always cleaned up
    try:
        admin.storage.from_(RESUME_BUCKET).remove([file_path])
    except Exception as e:
        logger.warning("Storage delete failed for path=%s: %s", file_path, e)

    # Delete from DB
    admin.table("resume_files").delete().eq("id", file_id).execute()
    return {"message": "File deleted"}
