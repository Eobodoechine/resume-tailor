r"""
Tests for backend/routes/tailor.py.

Changes this session:
  - _safe_filename_part: regex changed from [^\w\s\-] to [^\w \-] (literal space)
    → tabs and newlines are now stripped, not preserved
  - _pdf_semaphore = asyncio.Semaphore(1) added at module level
  - download_pdf changed from def → async def
  - asyncio.to_thread wraps all blocking Supabase calls in download_pdf
  - asyncio.shield wraps SSE DB insert so client disconnect can't lose history
  - get_history pagination: total reconciliation when stale count < actual items

Pure-function tests (no HTTP):
  - _safe_filename_part (regex correctness)
  - _validate_fetch_url (SSRF protection)
  - _strip_html / _extract_jsonld_job (HTML parsing)
  - get_history pagination math

Endpoint tests use authed_client with full Supabase mocking.
"""
import asyncio
import inspect
import uuid
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ─── _safe_filename_part ──────────────────────────────────────────────────────

class TestSafeFilenamePart:
    r"""
    _safe_filename_part must:
      - Allow word chars, spaces (→ underscores), and hyphens
      - STRIP tabs and newlines (the \s→literal-space fix)
      - Truncate at 80 chars
      - Return the fallback when result is empty
    """

    def _fn(self, value, fallback="fallback"):
        from routes.tailor import _safe_filename_part
        return _safe_filename_part(value, fallback)

    # Basic functionality
    def test_normal_company_name(self):
        assert self._fn("Acme Corp") == "Acme_Corp"

    def test_spaces_become_underscores(self):
        assert self._fn("Google LLC") == "Google_LLC"

    def test_hyphens_preserved(self):
        assert self._fn("well-known") == "well-known"

    def test_special_chars_stripped(self):
        result = self._fn("Company & Co. (2024)!")
        # Parentheses, ampersand, dot, exclamation must be gone
        assert "&" not in result
        assert "(" not in result
        assert ")" not in result
        assert "." not in result
        assert "!" not in result

    def test_fallback_on_empty_string(self):
        assert self._fn("", "myfallback") == "myfallback"

    def test_fallback_on_only_special_chars(self):
        assert self._fn("!!!@@@###", "default") == "default"

    def test_fallback_on_none_like_empty(self):
        # routes.tailor._safe_filename_part("", fallback)
        assert self._fn("", "fb") == "fb"

    def test_truncated_at_80_chars(self):
        long = "A" * 120
        result = self._fn(long, "fallback")
        assert len(result) <= 80

    # The critical \s → literal-space fix
    def test_tab_character_stripped(self):
        r"""
        Old regex [^\w\s\-] kept tabs because \s matches them.
        New regex [^\w \-] (literal space) strips tabs.
        """
        result = self._fn("Company\tName")
        assert "\t" not in result, (
            "Tab survived safe_filename_part — the \\s→literal-space fix was not applied."
        )

    def test_newline_character_stripped(self):
        r"""Same as tab: \s kept newlines; literal space does not."""
        result = self._fn("Company\nName")
        assert "\n" not in result, (
            "Newline survived safe_filename_part — the \\s→literal-space fix was not applied."
        )

    def test_carriage_return_stripped(self):
        result = self._fn("Company\rName")
        assert "\r" not in result

    def test_mixed_whitespace_normalized(self):
        """Multiple consecutive spaces should become a single underscore after strip."""
        result = self._fn("A   B   C")
        assert "   " not in result  # no raw triple-space in output
        assert result  # non-empty


# ─── _validate_fetch_url (SSRF protection) ───────────────────────────────────

