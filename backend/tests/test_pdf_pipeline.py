"""
Tests for the PDF generation pipeline:
  - extract_resume_json (Claude JSON extraction + retry logic)
  - generate_pdf (full orchestration: extract → render → bytes)

Claude API and LibreOffice are always mocked — no external calls.
"""
import json
import sys
import types
from unittest.mock import patch, MagicMock, call
import pytest


# ── Ensure services.claude is the REAL module, not the conftest stub ──────────
for _mod in list(sys.modules):
    if _mod in ("services.claude", "services.pdf_generator", "renderers.registry",
                "renderers.fde_docx"):
        del sys.modules[_mod]

# Stub docx so renderers.fde_docx import doesn't try to open the real template
import renderers.fde_docx  # noqa: E402  (imported for side-effect)


SAMPLE_PROFILE = {
    "full_name": "Nnamdi Obodoechine",
    "email": "nnamdi@example.com",
    "phone": "404-555-0100",
    "location": "Atlanta, GA",
    "linkedin_url": "linkedin.com/in/nnamdi",
}

SAMPLE_RESUME_TEXT = """
NNAMDI OBODOECHINE
Atlanta, GA | nnamdi@example.com | linkedin.com/in/nnamdi

SUMMARY
Results-driven finance professional with 5 years of experience.

EXPERIENCE
Property Tax Specialist | UPS | June 2022 – Present
• Managed $50M property tax portfolio across 12 states.
• Reduced audit exposure by 30% through process improvements.

SKILLS
Systems & Tools: CoStar, Oracle EBS, Power Automate

EDUCATION
B.B.A., Economics | Georgia Southern University | 2019
"""

VALID_JSON = {
    "name": "Nnamdi Obodoechine",
    "email": "nnamdi@example.com",
    "phone": "404-555-0100",
    "location": "Atlanta, GA",
    "tagline": "Finance Professional",
    "summary": "Results-driven finance professional with 5 years of experience.",
    "experience": [
        {
            "title": "Property Tax Specialist",
            "company": "UPS",
            "dates": "June 2022 – Present",
            "bullets": [
                "Managed $50M property tax portfolio across 12 states.",
                "Reduced audit exposure by 30% through process improvements.",
            ],
        }
    ],
    "skills": [
        {"category": "Systems & Tools", "items": ["CoStar", "Oracle EBS", "Power Automate"]},
    ],
    "education": [
        {"degree": "B.B.A., Economics", "school": "Georgia Southern University • 2019"},
    ],
}


# ── extract_resume_json ───────────────────────────────────────────────────────

