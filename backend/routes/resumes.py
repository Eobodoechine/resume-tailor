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

# Allowed MIME types for resume files.
# Legacy .doc (application/msword) is intentionally excluded — python-docx cannot
# read OLE2 .doc files, so they are rejected up front with a clear message (B4).
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Magic bytes for file type validation (TD-07)
# Reading the Content-Type header alone is bypassable — any client can lie.
# Checking the actual file bytes prevents disguised uploads.
_PDF_MAGIC = b"%PDF"
_DOCX_MAGIC = b"PK\x03\x04"   # ZIP/OOXML container (DOCX, XLSX, PPTX)


def _check_magic_bytes(data: bytes, ext: str) -> bool:
    """Return True if the file's leading bytes match the declared extension.

    Only pdf and docx are supported. Legacy .doc (OLE2) is rejected earlier at
    the extension check (B4), so it never reaches here.
    """
    if ext == "pdf":
        return data[:4] == _PDF_MAGIC
    elif ext == "docx":
        return data[:4] == _DOCX_MAGIC
    return False


@router.get("/")
@limiter.limit("60/minute")
def list_resumes(request: Request, ctx: AuthContext = Depends(require_user)):
    """List all uploaded resume files for the current user."""
    logger.debug("[resumes] list  user=%s", ctx.user.id)
    db = get_client(ctx.token)
    result = db.table("resume_files") \
        .select("id, filename, file_type, uploaded_at") \
        .eq("user_id", str(ctx.user.id)) \
        .order("uploaded_at", desc=True) \
        .execute()
    logger.debug("[resumes] list returned %d file(s)  user=%s", len(result.data), ctx.user.id)
    return result.data


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_user)
):
    """Upload a new resume file. Extracts text and stores in Supabase."""
    raw_filename = file.filename or "resume"
    safe_filename = Path(raw_filename).name
    logger.info("[resumes] upload START  user=%s  filename=%r  content_type=%s",
                ctx.user.id, safe_filename, file.content_type)
    admin = get_admin_client()

    # Enforce per-user file cap before accepting the upload
    count_result = admin.table("resume_files") \
        .select("id", count="exact") \
        .eq("user_id", str(ctx.user.id)) \
        .execute()
    existing_count = count_result.count if count_result.count is not None else len(count_result.data)
    logger.debug("[resumes] upload file cap check  user=%s  existing=%d  max=%d",
                 ctx.user.id, existing_count, MAX_FILES_PER_USER)
    if existing_count >= MAX_FILES_PER_USER:
        logger.warning("[resumes] upload 400 file cap reached  user=%s  existing=%d",
                       ctx.user.id, existing_count)
        raise HTTPException(
            status_code=400,
            detail=f"You've reached the maximum of {MAX_FILES_PER_USER} uploaded files. Delete some before uploading more."
        )

    # Validate file extension
    ext = safe_filename.lower().rsplit(".", 1)[-1] if "." in safe_filename else ""
    if ext not in ("pdf", "docx"):
        # Legacy .doc is rejected here (B4): python-docx can't parse OLE2 .doc,
        # so accepting it would only fail later at extraction with a vague error.
        logger.warning("[resumes] upload 400 bad extension  user=%s  filename=%r  ext=%r",
                       ctx.user.id, safe_filename, ext)
        detail = (
            "Legacy .doc files aren't supported — please save as .docx or PDF and re-upload."
            if ext == "doc" else "Only PDF and DOCX files are supported"
        )
        raise HTTPException(status_code=400, detail=detail)

    # Validate MIME type (defense-in-depth alongside extension check)
    content_type = file.content_type or ""
    if content_type and content_type not in ALLOWED_MIME_TYPES:
        logger.warning("[resumes] upload 400 bad MIME  user=%s  filename=%r  content_type=%s",
                       ctx.user.id, safe_filename, content_type)
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type}")

    # Enforce file size limit BEFORE reading the entire payload into memory
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    file_size = len(file_bytes)
    logger.debug("[resumes] upload bytes read  user=%s  filename=%r  size=%d", ctx.user.id, safe_filename, file_size)
    if file_size > MAX_UPLOAD_BYTES:
        logger.warning("[resumes] upload 413 file too large  user=%s  filename=%r  size=%d",
                       ctx.user.id, safe_filename, file_size)
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")

    # Validate magic bytes — prevents disguised file uploads (TD-07)
    if not _check_magic_bytes(file_bytes, ext):
        logger.warning("[resumes] upload 400 magic bytes mismatch  user=%s  filename=%r  ext=%r  first4=%r",
                       ctx.user.id, safe_filename, ext, file_bytes[:4])
        raise HTTPException(
            status_code=400,
            detail=f"File content does not match the declared extension (.{ext}). Upload a real PDF or DOCX file."
        )

    # Extract text
    try:
        extracted = extract_text(file_bytes, safe_filename)
        logger.info("[resumes] upload text extracted  user=%s  filename=%r  chars=%d",
                    ctx.user.id, safe_filename, len(extracted))
    except Exception as e:
        logger.error("[resumes] upload 422 text extraction failed  user=%s  filename=%r  error=%s",
                     ctx.user.id, safe_filename, e)
        raise HTTPException(status_code=422, detail="Could not read the file. Make sure it's a valid PDF or DOCX.")

    # Upload to Supabase Storage — uuid in path prevents collisions; safe_filename strips traversal
    storage_path = f"{ctx.user.id}/{uuid.uuid4()}/{safe_filename}"
    logger.info("[resumes] upload → storage  user=%s  path=%s  bytes=%d", ctx.user.id, storage_path, file_size)
    try:
        admin.storage.from_(RESUME_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": content_type or "application/octet-stream"}
        )
        logger.info("[resumes] upload storage OK  user=%s  path=%s", ctx.user.id, storage_path)
    except Exception as e:
        logger.error("[resumes] upload 500 storage FAILED  user=%s  filename=%r  path=%s  error=%s",
                     ctx.user.id, safe_filename, storage_path, e)
        raise HTTPException(status_code=500, detail="Failed to store file. Please try again.")

    # Save metadata to DB — use admin client since storage returns service-level metadata.
    # If the insert fails, remove the already-uploaded storage file to avoid orphaning it.
    try:
        admin.table("resume_files").insert({
            "user_id": str(ctx.user.id),
            "filename": safe_filename,
            "file_path": storage_path,
            "file_type": ext,
            "extracted_text": extracted
        }).execute()
    except Exception as e:
        logger.error("[resumes] upload DB insert FAILED — removing orphaned storage file  user=%s  path=%s  error=%s",
                     ctx.user.id, storage_path, e)
        try:
            admin.storage.from_(RESUME_BUCKET).remove([storage_path])
        except Exception as cleanup_err:
            logger.warning("[resumes] upload orphan cleanup also FAILED  user=%s  path=%s  error=%s",
                           ctx.user.id, storage_path, cleanup_err)
        raise HTTPException(status_code=500, detail="Failed to save file metadata. Please try again.")
    logger.info("[resumes] upload COMPLETE  user=%s  filename=%r  ext=%s  chars=%d",
                ctx.user.id, safe_filename, ext, len(extracted))

    return {"message": f"'{safe_filename}' uploaded successfully", "extracted_length": len(extracted)}


