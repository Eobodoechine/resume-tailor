"""
Tests for services/supabase_client.py — admin client singleton behavior.

get_admin_client() previously used @lru_cache (risk: cache-lock + client-state
interaction under high concurrency). It was replaced with a plain module-level
singleton (_admin_client). These tests verify the singleton contract:
  - create_client() is called exactly once across N calls.
  - All calls return the same object reference.

The conftest stubs services.supabase_client as a MagicMock. This file clears
that stub and imports the real module to test actual logic.
"""
import sys
import pytest
from unittest.mock import patch, MagicMock


# ── Load real services.supabase_client; restore stub so other test files are unaffected ─
_STUB = sys.modules.get("services.supabase_client")
sys.modules.pop("services.supabase_client", None)
import services.supabase_client as sc_mod   # noqa: E402
sys.modules["services.supabase_client"] = _STUB  # restore MagicMock for the rest of the session


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the module-level singleton before each test so tests are isolated."""
    sc_mod._admin_client = None
    yield
    sc_mod._admin_client = None  # clean up after test


# ── Singleton contract ────────────────────────────────────────────────────────

class TestGetAdminClientSingleton:

    def test_create_client_called_once_on_first_call(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_create:
            sc_mod.get_admin_client()
        assert mock_create.call_count == 1

    def test_second_call_returns_same_instance(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake):
            a = sc_mod.get_admin_client()
            b = sc_mod.get_admin_client()
        assert a is b

    def test_ten_calls_all_return_same_instance(self):
        fake = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake) as mock_create:
            clients = [sc_mod.get_admin_client() for _ in range(10)]
        assert mock_create.call_count == 1
        assert all(c is clients[0] for c in clients)

    def test_create_client_uses_service_key(self):
        """Admin client must be created with the service-role key, not the anon key."""
        with patch("services.supabase_client.create_client") as mock_create:
            mock_create.return_value = MagicMock()
            sc_mod.get_admin_client()
        _, key_arg = mock_create.call_args[0]
        # Import from sc_mod (real module), not services.supabase_client which is
        # restored to the MagicMock stub after module-level import.
        assert key_arg == sc_mod.SUPABASE_SERVICE_KEY

    def test_fresh_client_created_after_reset(self):
        """If the singleton is reset (e.g. after tests), a new client is created."""
        fake_a = MagicMock(name="client_a")
        fake_b = MagicMock(name="client_b")
        sides = [fake_a, fake_b]

        with patch("services.supabase_client.create_client", side_effect=sides):
            client_a = sc_mod.get_admin_client()
            sc_mod._admin_client = None   # simulate reset
            client_b = sc_mod.get_admin_client()

        # After reset, a genuinely new client is created
        assert client_b is fake_b
        assert client_a is not client_b


# ── get_client (per-request RLS client) ──────────────────────────────────────

class TestGetClient:

    def test_get_client_uses_anon_key_not_service_key(self):
        """Per-request RLS client must use the anon key so PostgREST enforces RLS."""
        with patch("services.supabase_client.create_client") as mock_create:
            mock_create.return_value = MagicMock()
            sc_mod.get_client("user.jwt.token")
        _, key_arg = mock_create.call_args[0]
        assert key_arg == sc_mod.SUPABASE_ANON_KEY

    def test_get_client_calls_postgrest_auth_with_token(self):
        """Client must call .postgrest.auth(token) to forward the JWT to PostgREST."""
        fake_client = MagicMock()
        with patch("services.supabase_client.create_client", return_value=fake_client):
            sc_mod.get_client("my.user.token")
        fake_client.postgrest.auth.assert_called_once_with("my.user.token")

    def test_get_client_returns_new_instance_each_call(self):
        """Each call must return a fresh client (per-request JWT state must not leak)."""
        fake_a = MagicMock()
        fake_b = MagicMock()
        with patch("services.supabase_client.create_client", side_effect=[fake_a, fake_b]):
            client_1 = sc_mod.get_client("token_a")
            client_2 = sc_mod.get_client("token_b")
        assert client_1 is not client_2
