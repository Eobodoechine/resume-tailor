"""
Regression guard: ensure no route sends the literal string "now()" to Postgres.

The bug (TD fixed earlier): master.py and profile.py used "now()" instead of
datetime.now(timezone.utc).isoformat(). This test pins that regression.
"""
import re
import ast
import os
import pytest


ROUTES_DIR = os.path.join(os.path.dirname(__file__), "..", "routes")


def _source(filename: str) -> str:
    with open(os.path.join(ROUTES_DIR, filename)) as f:
        return f.read()


class TestNoNowStringLiteral:

    def _assert_no_now_string(self, source: str, filename: str):
        """Fail if the source contains `"now()"` or `'now()'` as a value."""
        # Simple regex: string literal containing exactly now()
        matches = re.findall(r'["\']now\(\)["\']', source)
        assert not matches, (
            f"{filename} contains literal \"now()\" string — use "
            f"datetime.now(timezone.utc).isoformat() instead. Found: {matches}"
        )

    def test_master_py_no_now_string(self):
        self._assert_no_now_string(_source("master.py"), "master.py")

    def test_profile_py_no_now_string(self):
        self._assert_no_now_string(_source("profile.py"), "profile.py")

    def test_admin_py_no_utcnow(self):
        """admin.py used datetime.utcnow() — verify it's replaced with timezone-aware version."""
        source = _source("admin.py")
        assert "datetime.utcnow()" not in source, (
            "admin.py still uses datetime.utcnow() — replace with "
            "datetime.now(timezone.utc)"
        )


class TestTimestampFormat:
    """Check that datetime.now(timezone.utc).isoformat() is used where timestamps are set."""

    def _route_uses_tz_aware(self, source: str) -> bool:
        return "datetime.now(timezone.utc).isoformat()" in source

    def test_master_py_uses_tz_aware_timestamp(self):
        assert self._route_uses_tz_aware(_source("master.py")), (
            "master.py should use datetime.now(timezone.utc).isoformat() for timestamps"
        )

    def test_profile_py_uses_tz_aware_timestamp(self):
        assert self._route_uses_tz_aware(_source("profile.py")), (
            "profile.py should use datetime.now(timezone.utc).isoformat() for timestamps"
        )

    def test_admin_py_uses_tz_aware_timestamp(self):
        assert self._route_uses_tz_aware(_source("admin.py")), (
            "admin.py should use datetime.now(timezone.utc).isoformat() for timestamps"
        )


class TestPromptsPipeFormat:
    """Regression: Claude prompts must enforce | separator for role headers (TD-05)."""

    def _assert_pipe_format_in_prompt(self, source: str, filename: str):
        # Verify the prompt template requires the pipe-separated role-header format.
        # claude.py uses "Job Title | Company Name | Month Year" — check for that.
        has_pipe = "| " in source
        has_format_instruction = "Job Title" in source or "pipe" in source.lower()
        assert has_pipe and has_format_instruction, (
            f"{filename} prompt is missing the pipe-separator format instruction — "
            "check that 'Job Title | Company Name | Start – End' format is required"
        )

    def test_claude_synthesize_prompt_has_pipe_format(self):
        services_dir = os.path.join(os.path.dirname(__file__), "..", "services")
        with open(os.path.join(services_dir, "claude.py")) as f:
            source = f.read()
        self._assert_pipe_format_in_prompt(source, "claude.py")
