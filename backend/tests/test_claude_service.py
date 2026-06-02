"""
Tests for services/claude.py — synthesis capping and deprecation warning.

All Anthropic API calls are mocked — no real network calls.

Strategy for loading the real module:
  conftest.py stubs services.claude as a MagicMock.  To test actual logic
  we temporarily remove the stub, import the real module into a local
  variable, then RESTORE the stub so other test files (test_http_layer.py
  etc.) that rely on the MagicMock are not affected.
"""
import sys
import warnings
from unittest.mock import patch, MagicMock
import pytest

# ── Load real services.claude; restore stub so other test files are unaffected ─
_STUB = sys.modules.get("services.claude")
sys.modules.pop("services.claude", None)
import services.claude as _real_claude_mod   # noqa: E402
sys.modules["services.claude"] = _STUB       # restore MagicMock for the rest of the session


PROFILE = {"full_name": "Jane Smith", "email": "jane@example.com"}


def _fake_message(text="SYNTHESIZED RESUME"):
    """Build a minimal Anthropic response stub."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage = MagicMock(input_tokens=100, output_tokens=50)
    return msg


# ── MAX_SYNTHESIS_CHARS capping ───────────────────────────────────────────────

class TestSynthesisCapping:
    """
    MAX_SYNTHESIS_CHARS = 150_000 caps total input to prevent context overflow.

    Three scenarios:
      1. All files fit within the limit → all included in the prompt.
      2. Cumulative size would exceed limit → excess files dropped.
      3. Single file alone exceeds limit → truncated to MAX_SYNTHESIS_CHARS.
    """

    @patch.object(_real_claude_mod, "client")
    def test_all_files_within_limit_are_included(self, mock_client):
        mock_client.messages.create.return_value = _fake_message()
        texts = ["A" * 1_000, "B" * 1_000]   # well under 150k
        _real_claude_mod.synthesize_master_resume(texts, PROFILE)

        prompt = mock_client.messages.create.call_args[1]["messages"][0]["content"]
        assert "A" * 100 in prompt
        assert "B" * 100 in prompt

    @patch.object(_real_claude_mod, "client")
    def test_file_beyond_limit_is_excluded(self, mock_client):
        """Second file pushes total over MAX_SYNTHESIS_CHARS — it is dropped."""
        mock_client.messages.create.return_value = _fake_message()
        limit = _real_claude_mod.MAX_SYNTHESIS_CHARS
        # First file: 100 chars under limit. Second file: 200 chars — would exceed.
        texts = ["A" * (limit - 100), "B" * 200]
        _real_claude_mod.synthesize_master_resume(texts, PROFILE)

        prompt = mock_client.messages.create.call_args[1]["messages"][0]["content"]
        assert "A" * 100 in prompt
        assert "B" * 200 not in prompt

    @patch.object(_real_claude_mod, "client")
    def test_single_oversized_file_is_truncated_not_skipped(self, mock_client):
        """
        If even the first file exceeds MAX_SYNTHESIS_CHARS, it must be
        truncated (not skipped) — uses fallback `capped = [texts[0][:limit]]`.
        A skipped-but-empty capped list would produce a 400 'No extracted text'.
        """
        mock_client.messages.create.return_value = _fake_message()
        limit = _real_claude_mod.MAX_SYNTHESIS_CHARS
        texts = ["X" * (limit + 50_000)]   # 50k chars over limit
        _real_claude_mod.synthesize_master_resume(texts, PROFILE)

        prompt = mock_client.messages.create.call_args[1]["messages"][0]["content"]
        # Some content must appear — file wasn't silently dropped
        assert "X" * 100 in prompt
        # Prompt must not be unboundedly long
        assert len(prompt) < limit * 3

    @patch.object(_real_claude_mod, "client")
    def test_synthesis_returns_claude_text(self, mock_client):
        """
        Result must combine both passes (compact + experience).
        Two-pass synthesis: compact call → SUMMARY/SKILLS/EDUCATION,
        experience call → EXPERIENCE.  Combined text is returned.
        """
        mock_client.messages.create.side_effect = [
            _fake_message("COMPACT_SECTIONS"),
            _fake_message("EXPERIENCE_SECTION"),
        ]
        result = _real_claude_mod.synthesize_master_resume(["Some text"], PROFILE)
        assert "COMPACT_SECTIONS" in result
        assert "EXPERIENCE_SECTION" in result

    @patch.object(_real_claude_mod, "client")
    def test_synthesis_sends_single_user_message(self, mock_client):
        """Each API call must use a single user turn, not a conversation."""
        mock_client.messages.create.return_value = _fake_message()
        _real_claude_mod.synthesize_master_resume(["Resume text"], PROFILE)

        # Two calls total (compact pass + experience pass)
        assert mock_client.messages.create.call_count == 2
        for call in mock_client.messages.create.call_args_list:
            kwargs = call[1]
            assert len(kwargs["messages"]) == 1
            assert kwargs["messages"][0]["role"] == "user"

    @patch.object(_real_claude_mod, "client")
    def test_timeout_passed_to_api(self, mock_client):
        """Both API calls must carry the timeout."""
        mock_client.messages.create.return_value = _fake_message()
        _real_claude_mod.synthesize_master_resume(["text"], PROFILE)

        for call in mock_client.messages.create.call_args_list:
            kwargs = call[1]
            assert "timeout" in kwargs
            assert kwargs["timeout"] == _real_claude_mod.API_TIMEOUT

    @patch.object(_real_claude_mod, "client")
    def test_synthesis_max_tokens_is_8000(self, mock_client):
        """
        The EXPERIENCE pass must request at least 8000 output tokens so it
        can write all roles without truncation.  The compact pass (SUMMARY +
        SKILLS + EDUCATION) uses a smaller 2000-token budget — that is fine
        because those sections are short.

        This test pins the experience-pass value so a future accidental
        regression is caught before it ships.
        """
        mock_client.messages.create.return_value = _fake_message()
        _real_claude_mod.synthesize_master_resume(["Resume content"], PROFILE)

        # Second call is the experience pass — must have the large budget
        assert mock_client.messages.create.call_count == 2
        exp_kwargs = mock_client.messages.create.call_args_list[1][1]
        assert exp_kwargs.get("max_tokens", 0) >= 8000, (
            f"experience pass max_tokens={exp_kwargs.get('max_tokens')} — "
            "must be >= 8000 to avoid truncating long EXPERIENCE sections"
        )


# ── DeprecationWarning on sync stream_tailor_resume ──────────────────────────

class TestStreamDeprecationWarning:
    """
    stream_tailor_resume() (sync) should emit DeprecationWarning pointing
    callers to stream_tailor_resume_async() instead.
    """

    @patch.object(_real_claude_mod, "client")
    def test_sync_stream_emits_deprecation_warning(self, mock_client):
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=stream_ctx)
        stream_ctx.__exit__ = MagicMock(return_value=False)
        stream_ctx.text_stream = iter(["hello", " world"])
        mock_client.messages.stream.return_value = stream_ctx

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            list(_real_claude_mod.stream_tailor_resume("master", "jd", PROFILE))

        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1, "Expected at least one DeprecationWarning"
        assert "stream_tailor_resume_async" in str(dep_warnings[0].message)

    @patch.object(_real_claude_mod, "client")
    def test_sync_stream_still_yields_chunks(self, mock_client):
        """Despite the warning, the generator must still yield Claude's output."""
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=stream_ctx)
        stream_ctx.__exit__ = MagicMock(return_value=False)
        stream_ctx.text_stream = iter(["chunk_one", "chunk_two"])
        mock_client.messages.stream.return_value = stream_ctx

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            chunks = list(_real_claude_mod.stream_tailor_resume("master", "jd", PROFILE))

        assert chunks == ["chunk_one", "chunk_two"]


