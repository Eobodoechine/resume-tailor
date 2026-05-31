"""
HTTP layer tests for the Playwright pipeline routes in routes/tailor.py:

  GET  /{id}/preview  — 501 when engine=libreoffice
                        200 HTML when engine=playwright
  GET  /{id}/pdf      — uses correct renderer based on RESUME_PDF_ENGINE
  HEAD /{id}/pdf      — 200 for both engines, renderer never runs
  404/422             — ownership + UUID validation

All Supabase I/O, text_to_resume_data, and rendering are mocked.
FastAPI routing, dependency injection, and HTTP response shape are tested real.
"""
import uuid
import sys
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _fake_auth():
    from dependencies.auth import AuthContext
    user = MagicMock()
    user.id    = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    user.email = "test@example.com"
    return AuthContext(user=user, token="fake-token")


@pytest.fixture(scope="module")
def app():
    from routes import tailor
    a = FastAPI()
    a.include_router(tailor.router)
    return a


@pytest.fixture
def client(app):
    from dependencies.auth import require_user
    app.dependency_overrides[require_user] = lambda: _fake_auth()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db_mock():
    sc = sys.modules["services.supabase_client"]
    db    = MagicMock()
    admin = MagicMock()
    sc.get_client.return_value    = db
    sc.get_admin_client.return_value = admin
    return db, admin


def _wire_record(db, record_id, company="Acme", job_title="Engineer"):
    record = {
        "id":              str(record_id),
        "user_id":         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tailored_content": "SUMMARY\nSenior Engineer",
        "company":         company,
        "job_title":       job_title,
    }
    # Double .eq() chain used in _fetch_record_and_profile
    (db.table.return_value
       .select.return_value
       .eq.return_value
       .eq.return_value
       .execute.return_value.data) = [record]
    # Profile fetch (.eq() chain for profiles)
    (db.table.return_value
       .select.return_value
       .eq.return_value
       .execute.return_value.data) = [{"full_name": "Test User"}]
    return record


def _wire_missing(db):
    (db.table.return_value
       .select.return_value
       .eq.return_value
       .eq.return_value
       .execute.return_value.data) = []


# ── 1. GET /{id}/preview  — libreoffice mode ──────────────────────────────────

