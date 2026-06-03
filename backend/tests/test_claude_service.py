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
        """Return value is the text from the Claude response."""
        mock_client.messages.create.return_value = _fake_message("MY MASTER RESUME")
        result = _real_claude_mod.synthesize_master_resume(["Some text"], PROFILE)
        assert result == "MY MASTER RESUME"

    @patch.object(_real_claude_mod, "client")
    def test_synthesis_sends_single_user_message(self, mock_client):
        """synthesize_master_resume must use a single user turn when not truncated."""
        mock_client.messages.create.return_value = _fake_message()
        _real_claude_mod.synthesize_master_resume(["Resume text"], PROFILE)

        # Normal (non-truncated) run = exactly one call
        assert mock_client.messages.create.call_count == 1
        call_kwargs = mock_client.messages.create.call_args[1]
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["role"] == "user"

    @patch.object(_real_claude_mod, "client")
    def test_timeout_passed_to_api(self, mock_client):
        """API call must carry the timeout so slow responses don't hang forever."""
        mock_client.messages.create.return_value = _fake_message()
        _real_claude_mod.synthesize_master_resume(["text"], PROFILE)

        call_kwargs = mock_client.messages.create.call_args[1]
        assert "timeout" in call_kwargs
        assert call_kwargs["timeout"] == _real_claude_mod.API_TIMEOUT

    @patch.object(_real_claude_mod, "client")
    def test_synthesis_max_tokens_is_8000(self, mock_client):
        """
        Synthesis must request 8000 output tokens so long EXPERIENCE sections
        are not cut mid-bullet.  If truncated, a continuation call fires with
        the same 8000-token budget.
        """
        mock_client.messages.create.return_value = _fake_message()
        _real_claude_mod.synthesize_master_resume(["Resume content"], PROFILE)

        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs.get("max_tokens", 0) >= 8000, (
            f"max_tokens={call_kwargs.get('max_tokens')} — "
            "must be >= 8000 to avoid truncating long EXPERIENCE sections"
        )

    @patch.object(_real_claude_mod, "client")
    def test_continuation_fires_when_truncated(self, mock_client):
        """
        When the first call hits max_tokens (output_tokens >= 7900), a
        continuation call fires.  The continuation sends prior output as an
        assistant turn so Claude resumes from the exact stopping point.
        """
        truncated = _fake_message("PARTIAL RESUME\nSome complete line.")
        truncated.usage.output_tokens = 8000   # at the limit → triggers continuation
        cont = _fake_message("REST OF RESUME")
        cont.usage.output_tokens = 100          # clean stop
        mock_client.messages.create.side_effect = [truncated, cont]

        result = _real_claude_mod.synthesize_master_resume(["text"], PROFILE)

        assert mock_client.messages.create.call_count == 2
        assert "PARTIAL RESUME" in result
        assert "REST OF RESUME" in result

        # Continuation must send the prior output as an assistant turn
        cont_msgs = mock_client.messages.create.call_args_list[1][1]["messages"]
        roles = [m["role"] for m in cont_msgs]
        assert roles == ["user", "assistant", "user"]

    @patch.object(_real_claude_mod, "client")
    def test_no_continuation_when_not_truncated(self, mock_client):
        """When output_tokens is well below the limit, no second call fires."""
        msg = _fake_message("COMPLETE RESUME")
        msg.usage.output_tokens = 3000   # far from limit
        mock_client.messages.create.return_value = msg

        _real_claude_mod.synthesize_master_resume(["text"], PROFILE)
        assert mock_client.messages.create.call_count == 1

    @patch.object(_real_claude_mod, "client")
    def test_max_four_continuations(self, mock_client):
        """Loop stops after 4 continuations even if every pass is truncated."""
        always_truncated = _fake_message("CHUNK")
        always_truncated.usage.output_tokens = 8000
        mock_client.messages.create.return_value = always_truncated

        _real_claude_mod.synthesize_master_resume(["text"], PROFILE)

        # 1 original + 4 continuations = 5 total
        assert mock_client.messages.create.call_count == 5


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