# ── Shared prompt builder ─────────────────────────────────────────────────────

class TestBuildTailorPrompt:
    """_build_tailor_prompt is the single source of truth used by all tailor variants."""

    def test_prompt_contains_master_resume(self):
        prompt = _real_claude_mod._build_tailor_prompt(
            name="Jane",
            contact_block="Jane | jane@example.com",
            target="the role below",
            master_resume="MASTER CONTENT",
            job_description="JD CONTENT",
        )
        assert "MASTER CONTENT" in prompt

    def test_prompt_contains_job_description(self):
        prompt = _real_claude_mod._build_tailor_prompt(
            name="Jane",
            contact_block="Jane | jane@example.com",
            target="the role below",
            master_resume="MASTER",
            job_description="JD CONTENT",
        )
        assert "JD CONTENT" in prompt

    def test_prompt_enforces_pipe_format_for_roles(self):
        """Prompt must instruct Claude on the pipe-separated role header format (TD-05)."""
        prompt = _real_claude_mod._build_tailor_prompt(
            name="Jane",
            contact_block="Jane | jane@example.com",
            target="the role below",
            master_resume="MASTER",
            job_description="JD",
        )
        assert "|" in prompt
        assert "Job Title" in prompt

    def test_contact_block_appears_in_prompt(self):
        block = "Jane Smith | jane@x.com | Atlanta, GA"
        prompt = _real_claude_mod._build_tailor_prompt(
            name="Jane",
            contact_block=block,
            target="Engineer at Acme",
            master_resume="MASTER",
            job_description="JD",
        )
        assert block in prompt

    def test_do_not_fabricate_instruction_present(self):
        """Prompt must explicitly tell Claude not to invent content."""
        prompt = _real_claude_mod._build_tailor_prompt(
            name="Jane",
            contact_block="Jane",
            target="role",
            master_resume="MASTER",
            job_description="JD",
        )
        assert "fabricate" in prompt.lower() or "invent" in prompt.lower()