@router.delete("/{file_id}")
def delete_resume(file_id: str, ctx: AuthContext = Depends(require_user)):
    """Delete a resume file."""
    logger.info("[resumes] delete START  user=%s  file_id=%s", ctx.user.id, file_id)
    db = get_client(ctx.token)
    admin = get_admin_client()

    # Confirm ownership via RLS-respecting client
    result = db.table("resume_files").select("file_path, filename").eq("id", file_id).eq("user_id", str(ctx.user.id)).execute()
    if not result.data:
        logger.warning("[resumes] delete 404  user=%s  file_id=%s", ctx.user.id, file_id)
        raise HTTPException(status_code=404, detail="File not found")

    file_path = result.data[0]["file_path"]
    filename   = result.data[0].get("filename", "unknown")
    logger.info("[resumes] delete found  user=%s  file_id=%s  filename=%r  path=%s",
                ctx.user.id, file_id, filename, file_path)

    # Delete DB record first — if storage delete fails, the record is gone
    # (file orphaned in storage) but the user can't see it. Safer than deleting
    # storage first and leaving an orphaned DB record.
    admin.table("resume_files").delete().eq("id", file_id).execute()
    logger.info("[resumes] delete DB record removed  user=%s  file_id=%s", ctx.user.id, file_id)

    # Delete from storage — log failure but don't surface it to the user
    try:
        admin.storage.from_(RESUME_BUCKET).remove([file_path])
        logger.info("[resumes] delete storage removed  user=%s  path=%s", ctx.user.id, file_path)
    except Exception as e:
        logger.warning("[resumes] delete storage FAILED (file orphaned in bucket)  user=%s  path=%s  error=%s",
                       ctx.user.id, file_path, e)

    return {"message": "File deleted"}
