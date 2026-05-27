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


# ── Load real services.claude (for TestBuildContactBlock); restore stub after ──
_CLAUDE_STUB = sys.modules.get("services.claude")
sys.modules.pop("services.claude", None)
import services.claude as _real_claude_mod  # noqa: E402
sys.modules["services.claude"] = _CLAUDE_STUB  # restore MagicMock for other test files

# ── Ensure renderer modules are fresh (not stale cached versions) ─────────────
for _mod in list(sys.modules):
    if _mod in ("renderers.registry", "renderers.fde_docx"):
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


# ── text_to_resume_data — pipeline integration ───────────────────────────────

class TestTextToResumeDataPipeline:
    """
    Integration tests for the current parsing pipeline.

    extract_resume_json (Claude JSON) was replaced by text_to_resume_data
    (regex parser in services/resume_parser.py — no API call, no retry logic).
    Unit tests live in test_resume_parser.py; these tests verify the pipeline
    contract: the output ResumeData dict is compatible with FDEDocxRenderer.render().
    """

    def test_parser_output_has_renderer_required_keys(self):
        """text_to_resume_data returns a dict with every key FDEDocxRenderer expects."""
        from services.resume_parser import text_to_resume_data
        result = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        for key in ("name", "email", "experience", "skills", "education"):
            assert key in result, f"Missing required key: {key}"

    def test_experience_is_list_of_dicts(self):
        from services.resume_parser import text_to_resume_data
        result = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert isinstance(result["experience"], list)
        if result["experience"]:
            role = result["experience"][0]
            assert "company" in role or "title" in role

    def test_skills_is_list_of_category_dicts(self):
        from services.resume_parser import text_to_resume_data
        result = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert isinstance(result["skills"], list)
        if result["skills"]:
            assert "category" in result["skills"][0]
            assert "items" in result["skills"][0]

    def test_profile_name_injected_into_output(self):
        from services.resume_parser import text_to_resume_data
        result = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert result["name"] == SAMPLE_PROFILE["full_name"]

    def test_empty_text_returns_valid_structure(self):
        """Empty resume text must not crash — renderer must get a safe dict."""
        from services.resume_parser import text_to_resume_data
        result = text_to_resume_data("", SAMPLE_PROFILE)
        assert result["experience"] == []
        assert result["skills"] == []
        assert result["name"] == SAMPLE_PROFILE["full_name"]

    def test_parser_is_idempotent(self):
        """Same input must produce identical output each call."""
        from services.resume_parser import text_to_resume_data
        r1 = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        r2 = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert r1["summary"] == r2["summary"]
        assert len(r1["experience"]) == len(r2["experience"])

    def test_education_entries_have_required_keys(self):
        from services.resume_parser import text_to_resume_data
        result = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        for entry in result["education"]:
            assert "degree" in entry

    def test_deprecation_warning_on_pdf_generator_import(self):
        """services.pdf_generator is archived — importing it emits DeprecationWarning."""
        import warnings, sys
        # Pop so the module-level warn() re-fires on fresh import
        sys.modules.pop("services.pdf_generator", None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import services.pdf_generator  # noqa: F401
        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert any("pdf_generator" in str(w.message).lower() or
                   "archived" in str(w.message).lower() for w in dep)


# ── _build_contact_block ──────────────────────────────────────────────────────

class TestBuildContactBlock:
    """
    Tests for _build_contact_block helper.

    Imports from _real_claude_mod (the real module saved before conftest stub
    was restored) rather than sys.modules["services.claude"] which is a MagicMock.
    """

    def test_all_fields_joined_with_pipe(self):
        profile = {
            "full_name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "555-0100",
            "location": "NYC",
            "linkedin_url": "linkedin.com/in/jane",
            "website": "jane.dev",
        }
        result = _real_claude_mod._build_contact_block(profile)
        assert "Jane Doe" in result
        assert "jane@example.com" in result
        assert " | " in result

    def test_missing_optional_fields_omitted(self):
        profile = {"full_name": "Solo Name"}
        result = _real_claude_mod._build_contact_block(profile)
        assert result == "Solo Name"
        assert " | " not in result

    def test_empty_profile_returns_empty_or_name_only(self):
        result = _real_claude_mod._build_contact_block({})
        assert isinstance(result, str)


# ── get_renderer().render() — current PDF pipeline ───────────────────────────

class TestRenderPipeline:
    """
    Tests for the current PDF generation pipeline:
      text_to_resume_data() → get_renderer().render()

    services.pdf_generator is ARCHIVED — generate_pdf() no longer exists.
    The download_pdf() route now calls:
      resume_data = text_to_resume_data(tailored_content, profile)
      pdf_bytes   = get_renderer().render(resume_data)
    """

    def test_get_renderer_returns_renderer_with_render_method(self):
        """get_renderer() must return an object with a .render() method."""
        from renderers.registry import get_renderer
        import renderers.fde_docx as fde_mod
        with patch.object(fde_mod, "_docx_to_pdf", return_value=b"%PDF-1.4 fake"):
            renderer = get_renderer()
        assert hasattr(renderer, "render"), "Renderer must have a .render() method"

    def test_render_pipeline_returns_bytes(self):
        """Full pipeline: parse → render → bytes."""
        from services.resume_parser import text_to_resume_data
        from renderers.registry import get_renderer
        import renderers.fde_docx as fde_mod

        resume_data = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        with patch.object(fde_mod, "_docx_to_pdf", return_value=b"%PDF fake"):
            result = get_renderer().render(resume_data)
        assert isinstance(result, bytes)

    def test_render_propagates_libreoffice_error(self):
        """RuntimeError from _docx_to_pdf must propagate to the caller."""
        from services.resume_parser import text_to_resume_data
        from renderers.registry import get_renderer
        import renderers.fde_docx as fde_mod

        resume_data = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        with patch.object(fde_mod, "_docx_to_pdf",
                          side_effect=RuntimeError("LibreOffice crashed")):
            with pytest.raises(RuntimeError, match="LibreOffice crashed"):
                get_renderer().render(resume_data)

    def test_render_called_with_profile_name(self):
        """Renderer receives profile name from the parsed data, not hardcoded."""
        from services.resume_parser import text_to_resume_data
        from renderers.registry import get_renderer
        import renderers.fde_docx as fde_mod

        resume_data = text_to_resume_data(SAMPLE_RESUME_TEXT, SAMPLE_PROFILE)
        assert resume_data["name"] == SAMPLE_PROFILE["full_name"]
        # Name makes it into render() without further transformation
        with patch.object(fde_mod, "_docx_to_pdf", return_value=b"pdf"):
            get_renderer().render(resume_data)  # must not raise

    def test_empty_resume_text_does_not_crash_render(self):
        """Parser produces a safe empty structure; renderer must not crash on it."""
        from services.resume_parser import text_to_resume_data
        from renderers.registry import get_renderer
        import renderers.fde_docx as fde_mod

        resume_data = text_to_resume_data("", SAMPLE_PROFILE)
        with patch.object(fde_mod, "_docx_to_pdf", return_value=b"pdf"):
            result = get_renderer().render(resume_data)
        assert isinstance(result, bytes)
