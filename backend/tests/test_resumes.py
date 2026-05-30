"""
Tests for backend/routes/resumes.py.

Changes this session:
  - Storage upload wrapped in try/except → 500 HTTPException on failure
    (was unhandled exception that produced an opaque 500 with no user-facing message)
  - Delete order reversed: DB delete first, then storage delete
    (was storage first, risking orphaned DB records if storage raised)
  - _check_magic_bytes validates file content against declared extension

Pure-function tests exercise _check_magic_bytes directly (no app needed).
Endpoint tests use authed_client with Supabase mocked per-test.
"""
import io
import uuid
import pytest
from unittest.mock import MagicMock, patch, call


# ─── Magic bytes validation ───────────────────────────────────────────────────

class TestMagicBytesValidation:
    """
    _check_magic_bytes must accept files whose leading bytes match the declared
    extension and reject mismatches.
    """

    def test_pdf_with_correct_magic_passes(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"%PDF-1.4 rest of file", "pdf") is True

    def test_pdf_with_wrong_magic_fails(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"PK\x03\x04 docx-data", "pdf") is False

    def test_pdf_with_random_bytes_fails(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"\x00\x00\x00\x00 random", "pdf") is False

    def test_docx_with_correct_magic_passes(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"PK\x03\x04 ooxml-data", "docx") is True

    def test_docx_with_pdf_magic_fails(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"%PDF-1.4 bad", "docx") is False

    def test_doc_extension_passes_without_magic_check(self):
        """
        Legacy .doc (OLE2 compound) has inconsistent magic bytes across versions.
        We trust the extension alone for .doc — any byte content is accepted.
        """
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"\xd0\xcf\x11\xe0 ole2-header", "doc") is True
        assert _check_magic_bytes(b"anything at all", "doc") is True

    def test_empty_bytes_fails_pdf(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"", "pdf") is False

    def test_empty_bytes_fails_docx(self):
        from routes.resumes import _check_magic_bytes
        assert _check_magic_bytes(b"", "docx") is False


# ─── Upload endpoint ──────────────────────────────────────────────────────────

class TestUploadResume:
    """Tests for POST /api/resumes/upload."""

    def _make_admin_mock(self, monkeypatch, file_count: int = 0):
        import routes.resumes as m
        admin_mock = MagicMock()
        # File count query
        count_result = MagicMock()
        count_result.count = file_count
        count_result.data = []
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = count_result
        # Storage upload succeeds
        admin_mock.storage.from_.return_value.upload.return_value = MagicMock()
        # DB insert succeeds
        admin_mock.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{}])
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)
        return admin_mock

    def _make_user_client_mock(self, monkeypatch):
        import routes.resumes as m
        user_mock = MagicMock()
        user_mock.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(data=[])
        monkeypatch.setattr(m, "get_client", lambda token: user_mock)
        return user_mock

    def _fake_pdf_bytes(self):
        return b"%PDF-1.4 minimal"

    def test_upload_wrong_extension_rejected_with_400(self, authed_client, monkeypatch):
        self._make_admin_mock(monkeypatch)
        self._make_user_client_mock(monkeypatch)
        data = {"file": ("resume.exe", b"MZ\x90\x00", "application/octet-stream")}
        r = authed_client.post("/api/resumes/upload", files=data)
        assert r.status_code == 400

    def test_upload_magic_bytes_mismatch_rejected_with_400(self, authed_client, monkeypatch):
        """Uploading a file with .pdf extension but non-PDF bytes should fail."""
        self._make_admin_mock(monkeypatch)
        self._make_user_client_mock(monkeypatch)
        import routes.resumes as m
        monkeypatch.setattr(m, "extract_text", lambda data, name: "fake text")
        data = {"file": ("resume.pdf", b"PK\x03\x04 actually-a-zip", "application/pdf")}
        r = authed_client.post("/api/resumes/upload", files=data)
        assert r.status_code == 400

    def test_storage_upload_failure_returns_500_with_user_message(self, authed_client, monkeypatch):
        """
        Before this fix: storage exception propagated as an unhandled 500.
        After: wrapped in try/except → 500 with a readable "Failed to store file" message.
        """
        import routes.resumes as m
        admin_mock = self._make_admin_mock(monkeypatch, file_count=0)
        self._make_user_client_mock(monkeypatch)

        # Override extract_text so magic bytes check passes
        monkeypatch.setattr(m, "extract_text", lambda data, name: "extracted text")

        # Make storage upload raise
        admin_mock.storage.from_.return_value.upload.side_effect = Exception("S3 bucket error")

        data = {"file": ("resume.pdf", self._fake_pdf_bytes(), "application/pdf")}
        r = authed_client.post("/api/resumes/upload", files=data)
        assert r.status_code == 500
        # The error message must be user-friendly, not a raw exception traceback
        body = r.json()
        detail = body.get("detail", "")
        assert "store" in detail.lower() or "failed" in detail.lower(), (
            f"Storage error message not user-friendly: {detail!r}"
        )

    def test_file_too_large_returns_413(self, authed_client, monkeypatch):
        self._make_admin_mock(monkeypatch)
        self._make_user_client_mock(monkeypatch)
        # 11 MB — over the 10 MB limit
        big_bytes = b"%PDF-1.4 " + b"x" * (11 * 1024 * 1024)
        data = {"file": ("resume.pdf", big_bytes, "application/pdf")}
        r = authed_client.post("/api/resumes/upload", files=data)
        assert r.status_code == 413


