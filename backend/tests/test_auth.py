"""
Tests for backend/routes/auth.py.

Changes this session:
  - _send_via_resend DELETED (dead code — was unused)
  - httpx import REMOVED (was only used by deleted function)
  - RESEND_API_KEY REMOVED from config and auth.py imports
  - should_create_user: False in _send_via_supabase_otp (was True)

Endpoint tests use authed_client with per-test Supabase mocks.
Dead-code tests inspect the source module directly — no HTTP needed.
"""
import inspect
import uuid
import pytest
from unittest.mock import MagicMock, patch


# ─── Dead-code / structural assertions ────────────────────────────────────────

class TestDeadCodeRemoval:
    """Verify the deleted Resend integration left no orphaned code."""

    def _src(self):
        import routes.auth as m
        return inspect.getsource(m)

    def test_send_via_resend_function_deleted(self):
        import routes.auth as m
        assert not hasattr(m, "_send_via_resend"), (
            "_send_via_resend still exists in routes/auth.py — "
            "it was dead code and should have been deleted."
        )

    def test_httpx_not_imported(self):
        """httpx was only used by _send_via_resend; removing the function removes the dep."""
        assert "import httpx" not in self._src(), (
            "httpx is still imported in routes/auth.py — "
            "it was only used by the deleted _send_via_resend function."
        )

    def test_resend_api_key_not_referenced(self):
        """RESEND_API_KEY was removed from config; auth.py must not import it."""
        assert "RESEND_API_KEY" not in self._src(), (
            "RESEND_API_KEY is still referenced in routes/auth.py — "
            "it was removed from config.py this session."
        )

    def test_send_via_resend_not_called_anywhere(self):
        """Belt-and-suspenders: function name must not appear as a string either."""
        assert "_send_via_resend" not in self._src(), (
            "_send_via_resend appears as a string in auth.py — check for dead comments."
        )


# ─── OTP options ──────────────────────────────────────────────────────────────

class TestShouldCreateUser:
    """
    _send_via_supabase_otp must pass should_create_user: False.

    Passing True would let Supabase silently create an auth.users record for
    any email address that requests a magic link — bypassing the approval gate
    entirely. The fix was to set it to False so only pre-existing users receive
    a valid OTP link.
    """

    def test_should_create_user_is_false_in_source(self):
        import routes.auth as m
        src = inspect.getsource(m._send_via_supabase_otp)
        assert (
            '"should_create_user": False' in src
            or "'should_create_user': False" in src
        ), (
            "should_create_user is not set to False in _send_via_supabase_otp. "
            "This allows unapproved users to create Supabase auth accounts via OTP."
        )

    def test_should_create_user_true_absent_from_source(self):
        """Make sure the old True value isn't lurking anywhere in the function."""
        import routes.auth as m
        src = inspect.getsource(m._send_via_supabase_otp)
        assert '"should_create_user": True' not in src, (
            "should_create_user: True found in _send_via_supabase_otp — revert was incomplete."
        )


# ─── Endpoint tests ───────────────────────────────────────────────────────────

class TestRequestAccess:

    def _setup_admin(self, monkeypatch, existing_data=None):
        """Patch get_admin_client in the auth route module."""
        import routes.auth as m
        admin_mock = MagicMock()
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=existing_data if existing_data is not None else []
        )
        admin_mock.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{}])
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)
        return admin_mock

    def test_new_user_request_returns_200(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, existing_data=[])
        r = authed_client.post("/api/auth/request-access", json={
            "email": "newuser@example.com",
            "full_name": "New Person",
            "reason": "I want to tailor resumes",
        })
        assert r.status_code == 200
        assert "received" in r.json().get("message", "").lower()

    def test_already_approved_returns_200_with_approved_message(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, existing_data=[{"status": "approved"}])
        r = authed_client.post("/api/auth/request-access", json={"email": "approved@example.com"})
        assert r.status_code == 200
        assert "approved" in r.json().get("message", "").lower()

    def test_pending_request_returns_200_with_pending_message(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, existing_data=[{"status": "pending"}])
        r = authed_client.post("/api/auth/request-access", json={"email": "pending@example.com"})
        assert r.status_code == 200
        assert "pending" in r.json().get("message", "").lower()

    def test_rejected_request_returns_403(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, existing_data=[{"status": "rejected"}])
        r = authed_client.post("/api/auth/request-access", json={"email": "rejected@example.com"})
        assert r.status_code == 403

    def test_email_is_lowercased_before_db_insert(self, authed_client, monkeypatch):
        """
        Pydantic's EmailStr does not lowercase. The route must normalize the
        email so "USER@EXAMPLE.COM" and "user@example.com" map to the same row.
        """
        import routes.auth as m
        admin_mock = MagicMock()
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        admin_mock.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{}])
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)

        authed_client.post("/api/auth/request-access", json={"email": "UPPER@EXAMPLE.COM"})

        # Verify insert was called with lowercase email
        call_args = admin_mock.table.return_value.insert.call_args
        if call_args:
            inserted = call_args[0][0]
            assert inserted.get("email") == "upper@example.com", (
                f"Email not lowercased before insert: {inserted.get('email')!r}"
            )

    def test_invalid_email_format_returns_422(self, authed_client):
        r = authed_client.post("/api/auth/request-access", json={"email": "not-an-email"})
        assert r.status_code == 422


class TestLogin:

    def _setup_admin(self, monkeypatch, status_data):
        import routes.auth as m
        admin_mock = MagicMock()
        admin_mock.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=status_data
        )
        monkeypatch.setattr(m, "get_admin_client", lambda: admin_mock)
        return admin_mock

    def test_pending_user_cannot_login(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, [{"status": "pending"}])
        r = authed_client.post("/api/auth/login", json={"email": "pending@example.com"})
        assert r.status_code == 403

    def test_rejected_user_cannot_login(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, [{"status": "rejected"}])
        r = authed_client.post("/api/auth/login", json={"email": "rejected@example.com"})
        assert r.status_code == 403

    def test_unknown_email_cannot_login(self, authed_client, monkeypatch):
        self._setup_admin(monkeypatch, [])
        r = authed_client.post("/api/auth/login", json={"email": "unknown@example.com"})
        assert r.status_code == 403

    def test_invalid_email_format_returns_422(self, authed_client):
        r = authed_client.post("/api/auth/login", json={"email": "not-an-email"})
        assert r.status_code == 422


class TestSession:

    def test_delete_session_returns_200(self, authed_client):
        """DELETE /api/auth/session must always succeed, even without a cookie."""
        r = authed_client.delete("/api/auth/session")
        assert r.status_code == 200

    def test_create_session_with_empty_token_returns_400(self, authed_client, monkeypatch):
        import routes.auth as m
        monkeypatch.setattr(m, "get_user_from_token", lambda t: None)
        r = authed_client.post("/api/auth/session", json={"token": ""})
        assert r.status_code == 400

    def test_create_session_invalid_token_returns_401(self, authed_client, monkeypatch):
        import routes.auth as m
        monkeypatch.setattr(m, "get_user_from_token", lambda t: None)
        r = authed_client.post("/api/auth/session", json={"token": "bad-token"})
        assert r.status_code == 401
