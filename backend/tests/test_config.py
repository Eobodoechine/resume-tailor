"""
Tests for backend/config.py.

Focus: the _require() helper introduced this session.
Key regressions to guard:
  - ADMIN_EMAIL was previously os.environ.get("ADMIN_EMAIL", "enollc21@gmail.com");
    it is now _require("ADMIN_EMAIL") — missing var must RAISE, not silently use
    the hardcoded email.
  - All five required vars use _require(), so a missing one raises at startup
    rather than 502-ing silently on the first API call.
"""
import os
import pytest


class TestRequireHelper:
    """Unit tests for config._require() — no app startup needed."""

    def test_returns_value_when_env_var_is_set(self, monkeypatch):
        monkeypatch.setenv("_RT_TEST_VAR_SET", "hello-world")
        from config import _require
        assert _require("_RT_TEST_VAR_SET") == "hello-world"

    def test_raises_runtime_error_when_var_is_missing(self, monkeypatch):
        monkeypatch.delenv("_RT_TEST_VAR_MISSING", raising=False)
        from config import _require
        with pytest.raises(RuntimeError):
            _require("_RT_TEST_VAR_MISSING")

    def test_raises_runtime_error_when_var_is_empty_string(self, monkeypatch):
        monkeypatch.setenv("_RT_TEST_VAR_EMPTY", "")
        from config import _require
        with pytest.raises(RuntimeError):
            _require("_RT_TEST_VAR_EMPTY")

    def test_error_message_names_the_missing_variable(self, monkeypatch):
        monkeypatch.delenv("SENTINEL_MISSING_XYZ_999", raising=False)
        from config import _require
        with pytest.raises(RuntimeError) as exc_info:
            _require("SENTINEL_MISSING_XYZ_999")
        assert "SENTINEL_MISSING_XYZ_999" in str(exc_info.value)

    def test_error_is_runtime_error_not_key_error(self, monkeypatch):
        """Regression: old code used os.environ["KEY"] which raised KeyError."""
        monkeypatch.delenv("_RT_TEST_KEY_ERROR", raising=False)
        from config import _require
        with pytest.raises(RuntimeError):
            # Must NOT raise KeyError — callers catch RuntimeError for clean
            # startup failure messages in Render logs.
            _require("_RT_TEST_KEY_ERROR")


class TestAdminEmailIsRequired:
    """
    ADMIN_EMAIL was previously:
        ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "enollc21@gmail.com")
    It must now be:
        ADMIN_EMAIL = _require("ADMIN_EMAIL")

    If someone removes the env var from Render, the app must refuse to start —
    not silently use the old developer email as an admin bypass.
    """

    def test_admin_email_comes_from_env_not_hardcoded_default(self):
        import config
        # The hardcoded fallback email must never appear as the live value
        assert config.ADMIN_EMAIL != "enollc21@gmail.com", (
            "ADMIN_EMAIL still uses the hardcoded default. "
            "It must be _require('ADMIN_EMAIL') so removing the env var fails loudly."
        )

    def test_admin_email_equals_env_var(self):
        import config
        assert config.ADMIN_EMAIL == os.environ.get("ADMIN_EMAIL"), (
            "config.ADMIN_EMAIL does not match os.environ['ADMIN_EMAIL']. "
            "It may still have a hardcoded default."
        )

    def test_all_required_vars_are_non_empty(self):
        """Smoke: every _require() call succeeds in the test environment."""
        from config import (
            SUPABASE_URL,
            SUPABASE_ANON_KEY,
            SUPABASE_SERVICE_KEY,
            ANTHROPIC_API_KEY,
            ADMIN_EMAIL,
        )
        for name, val in {
            "SUPABASE_URL":         SUPABASE_URL,
            "SUPABASE_ANON_KEY":    SUPABASE_ANON_KEY,
            "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
            "ANTHROPIC_API_KEY":    ANTHROPIC_API_KEY,
            "ADMIN_EMAIL":          ADMIN_EMAIL,
        }.items():
            assert val, f"config.{name} is empty — _require() should have raised"


class TestOptionalVarsHaveDefaults:
    """Optional vars (CLAUDE_MODEL, RESUME_BUCKET, etc.) must default gracefully."""

    def test_claude_model_has_default(self):
        from config import CLAUDE_MODEL
        assert isinstance(CLAUDE_MODEL, str) and CLAUDE_MODEL

    def test_cookie_max_age_is_integer(self):
        from config import COOKIE_MAX_AGE
        assert isinstance(COOKIE_MAX_AGE, int) and COOKIE_MAX_AGE > 0

    def test_cookie_secure_is_false_in_development(self):
        """ENV=development (set in conftest) → COOKIE_SECURE must be False."""
        from config import COOKIE_SECURE
        assert COOKIE_SECURE is False, (
            f"COOKIE_SECURE={COOKIE_SECURE!r} but ENV=development — should be False"
        )