class TestExtractResumeJson:
    """Tests for services.claude.extract_resume_json."""

    def _make_claude_response(self, text: str) -> MagicMock:
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        return msg

    def test_returns_parsed_dict_on_valid_json(self):
        from services.claude import extract_resume_json
        response = self._make_claude_response(json.dumps(VALID_JSON))
        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.return_value = response
            result = extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert result["name"] == "Nnamdi Obodoechine"
        assert len(result["experience"]) == 1

    def test_strips_markdown_fences(self):
        from services.claude import extract_resume_json
        fenced = f"```json\n{json.dumps(VALID_JSON)}\n```"
        response = self._make_claude_response(fenced)
        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.return_value = response
            result = extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert result["name"] == "Nnamdi Obodoechine"

    def test_strips_plain_code_fence(self):
        from services.claude import extract_resume_json
        fenced = f"```\n{json.dumps(VALID_JSON)}\n```"
        response = self._make_claude_response(fenced)
        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.return_value = response
            result = extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert isinstance(result, dict)

    def test_retries_on_bad_json_and_succeeds_on_second_attempt(self):
        from services.claude import extract_resume_json
        bad_response = self._make_claude_response("not valid json {{{")
        good_response = self._make_claude_response(json.dumps(VALID_JSON))

        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.side_effect = [bad_response, good_response]
            result = extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        assert mock_client.messages.create.call_count == 2
        assert result["name"] == "Nnamdi Obodoechine"

    def test_retries_three_times_then_raises_value_error(self):
        from services.claude import extract_resume_json
        bad = self._make_claude_response("not json at all")

        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.return_value = bad
            with pytest.raises(ValueError, match="3 attempts"):
                extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        assert mock_client.messages.create.call_count == 3

    def test_raises_value_error_with_raw_excerpt_in_message(self):
        from services.claude import extract_resume_json
        bad = self._make_claude_response("garbage output xyz")

        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.return_value = bad
            with pytest.raises(ValueError) as exc_info:
                extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        assert "garbage output" in str(exc_info.value)

    def test_returns_dict_with_expected_keys(self):
        from services.claude import extract_resume_json
        response = self._make_claude_response(json.dumps(VALID_JSON))
        with patch("services.claude.client") as mock_client:
            mock_client.messages.create.return_value = response
            result = extract_resume_json(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        assert "experience" in result
        assert "skills" in result
        assert isinstance(result["experience"], list)
        assert isinstance(result["skills"], list)


# ── _build_contact_block ──────────────────────────────────────────────────────

class TestBuildContactBlock:
    def test_all_fields_joined_with_pipe(self):
        from services.claude import _build_contact_block
        profile = {
            "full_name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "555-0100",
            "location": "NYC",
            "linkedin_url": "linkedin.com/in/jane",
            "website": "jane.dev",
        }
        result = _build_contact_block(profile)
        assert "Jane Doe" in result
        assert "jane@example.com" in result
        assert " | " in result

    def test_missing_optional_fields_omitted(self):
        from services.claude import _build_contact_block
        profile = {"full_name": "Solo Name"}
        result = _build_contact_block(profile)
        assert result == "Solo Name"
        assert " | " not in result

    def test_empty_profile_returns_empty_or_name_only(self):
        from services.claude import _build_contact_block
        result = _build_contact_block({})
        assert isinstance(result, str)


# ── generate_pdf (full pipeline) ─────────────────────────────────────────────

class TestGeneratePdf:
    """Tests for services.pdf_generator.generate_pdf."""

    def _mock_pipeline(self, json_data=None, pdf_bytes=b"fake pdf"):
        """Return context managers that mock Claude + LibreOffice."""
        import renderers.fde_docx as fde_mod

        json_data = json_data or VALID_JSON

        claude_mock = patch(
            "services.pdf_generator.extract_resume_json",
            return_value=json_data,
        )
        lo_mock = patch.object(fde_mod, "_docx_to_pdf", return_value=pdf_bytes)
        return claude_mock, lo_mock

    def test_returns_bytes(self):
        from services.pdf_generator import generate_pdf
        import renderers.fde_docx as fde_mod

        with patch("services.pdf_generator.extract_resume_json", return_value=VALID_JSON), \
             patch.object(fde_mod, "_docx_to_pdf", return_value=b"%PDF fake"):
            result = generate_pdf(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        assert isinstance(result, bytes)
        assert result == b"%PDF fake"

    def test_calls_extract_resume_json_with_text_and_profile(self):
        from services.pdf_generator import generate_pdf
        import renderers.fde_docx as fde_mod

        with patch("services.pdf_generator.extract_resume_json", return_value=VALID_JSON) as mock_extract, \
             patch.object(fde_mod, "_docx_to_pdf", return_value=b"pdf"):
            generate_pdf(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        mock_extract.assert_called_once_with(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

    def test_propagates_value_error_from_extraction(self):
        from services.pdf_generator import generate_pdf

        with patch("services.pdf_generator.extract_resume_json",
                   side_effect=ValueError("bad JSON")):
            with pytest.raises(ValueError, match="bad JSON"):
                generate_pdf(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

    def test_propagates_runtime_error_from_libreoffice(self):
        from services.pdf_generator import generate_pdf
        import renderers.fde_docx as fde_mod

        with patch("services.pdf_generator.extract_resume_json", return_value=VALID_JSON), \
             patch.object(fde_mod, "_docx_to_pdf",
                          side_effect=RuntimeError("LibreOffice failed")):
            with pytest.raises(RuntimeError, match="LibreOffice failed"):
                generate_pdf(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

    def test_uses_fde_docx_renderer_by_default(self):
        from services.pdf_generator import generate_pdf
        from renderers.registry import get_renderer
        import renderers.fde_docx as fde_mod

        with patch("services.pdf_generator.extract_resume_json", return_value=VALID_JSON), \
             patch.object(fde_mod, "_docx_to_pdf", return_value=b"pdf") as mock_lo:
            generate_pdf(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)

        mock_lo.assert_called_once()
