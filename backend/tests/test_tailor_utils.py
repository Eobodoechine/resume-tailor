"""
Unit tests for tailor-route utilities and business logic.

Covers:
  - _safe_filename_part:   PDF download filename sanitization
  - UUID path params:      Non-UUID strings return 422 automatically
  - History pagination:    limit/offset clamping, has_more calculation
  - UPDATE block parsing:  UPDATE_TAILORED_RESUME: / END_UPDATE extraction in refine_tailored
  - Master resume GET:     Returns {content, last_updated} or {content: None}
"""
import json
import sys
from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dependencies.auth import require_user, AuthContext


# ── Auth / app helpers ────────────────────────────────────────────────────────

def _fake_user(uid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
    u = MagicMock()
    u.id = uid
    u.email = "test@example.com"
    return u


def _make_ctx():
    return AuthContext(user=_fake_user(), token="tok")


@pytest.fixture(scope="module")
def test_app():
    from routes import tailor, master
    app = FastAPI()
    app.include_router(tailor.router)
    app.include_router(master.router)
    return app


@pytest.fixture
def client(test_app):
    test_app.dependency_overrides[require_user] = lambda: _make_ctx()
    with TestClient(test_app, raise_server_exceptions=True) as c:
        yield c
    test_app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def reset_supabase_mocks():
    supa = sys.modules["services.supabase_client"]
    db = MagicMock()
    supa.get_client.return_value = db
    admin = MagicMock()
    supa.get_admin_client.return_value = admin
    yield db, admin


# ── _safe_filename_part ───────────────────────────────────────────────────────

class TestSafeFilenamePart:

    def _call(self, value, fallback="resume"):
        from routes.tailor import _safe_filename_part
        return _safe_filename_part(value, fallback)

    def test_normal_company_name(self):
        assert self._call("Acme Corp") == "Acme_Corp"

    def test_spaces_become_underscores(self):
        result = self._call("Machine Learning Engineer")
        assert " " not in result
        assert "_" in result

    def test_special_chars_stripped(self):
        result = self._call("Acme/Corp & Co.!")
        assert "/" not in result
        assert "&" not in result
        assert "." not in result
        assert "!" not in result

    def test_empty_string_uses_fallback(self):
        assert self._call("", "company") == "company"

    def test_none_uses_fallback(self):
        assert self._call(None, "role") == "role"

    def test_truncated_to_80_chars(self):
        result = self._call("A" * 120)
        assert len(result) <= 80

    def test_alphanumeric_content_preserved(self):
        result = self._call("UPS123")
        assert "UPS123" in result

    def test_hyphens_preserved(self):
        result = self._call("Acme-Corp")
        assert "-" in result


# ── UUID path param validation ────────────────────────────────────────────────

class TestUUIDPathValidation:
    """FastAPI validates uuid.UUID path params automatically — non-UUIDs → 422."""

    def test_non_uuid_refine_returns_422(self, client):
        res = client.post(
            "/api/tailor/not-a-uuid/refine",
            json={"message": "help", "history": []},
        )
        assert res.status_code == 422

    def test_non_uuid_pdf_download_returns_422(self, client):
        res = client.get("/api/tailor/INVALID_ID/pdf")
        assert res.status_code == 422

    def test_all_zeros_uuid_is_valid_format(self, client, reset_supabase_mocks):
        """00000000-... is a valid UUID format — reaches route (may 404)."""
        db, _ = reset_supabase_mocks
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        res = client.post(
            "/api/tailor/00000000-0000-0000-0000-000000000000/refine",
            json={"message": "help", "history": []},
        )
        # 404 = path param valid, record not found
        assert res.status_code == 404

    def test_valid_uuid_pdf_reaches_route(self, client, reset_supabase_mocks):
        """Valid UUID reaches the handler (may 404 if no DB record)."""
        db, _ = reset_supabase_mocks
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
        res = client.get("/api/tailor/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/pdf")
        assert res.status_code == 404


# ── History pagination ─────────────────────────────────────────────────────────

class TestHistoryPagination:
    """limit/offset clamping and has_more logic in GET /api/tailor/history."""

    def _setup(self, db, count, items):
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.count = count
        db.table.return_value.select.return_value.eq.return_value \
            .order.return_value.range.return_value.execute.return_value.data = items

    def _items(self, n):
        return [
            {"id": f"r{i}", "job_title": "E", "company": "C", "created_at": "2025-01-01"}
            for i in range(n)
        ]

    def test_happy_path_response_shape(self, client, reset_supabase_mocks):
        db, _ = reset_supabase_mocks
        self._setup(db, 3, self._items(3))
        res = client.get("/api/tailor/history")
        assert res.status_code == 200
        body = res.json()
        assert "items" in body
        assert "total" in body
        assert "limit" in body
        assert "offset" in body
        assert "has_more" in body

    def test_has_more_false_when_all_on_first_page(self, client, reset_supabase_mocks):
        db, _ = reset_supabase_mocks
        self._setup(db, 3, self._items(3))
        body = client.get("/api/tailor/history?limit=10&offset=0").json()
        assert body["has_more"] is False
        assert body["total"] == 3

    def test_has_more_true_when_more_beyond_page(self, client, reset_supabase_mocks):
        db, _ = reset_supabase_mocks
        # total=10, page shows only 2
        self._setup(db, 10, self._items(2))
        body = client.get("/api/tailor/history?limit=2&offset=0").json()
        assert body["has_more"] is True

    def test_limit_0_clamped_to_1(self, client, reset_supabase_mocks):
        """limit=0 is invalid; must be clamped to 1."""
        db, _ = reset_supabase_mocks
        self._setup(db, 1, self._items(1))
        res = client.get("/api/tailor/history?limit=0")
        assert res.status_code == 200
        assert res.json()["limit"] == 1

    def test_limit_999_clamped_to_200(self, client, reset_supabase_mocks):
        db, _ = reset_supabase_mocks
        self._setup(db, 5, self._items(5))
        res = client.get("/api/tailor/history?limit=999")
        assert res.status_code == 200
        assert res.json()["limit"] == 200

    def test_negative_offset_clamped_to_0(self, client, reset_supabase_mocks):
        db, _ = reset_supabase_mocks
        self._setup(db, 5, self._items(5))
        res = client.get("/api/tailor/history?offset=-5")
        assert res.status_code == 200
        assert res.json()["offset"] == 0

    def test_stale_count_reconciled_with_items(self, client, reset_supabase_mocks):
        """If count is 0 but items came back, total is bumped to match seen items."""
        db, _ = reset_supabase_mocks
        # count returns 0 (stale/race), but data has 3 items
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.count = 0
        db.table.return_value.select.return_value.eq.return_value \
            .order.return_value.range.return_value.execute.return_value.data = self._items(3)
        body = client.get("/api/tailor/history").json()
        assert body["total"] >= 3


# ── UPDATE block parsing in refine_tailored ───────────────────────────────────

class TestRefineUpdateParsing:
    """UPDATE_TAILORED_RESUME: / END_UPDATE block is extracted and stripped."""

    RECORD_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _setup_db(self, db, reply_text):
        """Configure mock DB and Claude response for the refine route."""
        # Double-eq chain for tailored_resumes ownership check
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {
                "id": self.RECORD_ID,
                "job_title": "Engineer",
                "company": "Acme",
                "job_description": "Build things",
                "tailored_content": "OLD RESUME",
            }
        ]
        # Single-eq chain used for both profiles and master_resumes lookups
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"full_name": "Jane Smith", "content": "MASTER RESUME CONTEXT"}
        ]

        import routes.tailor as tm
        tm.ai_client.messages.create.return_value.content = [
            MagicMock(text=reply_text)
        ]

    def test_update_block_extracted_from_reply(self, client, reset_supabase_mocks):
        """updated_content contains only the resume text, not the surrounding reply."""
        db, _ = reset_supabase_mocks
        self._setup_db(db, (
            "Great — I added your metrics.\n"
            "UPDATE_TAILORED_RESUME:\nNEW RESUME TEXT HERE\nEND_UPDATE"
        ))

        res = client.post(
            f"/api/tailor/{self.RECORD_ID}/refine",
            json={"message": "Add my metrics", "history": []},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["updated_content"] == "NEW RESUME TEXT HERE"

    def test_update_block_stripped_from_visible_reply(self, client, reset_supabase_mocks):
        """The UPDATE_TAILORED_RESUME: / END_UPDATE block must not appear in reply."""
        db, _ = reset_supabase_mocks
        self._setup_db(db, (
            "I improved the summary.\n"
            "UPDATE_TAILORED_RESUME:\nNEW RESUME\nEND_UPDATE"
        ))

        res = client.post(
            f"/api/tailor/{self.RECORD_ID}/refine",
            json={"message": "Improve it", "history": []},
        )
        body = res.json()
        assert "UPDATE_TAILORED_RESUME:" not in body["reply"]
        assert "END_UPDATE" not in body["reply"]
        assert "I improved the summary" in body["reply"]

    def test_no_update_block_returns_none_updated_content(self, client, reset_supabase_mocks):
        """If Claude asks a question without updating, updated_content must be None."""
        db, _ = reset_supabase_mocks
        self._setup_db(db, "What specific metrics can you add to the UPS bullet?")

        res = client.post(
            f"/api/tailor/{self.RECORD_ID}/refine",
            json={"message": "Help", "history": []},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["updated_content"] is None
        assert "metrics" in body["reply"]

    def test_multiline_update_block_preserved(self, client, reset_supabase_mocks):
        """Multi-line resume text inside the block is preserved verbatim."""
        new_resume = "NNAMDI OBODOECHINE\n\nSUMMARY\nLeader.\n\nEXPERIENCE\nPM | Acme | 2022"
        db, _ = reset_supabase_mocks
        self._setup_db(db, (
            f"Updated!\nUPDATE_TAILORED_RESUME:\n{new_resume}\nEND_UPDATE"
        ))

        res = client.post(
            f"/api/tailor/{self.RECORD_ID}/refine",
            json={"message": "Update it", "history": []},
        )
        body = res.json()
        assert body["updated_content"] == new_resume


# ── GET /api/master/ ──────────────────────────────────────────────────────────

class TestMasterResumeGet:
    """GET /api/master/ returns the content or a null-content sentinel."""

    def test_returns_content_when_present(self, client, reset_supabase_mocks):
        db, _ = reset_supabase_mocks
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"content": "FULL MASTER RESUME", "last_updated": "2025-01-01T00:00:00Z"}
        ]
        res = client.get("/api/master/")
        assert res.status_code == 200
        body = res.json()
        assert body["content"] == "FULL MASTER RESUME"

    def test_returns_null_content_when_absent(self, client, reset_supabase_mocks):
        """No master resume yet → {content: None} not a 404."""
        db, _ = reset_supabase_mocks
        db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        res = client.get("/api/master/")
        assert res.status_code == 200
        body = res.json()
        assert body["content"] is None
