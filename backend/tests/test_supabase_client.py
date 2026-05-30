"""
Tests for services/supabase_client.py.

Changes this session:
  - get_anon_client() converted from a fresh-client-per-call to a cached singleton
    (mirrors get_admin_client; reuses the httpx connection pool across OTP calls)
  - get_user_from_token() now logs expired/JWT/invalid errors at DEBUG level
    (expected, high-frequency) and unexpected errors at WARNING level
    (was silently swallowing everything — made auth failures invisible in logs)

Both admin and anon singletons are tested for the singleton contract:
  - create_client() called exactly once regardless of how many times fn is called
  - All calls return the same object reference

conftest.py does NOT stub services.supabase_client — we import the real module.
"""
import logging
import pytest
from unittest.mock import patch, MagicMock

import services.supabase_client as sc_mod


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset module-level singletons before and after each test."""
    sc_mod._admin_client = None
    sc_mod._anon_client  = None
    yield
    sc_mod._admin_client = None
    sc_mod._anon_client  = None


# ── Admin client singleton ────────────────────────────────────────────────────

class TestGetAdminClientSingleton:

    def test_create_client_called_once_on_first_call(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_cc:
            sc_mod.get_admin_client()
        assert mock_cc.call_count == 1

    def test_second_call_returns_same_instance_without_creating_again(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_cc:
            a = sc_mod.get_admin_client()
            b = sc_mod.get_admin_client()
        assert a is b
        assert mock_cc.call_count == 1, (
            f"create_client called {mock_cc.call_count} times — singleton not working"
        )

    def test_ten_calls_all_return_same_instance(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_cc:
            clients = [sc_mod.get_admin_client() for _ in range(10)]
        assert mock_cc.call_count == 1
        assert all(c is clients[0] for c in clients)

    def test_uses_service_role_key(self):
        """Admin client must be initialized with the SERVICE key, not the anon key."""
        with patch("services.supabase_client.create_client") as mock_cc:
            mock_cc.return_value = MagicMock()
            sc_mod.get_admin_client()
        _, key_arg = mock_cc.call_args[0]
        assert key_arg == sc_mod.SUPABASE_SERVICE_KEY, (
            f"Admin client created with wrong key: {key_arg!r}"
        )

    def test_new_client_after_singleton_reset(self):
        fake_a = MagicMock(name="client_a")
        fake_b = MagicMock(name="client_b")
        with patch("services.supabase_client.create_client", side_effect=[fake_a, fake_b]):
            sc_mod.get_admin_client()
            sc_mod._admin_client = None   # simulate process restart
            second = sc_mod.get_admin_client()
        assert second is fake_b


# ── Anon client singleton (new this session) ──────────────────────────────────

class TestGetAnonClientSingleton:
    """
    get_anon_client() was previously non-cached (new client per call).
    This session converted it to a module-level singleton to reuse the
    httpx connection pool across OTP calls (mirrors get_admin_client pattern).
    """

    def test_create_client_called_once(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_cc:
            sc_mod.get_anon_client()
        assert mock_cc.call_count == 1

    def test_second_call_returns_same_instance(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_cc:
            a = sc_mod.get_anon_client()
            b = sc_mod.get_anon_client()
        assert a is b
        assert mock_cc.call_count == 1, (
            "get_anon_client() called create_client() more than once — "
            "the singleton caching this session added is missing or broken."
        )

    def test_uses_anon_key_not_service_key(self):
        """Anon client must use the ANON key — NOT the service role key."""
        with patch("services.supabase_client.create_client") as mock_cc:
            mock_cc.return_value = MagicMock()
            sc_mod.get_anon_client()
        _, key_arg = mock_cc.call_args[0]
        assert key_arg == sc_mod.SUPABASE_ANON_KEY, (
            f"Anon client created with wrong key: {key_arg!r}"
        )
        assert key_arg != sc_mod.SUPABASE_SERVICE_KEY, (
            "Anon client was initialized with the SERVICE key — this exposes admin access!"
        )


# ── get_client (per-request RLS client) ──────────────────────────────────────

class TestGetClient:

    def test_uses_anon_key(self):
        """RLS client must use the anon key so PostgREST enforces row-level security."""
        with patch("services.supabase_client.create_client") as mock_cc:
            mock_cc.return_value = MagicMock()
            sc_mod.get_client("user.jwt.token")
        _, key_arg = mock_cc.call_args[0]
        assert key_arg == sc_mod.SUPABASE_ANON_KEY

    def test_forwards_jwt_via_postgrest_auth(self):
        fake_client = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake_client):
            sc_mod.get_client("my.user.token")
        fake_client.postgrest.auth.assert_called_once_with("my.user.token")

    def test_returns_fresh_client_each_call(self):
        """Each request must get a fresh client — per-request JWT state must not leak."""
        fake_a = MagicMock()
        fake_b = MagicMock()
        with patch("services.supabase_client.create_client", side_effect=[fake_a, fake_b]):
            c1 = sc_mod.get_client("token_a")
            c2 = sc_mod.get_client("token_b")
        assert c1 is not c2


# ── get_user_from_token logging ───────────────────────────────────────────────

class TestGetUserFromTokenLogging:
    """
    Before this session: all exceptions were silently swallowed (return None, no log).
    After: JWT/expired/invalid → DEBUG (expected, noisy); other errors → WARNING.
    """

    def test_valid_token_returns_user(self):
        fake_user = MagicMock()
        fake_result = MagicMock()
        fake_result.user = fake_user
        with patch("services.supabase_client.get_admin_client") as mock_ac:
            mock_ac.return_value.auth.get_user.return_value = fake_result
            result = sc_mod.get_user_from_token("valid.jwt")
        assert result is fake_user

    def test_invalid_token_returns_none(self):
        with patch("services.supabase_client.get_admin_client") as mock_ac:
            mock_ac.return_value.auth.get_user.side_effect = Exception("invalid jwt token")
            result = sc_mod.get_user_from_token("bad.token")
        assert result is None

    def test_expired_token_logged_at_debug_not_warning(self, caplog):
        """
        Expired tokens are expected noise. Logging at DEBUG keeps WARNING logs
        meaningful (only true surprises) rather than flooding them.
        """
        with patch("services.supabase_client.get_admin_client") as mock_ac:
            mock_ac.return_value.auth.get_user.side_effect = Exception("JWT expired")
            with caplog.at_level(logging.DEBUG, logger="services.supabase_client"):
                sc_mod.get_user_from_token("expired.token")

        # Must produce a debug record, not a warning
        debug_msgs   = [r for r in caplog.records if r.levelno == logging.DEBUG]
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(debug_msgs) >= 1, "Expected a DEBUG log for expired token — got none."
        assert len(warning_msgs) == 0, (
            f"Unexpected WARNING log for expired token: {[r.message for r in warning_msgs]}"
        )

    def test_invalid_jwt_keyword_logged_at_debug(self, caplog):
        with patch("services.supabase_client.get_admin_client") as mock_ac:
            mock_ac.return_value.auth.get_user.side_effect = Exception("invalid JWT format")
            with caplog.at_level(logging.DEBUG, logger="services.supabase_client"):
                sc_mod.get_user_from_token("malformed.token")

        debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug_msgs) >= 1

    def test_network_error_logged_at_warning(self, caplog):
        """
        Unexpected errors (network down, Supabase outage) should be WARNING so
        on-call engineers see them — not silently swallowed or buried in DEBUG.
        """
        with patch("services.supabase_client.get_admin_client") as mock_ac:
            mock_ac.return_value.auth.get_user.side_effect = Exception("connection refused to supabase")
            with caplog.at_level(logging.DEBUG, logger="services.supabase_client"):
                sc_mod.get_user_from_token("any.token")

        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_msgs) >= 1, (
            "Expected a WARNING log for unexpected (network) error — "
            "make sure get_user_from_token logs non-JWT errors at WARNING level."
        )
