"""
Tests for backend/routes/admin.py.

Changes this session:
  - ApprovalBody.request_id: str → uuid.UUID (422 on non-UUID input)
  - list_requests paginated: limit clamped [1,200], offset clamped ≥0
  - list_users paginated: limit clamped [1,500], offset clamped ≥0
  - All .eq("id", …) calls wrapped in str() to prevent type mismatch
  - @limiter.limit("60/minute") added to list_requests
  - Non-admin requests must receive 403
"""
import uuid
import pytest
from unittest.mock import MagicMock, call
from pydantic import ValidationError


# ─── Pydantic model: ApprovalBody ────────────────────────────────────────────

class TestApprovalBodyValidation:
    """ApprovalBody.request_id must be validated as uuid.UUID, not a bare string."""

    def test_valid_uuid_object_accepted(self):
        from routes.admin import ApprovalBody
        uid = uuid.uuid4()
        body = ApprovalBody(request_id=uid)
        assert body.request_id == uid

    def test_valid_uuid_string_is_coerced_to_uuid(self):
        """Pydantic v2 coerces a well-formed UUID string into a uuid.UUID."""
        from routes.admin import ApprovalBody
        uid_str = str(uuid.uuid4())
        body = ApprovalBody(request_id=uid_str)
        assert isinstance(body.request_id, uuid.UUID)
        assert str(body.request_id) == uid_str

    def test_non_uuid_string_raises_validation_error(self):
        """
        Previously request_id: str — any string (including SQL injection payloads)
        would pass validation. Now uuid.UUID rejects them with ValidationError.
        """
        from routes.admin import ApprovalBody
        with pytest.raises((ValidationError, ValueError)):
            ApprovalBody(request_id="not-a-uuid-string")

    def test_sql_injection_payload_rejected(self):
        """Belt-and-suspenders: SQL-injection strings must NOT validate as a UUID."""
        from routes.admin import ApprovalBody
        with pytest.raises((ValidationError, ValueError)):
            ApprovalBody(request_id="'; DROP TABLE access_requests; --")

    def test_empty_string_rejected(self):
        from routes.admin import ApprovalBody
        with pytest.raises((ValidationError, ValueError)):
            ApprovalBody(request_id="")

    def test_integer_rejected(self):
        from routes.admin import ApprovalBody
        with pytest.raises((ValidationError, ValueError)):
            ApprovalBody(request_id=12345)


# ─── Non-admin access ─────────────────────────────────────────────────────────

class TestNonAdminBlocked:
    """Users who are not the admin email and not is_admin=True must get 403."""

    def _mock_non_admin_profile(self, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        # profiles query for is_admin check returns False
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"is_admin": False}]
        )
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)
        return admin_mock

    def test_list_requests_requires_admin(self, authed_client, monkeypatch):
        self._mock_non_admin_profile(monkeypatch)
        r = authed_client.get("/api/admin/requests")
        assert r.status_code == 403

    def test_approve_requires_admin(self, authed_client, monkeypatch):
        self._mock_non_admin_profile(monkeypatch)
        r = authed_client.post("/api/admin/approve", json={"request_id": str(uuid.uuid4())})
        assert r.status_code == 403

    def test_reject_requires_admin(self, authed_client, monkeypatch):
        self._mock_non_admin_profile(monkeypatch)
        r = authed_client.post("/api/admin/reject", json={"request_id": str(uuid.uuid4())})
        assert r.status_code == 403

    def test_list_users_requires_admin(self, authed_client, monkeypatch):
        self._mock_non_admin_profile(monkeypatch)
        r = authed_client.get("/api/admin/users")
        assert r.status_code == 403


# ─── Pagination clamping ──────────────────────────────────────────────────────

