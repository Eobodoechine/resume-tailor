"""
Resume file upload routes.
"""
from fastapi import APIRouter, Header, HTTPException, UploadFile, File
from services.supabase_client import get_admin_client, get_user_from_token
from services.extractor import extract_text
from config import RESUME_BUCKET
import uuid

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


def _require_user(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


@router.get("/")
def list_resumes(authorization: str = Header(None)):
    """List all uploaded resume files for the current user."""
    user = _require_user(authorization)
    admin = get_admin_client()
    result = admin.table("resume_files") \
        .select("id, filename, file_type, uploaded_at") \
        .eq("user_id", str(user.id)) \
        .order("uploaded_at", desc=True) \
        .execute()
    return result.data


@router.post("/upload")
async def upload_resume(
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    """Upload a new resume file. Extracts text and stores in Supabase."""
    user = _require_user(authorization)
    admin = get_admin_client()

    # Validate file type
    filename = file.filename or "resume"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in ("pdf", "docx", "doc"):
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported")

    file_bytes = await file.read()

    # Extract text
    try:
        extracted = extract_text(file_bytes, filename)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not read file: {str(e)}")

    # Upload to Supabase Storage
    storage_path = f"{user.id}/{uuid.uuid4()}/{filename}"
    admin.storage.from_(RESUME_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": file.content_type or "application/octet-stream"}
    )

    # Save metadata to DB
    admin.table("resume_files").insert({
        "user_id": str(user.id),
        "filename": filename,
        "file_path": storage_path,
        "file_type": ext,
        "extracted_text": extracted
    }).execute()

    return {"message": f"'{filename}' uploaded successfully", "extracted_length": len(extracted)}


@router.delete("/{file_id}")
def delete_resume(file_id: str, authorization: str = Header(None)):
    """Delete a resume file."""
    user = _require_user(authorization)
    admin = get_admin_client()

    # Confirm ownership
    result = admin.table("resume_files").select("file_path").eq("id", file_id).eq("user_id", str(user.id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = result.data[0]["file_path"]

    # Delete from storage
    try:
        admin.storage.from_(RESUME_BUCKET).remove([file_path])
    except Exception:
        pass  # Continue even if storage delete fails

    # Delete from DB
    admin.table("resume_files").delete().eq("id", file_id).execute()
    return {"message": "File deleted"}