# ─── Delete order ─────────────────────────────────────────────────────────────

class TestDeleteOrder:
    """
    The delete endpoint must delete the DB record BEFORE the storage file.

    Rationale: if storage delete fails, the user can't see the orphaned file
    (DB record is gone) but it causes no data corruption. The reverse order
    (storage first, DB second) risks a corrupt state where the storage file is
    gone but the DB record still shows a phantom file.
    """

    def test_db_delete_called_before_storage_remove(self, authed_client, monkeypatch):
        import routes.resumes as m
        file_id  = str(uuid.uuid4())
        user_id  = str(uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
        storage_path = f"{user_id}/some-uuid/resume.pdf"

        call_order = []

        # User-scoped client: ownership check
        user_mock  = MagicMock()
        user_mock.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"file_path": storage_path}]
        )
        monkeypatch.setattr(m, "get_client", lambda token: user_mock)

        # Admin client: DB delete + storage remove
        admin_mock = MagicMock()

        def db_delete_side_effect():
            call_order.append("db_delete")
            return MagicMock(data=[])

        def storage_remove_side_effect(paths):
            call_order.append("storage_remove")
            return MagicMock()

        admin_mock.table.return_value.delete.return_value.eq.return_value.execute.side_effect = \
            db_delete_side_effect
        admin_mock.storage.from_.return_value.remove.side_effect = storage_remove_side_effect
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = authed_client.delete(f"/api/resumes/{file_id}")
        assert r.status_code == 200

        assert call_order == ["db_delete", "storage_remove"], (
            f"Delete order wrong: {call_order}. "
            "DB delete must precede storage remove to prevent orphaned DB records."
        )

    def test_storage_delete_failure_does_not_surface_to_user(self, authed_client, monkeypatch):
        """
        Storage delete failure should be logged (warning) but not returned as
        a 500 to the user — the DB record is already gone so the user's view
        is clean.
        """
        import routes.resumes as m
        file_id      = str(uuid.uuid4())
        storage_path = "some/path/file.pdf"

        user_mock = MagicMock()
        user_mock.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"file_path": storage_path}]
        )
        monkeypatch.setattr(m, "get_client", lambda token: user_mock)

        admin_mock = MagicMock()
        admin_mock.table.return_value.delete.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        admin_mock.storage.from_.return_value.remove.side_effect = Exception("bucket gone")
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = authed_client.delete(f"/api/resumes/{file_id}")
        assert r.status_code == 200, (
            "Storage delete failure should not surface as a non-200 status code."
        )

    def test_delete_nonexistent_file_returns_404(self, authed_client, monkeypatch):
        import routes.resumes as m
        user_mock  = MagicMock()
        user_mock.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        monkeypatch.setattr(m, "get_client", lambda token: user_mock)

        admin_mock = MagicMock()
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = authed_client.delete(f"/api/resumes/{uuid.uuid4()}")
        assert r.status_code == 404