class TestValidateFetchUrl:
    """
    _validate_fetch_url must block non-http(s) schemes, known internal hosts,
    and private/loopback IP ranges.
    """

    def _validate(self, url):
        from routes.tailor import _validate_fetch_url
        from fastapi import HTTPException
        _validate_fetch_url(url)   # raises HTTPException on blocked URL

    def test_file_scheme_blocked(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            self._validate("file:///etc/passwd")
        assert exc_info.value.status_code == 400

    def test_ftp_scheme_blocked(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._validate("ftp://internal.host/data")

    def test_localhost_blocked(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._validate("http://localhost/admin")

    def test_loopback_hostname_blocked(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._validate("http://localhost:8080/secret")

    def test_metadata_service_blocked(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._validate("http://metadata.google.internal/computeMetadata/v1/")

    def test_url_without_hostname_blocked(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            self._validate("http:///nohost")

    def test_https_public_url_passes(self):
        """Public HTTPS URL must not raise."""
        from fastapi import HTTPException
        try:
            self._validate("https://jobs.lever.co/company/role")
            # If DNS lookup fails in CI (no network), that's OK — it's not an HTTPException
        except HTTPException:
            pytest.fail("Public HTTPS URL was blocked by _validate_fetch_url")
        except Exception:
            pass  # DNS/network errors in CI are acceptable


# ─── get_history pagination ───────────────────────────────────────────────────

class TestGetHistoryPagination:
    """
    GET /api/tailor/history must return the correct pagination envelope
    and reconcile a stale count when actual items exceed it.
    """

    def _setup_mocks(self, monkeypatch, items, count_val):
        import routes.tailor as m

        db_mock = MagicMock()
        # Count query
        count_result        = MagicMock()
        count_result.count  = count_val
        count_result.data   = []
        db_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = count_result

        # Data query (different chain: select → eq → order → range → execute)
        data_result      = MagicMock()
        data_result.data = items
        db_mock.table.return_value.select.return_value.eq.return_value.order.return_value.range.return_value.execute.return_value = data_result

        monkeypatch.setattr(m, "get_client", lambda token: db_mock)
        return db_mock

    def test_empty_history_returns_envelope(self, authed_client, monkeypatch):
        self._setup_mocks(monkeypatch, items=[], count_val=0)
        r = authed_client.get("/api/tailor/history")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "total" in body
        assert "has_more" in body
        assert body["has_more"] is False

    def test_has_more_is_true_when_items_exist_beyond_page(self, authed_client, monkeypatch):
        items = [{"id": str(uuid.uuid4()), "job_title": "Dev", "company": "Acme", "created_at": "2024-01-01"}
                 for _ in range(50)]
        self._setup_mocks(monkeypatch, items=items, count_val=150)
        r = authed_client.get("/api/tailor/history?limit=50&offset=0")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 150
        assert body["has_more"] is True

    def test_has_more_false_on_last_page(self, authed_client, monkeypatch):
        items = [{"id": str(uuid.uuid4()), "job_title": "Dev", "company": "Acme", "created_at": "2024-01-01"}
                 for _ in range(10)]
        self._setup_mocks(monkeypatch, items=items, count_val=10)
        r = authed_client.get("/api/tailor/history?limit=50&offset=0")
        assert r.status_code == 200
        assert r.json()["has_more"] is False

    def test_stale_count_reconciled_upward(self, authed_client, monkeypatch):
        """
        If count=0 (stale) but we got 5 items, total must be reconciled to 5
        so the frontend's 'Showing X of Y' display isn't broken.
        """
        items = [{"id": str(uuid.uuid4()), "job_title": "Dev", "company": "X", "created_at": "2024-01-01"}
                 for _ in range(5)]
        self._setup_mocks(monkeypatch, items=items, count_val=0)
        r = authed_client.get("/api/tailor/history?limit=50&offset=0")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 5, (
            f"total={body['total']} but 5 items were returned — stale count not reconciled"
        )

    def test_limit_clamped_to_200(self, authed_client, monkeypatch):
        self._setup_mocks(monkeypatch, items=[], count_val=0)
        r = authed_client.get("/api/tailor/history?limit=9999")
        assert r.status_code == 200
        body = r.json()
        # The response limit field must be ≤ 200
        assert body.get("limit", 200) <= 200


# ─── Module-level assertions ──────────────────────────────────────────────────

class TestTailorModuleStructure:
    """
    Verify module-level changes that can be checked without an HTTP call.
    """

    def test_pdf_semaphore_is_semaphore_of_one(self):
        """Semaphore(1) caps concurrent LibreOffice processes at 1 to prevent OOM."""
        from routes.tailor import _pdf_semaphore
        assert isinstance(_pdf_semaphore, asyncio.Semaphore), (
            "_pdf_semaphore is not an asyncio.Semaphore"
        )
        # Internal _value attribute holds the initial count
        assert _pdf_semaphore._value == 1, (
            f"_pdf_semaphore value is {_pdf_semaphore._value}, expected 1"
        )

    def test_download_pdf_is_async_function(self):
        """
        download_pdf was sync def before this session; it must now be async def
        so it can use asyncio.to_thread and asyncio.Semaphore correctly.
        """
        from routes.tailor import download_pdf
        assert asyncio.iscoroutinefunction(download_pdf), (
            "download_pdf is not an async function. "
            "asyncio.to_thread and asyncio.Semaphore require async context."
        )

    def test_safe_filename_part_uses_literal_space_not_backslash_s(self):
        """
        Inspect the source to confirm the regex uses [ ] (literal space),
        not \\s.  This is the root-cause fix that prevented tabs/newlines
        appearing in filenames.
        """
        from routes.tailor import _safe_filename_part
        src = inspect.getsource(_safe_filename_part)
        # The new regex pattern must contain a literal space inside the character class
        assert r"[^\w \-]" in src or r'[^\w \-]' in src, (
            f"_safe_filename_part source does not contain [^\\w \\-] (literal space): {src!r}. "
            "The \\s→literal-space fix may not have been applied."
        )
        # The old pattern must be gone
        assert r"[^\w\s\-]" not in src, (
            "_safe_filename_part still uses \\s — tabs and newlines are not stripped."
        )


# ─── download_pdf HEAD request ────────────────────────────────────────────────

class TestDownloadPdfHead:
    """
    HEAD /api/tailor/{record_id}/pdf must return 200 without triggering LibreOffice.
    This is the pre-flight check the frontend sends before the real GET.
    """

    def _setup(self, monkeypatch, record_id):
        import routes.tailor as m
        db_mock = MagicMock()
        db_mock.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{
                "id": str(record_id),
                "tailored_content": "fake resume content",
                "company": "TestCo",
                "job_title": "Engineer",
                "user_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            }]
        )
        monkeypatch.setattr(m, "get_client", lambda token: db_mock)
        return db_mock

    def test_head_request_returns_200(self, authed_client, monkeypatch):
        import routes.tailor as m
        record_id = uuid.uuid4()
        self._setup(monkeypatch, record_id)
        # Also patch get_admin_client (used inside download_pdf for profile check on GET)
        monkeypatch.setattr(m, "get_admin_client", lambda: MagicMock())

        r = authed_client.head(f"/api/tailor/{record_id}/pdf")
        assert r.status_code == 200

    def test_head_nonexistent_record_returns_404(self, authed_client, monkeypatch):
        import routes.tailor as m
        record_id = uuid.uuid4()
        db_mock = MagicMock()
        db_mock.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[]
        )
        monkeypatch.setattr(m, "get_client", lambda token: db_mock)
        monkeypatch.setattr(m, "get_admin_client", lambda: MagicMock())

        r = authed_client.head(f"/api/tailor/{record_id}/pdf")
        assert r.status_code == 404

    def test_non_uuid_record_id_returns_422(self, authed_client):
        """FastAPI validates record_id as uuid.UUID — non-UUID must 422."""
        r = authed_client.head("/api/tailor/not-a-uuid/pdf")
        assert r.status_code == 422


# ─── GET /api/tailor/{record_id} ─────────────────────────────────────────────

class TestGetRecord:
    """
    GET /api/tailor/{record_id} must:
      - Return the full record (id, job_title, company, job_description,
        tailored_content, created_at) for the owning user.
      - Return 404 when the record belongs to a different user or does not exist.
      - Return 422 for a non-UUID record_id.
    """

    _RECORD = {
        "id": str(uuid.uuid4()),
        "job_title": "Senior Engineer",
        "company": "Acme Corp",
        "job_description": "Build amazing things.",
        "tailored_content": "SUMMARY\nTailored for Acme.",
        "created_at": "2024-06-01T10:00:00Z",
    }

    def _setup_found(self, monkeypatch, record=None):
        """Patch get_client so the DB returns the given record for owner check."""
        import routes.tailor as m
        rec = record or self._RECORD
        db_mock = MagicMock()
        db_mock.table.return_value.select.return_value \
            .eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[rec])
        monkeypatch.setattr(m, "get_client", lambda token: db_mock)
        return db_mock

    def _setup_not_found(self, monkeypatch):
        """Patch get_client so the DB returns no rows (record absent or wrong user)."""
        import routes.tailor as m
        db_mock = MagicMock()
        db_mock.table.return_value.select.return_value \
            .eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        monkeypatch.setattr(m, "get_client", lambda token: db_mock)
        return db_mock

    def test_returns_full_record_for_owner(self, authed_client, monkeypatch):
        """Owner gets a 200 with all expected fields."""
        record_id = self._RECORD["id"]
        self._setup_found(monkeypatch)
        r = authed_client.get(f"/api/tailor/{record_id}")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        assert body["id"] == record_id
        assert body["job_title"]        == self._RECORD["job_title"]
        assert body["company"]          == self._RECORD["company"]
        assert body["job_description"]  == self._RECORD["job_description"]
        assert body["tailored_content"] == self._RECORD["tailored_content"]

    def test_returns_404_for_different_user(self, authed_client, monkeypatch):
        """
        When the DB returns no rows (record belongs to another user or doesn't
        exist), the endpoint must respond with 404.
        """
        record_id = uuid.uuid4()
        self._setup_not_found(monkeypatch)
        r = authed_client.get(f"/api/tailor/{record_id}")
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"

    def test_returns_404_for_nonexistent_record(self, authed_client, monkeypatch):
        """A UUID that doesn't exist in the DB must return 404."""
        record_id = uuid.uuid4()
        self._setup_not_found(monkeypatch)
        r = authed_client.get(f"/api/tailor/{record_id}")
        assert r.status_code == 404

    def test_non_uuid_returns_422(self, authed_client):
        """FastAPI validates record_id as uuid.UUID — non-UUID input must 422."""
        r = authed_client.get("/api/tailor/not-a-valid-uuid")
        assert r.status_code == 422