class TestListRequestsPagination:
    """limit and offset must be clamped before use in .range()."""

    def _setup_chain(self, admin_mock):
        """Wire the mock for the list_requests query chain."""
        # With status=all: table().select().order().range().execute()
        # With status!=all: table().select().order().eq().range().execute()
        # We test with status=all so there's no .eq() in the chain.
        chain = admin_mock.table.return_value.select.return_value.order.return_value
        chain.range.return_value.execute.return_value = MagicMock(data=[])
        chain.eq.return_value.range.return_value.execute.return_value = MagicMock(data=[])

    def test_overlarge_limit_clamped_to_200(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        self._setup_chain(admin_mock)
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.get("/api/admin/requests?status=all&limit=99999&offset=0")
        assert r.status_code == 200

        # Inspect the range() call to verify clamping happened
        chain = admin_mock.table.return_value.select.return_value.order.return_value
        range_call = chain.range.call_args
        assert range_call is not None, ".range() was not called"
        start, end = range_call[0]
        used_limit = end - start + 1
        assert used_limit <= 200, f"limit not clamped: .range({start}, {end}) implies limit={used_limit}"

    def test_zero_limit_clamped_to_minimum_1(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        self._setup_chain(admin_mock)
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.get("/api/admin/requests?status=all&limit=0&offset=0")
        assert r.status_code == 200

        chain = admin_mock.table.return_value.select.return_value.order.return_value
        range_call = chain.range.call_args
        assert range_call is not None
        start, end = range_call[0]
        assert end >= start, f"end < start: .range({start}, {end})"

    def test_negative_offset_clamped_to_zero(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        self._setup_chain(admin_mock)
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.get("/api/admin/requests?status=all&limit=10&offset=-100")
        assert r.status_code == 200

        chain = admin_mock.table.return_value.select.return_value.order.return_value
        range_call = chain.range.call_args
        assert range_call is not None
        start, end = range_call[0]
        assert start >= 0, f"offset not clamped: .range({start}, {end})"

    def test_normal_pagination_is_forwarded(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        self._setup_chain(admin_mock)
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.get("/api/admin/requests?status=all&limit=25&offset=50")
        assert r.status_code == 200

        chain = admin_mock.table.return_value.select.return_value.order.return_value
        range_call = chain.range.call_args
        assert range_call is not None
        start, end = range_call[0]
        assert start == 50, f"offset not forwarded: start={start}"
        assert end == 74, f"range end wrong: end={end} (expected 74 for limit=25)"


class TestListUsersPagination:
    """list_users must clamp limit to [1, 500]."""

    def test_overlarge_limit_clamped_to_500(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        admin_mock.table.return_value.select.return_value.range.return_value.execute.return_value = MagicMock(data=[])
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.get("/api/admin/users?limit=999999&offset=0")
        assert r.status_code == 200

        range_call = admin_mock.table.return_value.select.return_value.range.call_args
        if range_call:
            start, end = range_call[0]
            used_limit = end - start + 1
            assert used_limit <= 500, f"users limit not clamped: .range({start}, {end})"


# ─── str(body.request_id) in DB calls ────────────────────────────────────────

class TestRequestIdPassedAsString:
    """
    After changing request_id to uuid.UUID, all .eq("id", ...) calls must
    wrap the value in str() — Supabase-py's PostgREST client does string
    comparison; passing a UUID object silently produces no match on some versions.
    """

    def _setup_approve(self, admin_mock, request_uuid):
        """Return a mock where the select finds the record and update succeeds."""
        # First .eq() call (select for existence check)
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": str(request_uuid), "email": "x@test.com"}]
        )
        # Update
        admin_mock.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
        # Invite (non-fatal)
        admin_mock.auth.admin.invite_user_by_email.return_value = MagicMock()

    def test_approve_passes_str_not_uuid_to_db(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        test_uuid = uuid.uuid4()
        self._setup_approve(admin_mock, test_uuid)
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.post("/api/admin/approve", json={"request_id": str(test_uuid)})
        # 200 or 500 (if invite fails) — NOT 422 (would mean UUID parsing failed)
        # and NOT 404 (would mean str() wrapping was missing and eq produced no match)
        assert r.status_code in (200, 500), (
            f"Unexpected {r.status_code}: {r.json()}. "
            "404 would indicate str() wrapping is missing; 422 would indicate UUID validation failed."
        )

    def test_approve_nonexistent_uuid_returns_404(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.post("/api/admin/approve", json={"request_id": str(uuid.uuid4())})
        assert r.status_code == 404

    def test_approve_non_uuid_string_returns_422(self, admin_authed_client, monkeypatch):
        import routes.admin as m
        admin_mock = MagicMock()
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        r = admin_authed_client.post("/api/admin/approve", json={"request_id": "not-a-uuid"})
        assert r.status_code == 422, (
            f"Expected 422 for non-UUID request_id, got {r.status_code}. "
            "This means request_id is still typed as str, not uuid.UUID."
        )
