"""
Tests for the require_user FastAPI dependency.

Covers: missing header, wrong format, invalid token, valid token,
        HttpOnly cookie fallback (TD-09).
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException


# ── Unit tests: dependency function directly ──────────────────────────────────

def _call_require_user(authorization=None, cookie_token=None):
    """Helper to invoke require_user with a mock Request."""
    from dependencies.auth import require_user
    from unittest.mock import MagicMock

    mock_request = MagicMock()
    mock_request.cookies = {}
    if cookie_token:
        mock_request.cookies["rt_session"] = cookie_token

    return require_user(request=mock_request, authorization=authorization)


class TestRequireUser:

    def test_missing_header_and_no_cookie_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            _call_require_user(authorization=None)
        assert exc.value.status_code == 401

    def test_malformed_bearer_missing_prefix_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            _call_require_user(authorization="Token abc123")
        assert exc.value.status_code == 401

    def test_invalid_token_raises_401(self):
        with patch("dependencies.auth.get_user_from_token", return_value=None):
            with pytest.raises(HTTPException) as exc:
                _call_require_user(authorization="Bearer expired.token.here")
            assert exc.value.status_code == 401

    def test_valid_token_returns_auth_context(self, mock_user, valid_token):
        with patch("dependencies.auth.get_user_from_token", return_value=mock_user):
            ctx = _call_require_user(authorization=f"Bearer {valid_token}")
        assert ctx.user is mock_user
        assert ctx.token == valid_token

    def test_cookie_takes_precedence_over_header(self, mock_user, valid_token):
        """Cookie session should be used when present; Authorization header ignored."""
        with patch("dependencies.auth.get_user_from_token", return_value=mock_user) as mock_get:
            ctx = _call_require_user(authorization="Bearer stale.header.token", cookie_token=valid_token)
        # get_user_from_token should have been called with the cookie token, not the header token
        mock_get.assert_called_once_with(valid_token)
        assert ctx.token == valid_token

    def test_cookie_alone_authenticates_without_header(self, mock_user, valid_token):
        with patch("dependencies.auth.get_user_from_token", return_value=mock_user):
            ctx = _call_require_user(authorization=None, cookie_token=valid_token)
        assert ctx.user is mock_user

    def test_expired_cookie_raises_401(self, valid_token):
        """An invalid cookie token still causes 401 — no silent bypass."""
        with patch("dependencies.auth.get_user_from_token", return_value=None):
            with pytest.raises(HTTPException) as exc:
                _call_require_user(authorization=None, cookie_token="expired.cookie.token")
            assert exc.value.status_code == 401

    def test_bearer_prefix_only_no_token_raises_401(self):
        """'Bearer ' with nothing after the space should be rejected."""
        with pytest.raises(HTTPException) as exc:
            _call_require_user(authorization="Bearer ")
        assert exc.value.status_code == 401