class TestPreviewLibreofficMode:

    def test_501_when_engine_is_libreoffice(self, client, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        r = client.get(f"/api/tailor/{uuid.uuid4()}/preview")
        assert r.status_code == 501

    def test_501_detail_mentions_playwright(self, client, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        r = client.get(f"/api/tailor/{uuid.uuid4()}/preview")
        assert "playwright" in r.json()["detail"].lower()

    def test_preview_requires_auth(self, app):
        from dependencies.auth import require_user
        app.dependency_overrides.clear()
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get(f"/api/tailor/{uuid.uuid4()}/preview")
        assert r.status_code == 401

    def test_non_uuid_preview_returns_422(self, client):
        r = client.get("/api/tailor/not-a-uuid/preview")
        assert r.status_code == 422


# ── 2. GET /{id}/preview  — playwright mode ───────────────────────────────────

class TestPreviewPlaywrightMode:

    def test_200_html_when_engine_playwright(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        monkeypatch.setattr(m, "text_to_resume_data",
                            lambda *_: {"name": "Test", "experience": [], "skills": []})
        renderer = MagicMock()
        renderer.render_html.return_value = "<!DOCTYPE html><html><body>PREVIEW</body></html>"
        monkeypatch.setattr(m, "FDEHtmlRenderer", lambda: renderer)

        r = client.get(f"/api/tailor/{rid}/preview")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "PREVIEW" in r.text

    def test_preview_html_has_doctype(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        monkeypatch.setattr(m, "text_to_resume_data", lambda *_: {})
        renderer = MagicMock()
        renderer.render_html.return_value = "<!DOCTYPE html><html></html>"
        monkeypatch.setattr(m, "FDEHtmlRenderer", lambda: renderer)

        r = client.get(f"/api/tailor/{rid}/preview")
        assert "<!DOCTYPE html>" in r.text

    def test_preview_404_for_missing_record(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        _wire_missing(db)
        r = client.get(f"/api/tailor/{uuid.uuid4()}/preview")
        assert r.status_code == 404


# ── 3. GET /{id}/pdf — engine switching ───────────────────────────────────────

class TestDownloadPdfEngineSwitch:

    def test_playwright_engine_calls_html_to_pdf(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        monkeypatch.setattr(m, "text_to_resume_data", lambda *_: {})
        renderer = MagicMock()
        renderer.render_html.return_value = "<html></html>"
        monkeypatch.setattr(m, "FDEHtmlRenderer", lambda: renderer)

        fake_pdf = b"%PDF-1.4" + b"x" * 5000
        html_to_pdf_mock = AsyncMock(return_value=fake_pdf)
        with patch("routes.tailor.html_to_pdf", html_to_pdf_mock):
            r = client.get(f"/api/tailor/{rid}/pdf")

        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content == fake_pdf
        html_to_pdf_mock.assert_awaited_once()

    def test_libreoffice_engine_calls_get_renderer(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        monkeypatch.setattr(m, "text_to_resume_data", lambda *_: {})

        fake_pdf = b"%PDF-libreoffice" + b"x" * 5000
        renderer = MagicMock()
        renderer.render.return_value = fake_pdf
        monkeypatch.setattr(m, "get_renderer", lambda: renderer)

        r = client.get(f"/api/tailor/{rid}/pdf")
        assert r.status_code == 200
        assert r.content == fake_pdf
        renderer.render.assert_called_once()

    def test_playwright_engine_does_not_call_libreoffice(self, client, db_mock, monkeypatch):
        """When engine=playwright, get_renderer() (LibreOffice path) must not be called."""
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        monkeypatch.setattr(m, "text_to_resume_data", lambda *_: {})
        renderer = MagicMock()
        renderer.render_html.return_value = "<html></html>"
        monkeypatch.setattr(m, "FDEHtmlRenderer", lambda: renderer)
        get_renderer_mock = MagicMock()
        monkeypatch.setattr(m, "get_renderer", get_renderer_mock)

        with patch("routes.tailor.html_to_pdf", AsyncMock(return_value=b"%PDF" + b"x" * 5000)):
            client.get(f"/api/tailor/{rid}/pdf")

        get_renderer_mock.assert_not_called()

    def test_content_disposition_has_attachment_and_pdf(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid, company="Acme", job_title="Engineer")
        monkeypatch.setattr(m, "text_to_resume_data", lambda *_: {})
        renderer = MagicMock()
        renderer.render.return_value = b"%PDF" + b"x" * 5000
        monkeypatch.setattr(m, "get_renderer", lambda: renderer)

        r = client.get(f"/api/tailor/{rid}/pdf")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert ".pdf" in cd


# ── 4. HEAD /{id}/pdf — no renderer runs ──────────────────────────────────────

class TestHeadPdfRequest:
    """HEAD must return 200 for both engines WITHOUT running any renderer."""

    def test_head_libreoffice_200(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        renderer = MagicMock()
        monkeypatch.setattr(m, "get_renderer", lambda: renderer)

        r = client.head(f"/api/tailor/{rid}/pdf")
        assert r.status_code == 200
        renderer.render.assert_not_called()

    def test_head_playwright_200(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        renderer = MagicMock()
        monkeypatch.setattr(m, "FDEHtmlRenderer", lambda: renderer)

        r = client.head(f"/api/tailor/{rid}/pdf")
        assert r.status_code == 200
        renderer.render_html.assert_not_called()

    def test_head_has_pdf_content_type(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        db, _ = db_mock
        rid = uuid.uuid4()
        _wire_record(db, rid)
        monkeypatch.setattr(m, "get_renderer", lambda: MagicMock())

        r = client.head(f"/api/tailor/{rid}/pdf")
        assert "application/pdf" in r.headers.get("content-type", "")


# ── 5. 404 / 422 ──────────────────────────────────────────────────────────────

class TestOwnershipAndValidation:

    def test_missing_record_returns_404(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "libreoffice")
        db, _ = db_mock
        _wire_missing(db)
        r = client.get(f"/api/tailor/{uuid.uuid4()}/pdf")
        assert r.status_code == 404

    def test_non_uuid_returns_422(self, client):
        r = client.get("/api/tailor/not-a-uuid/pdf")
        assert r.status_code == 422

    def test_missing_record_preview_returns_404(self, client, db_mock, monkeypatch):
        import routes.tailor as m
        monkeypatch.setattr(m, "RESUME_PDF_ENGINE", "playwright")
        db, _ = db_mock
        _wire_missing(db)
        r = client.get(f"/api/tailor/{uuid.uuid4()}/preview")
        assert r.status_code == 404


# ── 6. Import / regression safety ────────────────────────────────────────────

class TestImportSafety:

    def test_tailor_imports_htmlresponse(self):
        """HTMLResponse must be imported — missing import → NameError at runtime."""
        import inspect
        import routes.tailor as m
        assert "HTMLResponse" in inspect.getsource(m), (
            "HTMLResponse not in routes/tailor.py source — "
            "preview_html() would raise NameError at runtime."
        )

    def test_default_engine_is_libreoffice(self):
        """RESUME_PDF_ENGINE must default to 'libreoffice' — no production behaviour change on deploy."""
        import config
        assert config.RESUME_PDF_ENGINE in ("libreoffice", "playwright"), (
            f"Unknown engine value: {config.RESUME_PDF_ENGINE!r}"
        )
        # If env var is not set, the default must be libreoffice
        import os
        if not os.getenv("RESUME_PDF_ENGINE"):
            assert config.RESUME_PDF_ENGINE == "libreoffice", (
                "Default RESUME_PDF_ENGINE must be 'libreoffice' — "
                "changing this silently switches all users to the new engine."
            )

    def test_fde_html_import_does_not_crash(self):
        """fde_html.py must import cleanly even without Liberation Sans fonts."""
        sys.modules.pop("renderers.fde_html", None)
        import renderers.fde_html  # must not raise
        assert hasattr(renderers.fde_html, "FDEHtmlRenderer")

    def test_registry_default_unchanged(self):
        """Regression: registry default must still be fde_docx."""
        from renderers.registry import DEFAULT_TEMPLATE
        assert DEFAULT_TEMPLATE == "fde_docx"
