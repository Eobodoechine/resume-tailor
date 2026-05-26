"""
Integration test fixtures.

These tests run against a REAL Supabase project (staging or production).
They are automatically SKIPPED when TEST_SUPABASE_URL is not set so that
the main test suite (unit + HTTP layer) runs cleanly in CI without credentials.

To run integration tests locally:
  export TEST_SUPABASE_URL=https://your-project.supabase.co
  export TEST_SUPABASE_ANON_KEY=eyJ...
  export TEST_SUPABASE_SERVICE_KEY=eyJ...
  pytest tests/integration/ -v

What these tests validate that unit tests cannot:
  - All expected tables exist in the database schema
  - Required columns are present with the correct names
  - RLS policies allow user-scoped reads (user client sees only own rows)
  - RLS policies block cross-user reads (can't see another user's data)
  - Auth flow end-to-end: token → user object → valid user_id
  - Magic link OTP sending doesn't raise on a valid approved email
"""
import os
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring real Supabase credentials (skipped in CI)"
    )


def _require_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"Integration test skipped: {name} not set")
    return val


@pytest.fixture(scope="session")
def supabase_url():
    return _require_env("TEST_SUPABASE_URL")


@pytest.fixture(scope="session")
def supabase_anon_key():
    return _require_env("TEST_SUPABASE_ANON_KEY")


@pytest.fixture(scope="session")
def supabase_service_key():
    return _require_env("TEST_SUPABASE_SERVICE_KEY")


@pytest.fixture(scope="session")
def admin_client(supabase_url, supabase_service_key):
    """Real Supabase admin (service-role) client."""
    from supabase import create_client
    client = create_client(supabase_url, supabase_service_key)
    return client


@pytest.fixture(scope="session")
def test_user_id():
    """
    A stable UUID for a test user that should exist in the test Supabase project.
    Override with TEST_USER_ID env var, or the schema tests will skip user-scoped checks.
    """
    return os.environ.get("TEST_USER_ID", "")
