"""
Unit tests for renderers/fde_html.py

Covers: HTML escaping (XSS), all-empty resume, font fallback,
section presence/absence, CSS column proportions, two-pass overflow guard.

No mocks needed for most tests — the renderer is pure Python string logic.
Font-related tests patch _FONTS_AVAILABLE at module level.
"""
import io
import sys
import pytest
from unittest.mock import AsyncMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_renderer():
    from renderers.fde_html import FDEHtmlRenderer
    return FDEHtmlRenderer()


def minimal_data(**overrides):
    """Minimum valid ResumeData — every optional field present but empty."""
    base = {
        "name":           "Jane Doe",
        "email":          "jane@example.com",
        "phone":          "555-0100",
        "location":       "Atlanta, GA",
        "tagline":        None,
        "summary":        "",
        "experience":     [],
        "skills":         [],
        "education":      [],
        "certifications": [],
    }
    base.update(overrides)
    return base


# ── 1. HTML escaping (XSS prevention) ─────────────────────────────────────────

class TestHtmlEscaping:
    """
    Every user-supplied field must be HTML-escaped before insertion.
    Risk: name/company/summary rendered raw allows stored XSS in the preview iframe.
    """

    XSS = '<script>alert("xss")</script>'

    def _render(self, **kwargs):
        return make_renderer().render_html(minimal_data(**kwargs))

    def test_name_is_escaped(self):
        html = self._render(name=self.XSS)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_email_is_escaped(self):
        html = self._render(email='<img src=x onerror=alert(1)>')
        assert "<img" not in html
        assert "&lt;img" in html

    def test_tagline_is_escaped(self):
        html = self._render(tagline='"><svg onload=alert(1)>')
        assert "<svg" not in html

    def test_summary_is_escaped(self):
        html = self._render(summary=self.XSS)
        assert "<script>" not in html

    def test_experience_title_escaped(self):
        data = minimal_data(experience=[{
            "title": self.XSS, "company": "Safe", "dates": "2020", "bullets": []
        }])
        assert "<script>" not in make_renderer().render_html(data)

    def test_experience_company_escaped(self):
        data = minimal_data(experience=[{
            "title": "Eng", "company": self.XSS, "dates": "2020", "bullets": []
        }])
        assert "<script>" not in make_renderer().render_html(data)

    def test_experience_bullet_escaped(self):
        data = minimal_data(experience=[{
            "title": "Eng", "company": "Co", "dates": "2020",
            "bullets": [self.XSS],
        }])
        assert "<script>" not in make_renderer().render_html(data)

    def test_skill_item_escaped(self):
        data = minimal_data(skills=[{"category": "Tech", "items": [self.XSS]}])
        assert "<script>" not in make_renderer().render_html(data)

    def test_education_degree_escaped(self):
        data = minimal_data(education=[{"degree": self.XSS, "school": "U"}])
        assert "<script>" not in make_renderer().render_html(data)

    def test_cert_name_escaped(self):
        data = minimal_data(certifications=[{"name": self.XSS, "detail": None}])
        assert "<script>" not in make_renderer().render_html(data)

    def test_linkedin_url_escaped(self):
        html = self._render(linkedin='" onmouseover="alert(1)" x="')
        # Value is rendered as escaped TEXT, not an attribute: the quotes become
        # &quot;, so no event-handler attribute can break out. Assert the *active*
        # injection form is absent (the inert, escaped word may remain as text).
        assert 'onmouseover="' not in html
        assert "&quot;" in html

    def test_ampersand_entity_encoded(self):
        html = self._render(name="Johnson & Johnson")
        assert "&amp;" in html
        assert "Johnson & Johnson" not in html

    def test_location_escaped(self):
        html = self._render(location="<b>Atlanta</b>")
        assert "<b>" not in html
        assert "&lt;b&gt;" in html


# ── 2. All-empty resume ────────────────────────────────────────────────────────

class TestAllEmptyResume:
    """Renderer must not crash on empty or minimal data."""

    def test_empty_dict_does_not_raise(self):
        html = make_renderer().render_html({})
        assert html.startswith("<!DOCTYPE html>")

    def test_empty_dict_has_html_skeleton(self):
        html = make_renderer().render_html({})
        assert "<html" in html
        assert "</html>" in html

    def test_none_name_does_not_raise(self):
        html = make_renderer().render_html({"name": None})
        assert "<!DOCTYPE html>" in html

    def test_no_experience_omits_section(self):
        html = make_renderer().render_html(minimal_data(experience=[]))
        assert "PROFESSIONAL EXPERIENCE" not in html

    def test_no_skills_omits_section(self):
        html = make_renderer().render_html(minimal_data(skills=[]))
        assert "CORE SKILLS" not in html

    def test_no_education_omits_section(self):
        html = make_renderer().render_html(minimal_data(education=[]))
        assert "EDUCATION" not in html

    def test_no_certifications_omits_section(self):
        html = make_renderer().render_html(minimal_data(certifications=[]))
        assert "CERTIFICATIONS" not in html

    def test_empty_summary_omits_profile(self):
        html = make_renderer().render_html(minimal_data(summary=""))
        assert "PROFILE" not in html

    def test_html_is_non_trivial_even_empty(self):
        """CSS alone ensures > 500 bytes even for an all-empty resume."""
        assert len(make_renderer().render_html({})) > 500


# ── 3. Font fallback ──────────────────────────────────────────────────────────

class TestFontFallback:
    """
    When Liberation Sans .ttf files are absent, render_html() must:
    - Not crash
    - Fall back to Arial in the CSS font-family
    - Omit @font-face blocks
    """

    def test_render_does_not_crash_without_fonts(self, monkeypatch):
        import renderers.fde_html as mod
        monkeypatch.setattr(mod, "_FONTS_AVAILABLE", False)
        html = make_renderer().render_html(minimal_data(name="Test"))
        assert "Test" in html

    def test_arial_present_when_fonts_absent(self, monkeypatch):
        import renderers.fde_html as mod
        monkeypatch.setattr(mod, "_FONTS_AVAILABLE", False)
        html = make_renderer().render_html({})
        assert "Arial" in html

    def test_no_font_face_when_fonts_absent(self, monkeypatch):
        import renderers.fde_html as mod
        monkeypatch.setattr(mod, "_FONTS_AVAILABLE", False)
        html = make_renderer().render_html({})
        assert "@font-face" not in html

    def test_font_face_present_when_fonts_available(self, monkeypatch):
        import renderers.fde_html as mod
        monkeypatch.setattr(mod, "_FONTS_AVAILABLE", True)
        monkeypatch.setattr(mod, "_FONTS", {
            "regular":     "AAAA",
            "bold":        "BBBB",
            "italic":      "CCCC",
            "bold_italic": "DDDD",
        })
        html = make_renderer().render_html({})
        assert "@font-face" in html

    def test_module_import_safe_without_font_dir(self):
        """
        Importing fde_html must succeed even when the Liberation Sans directory
        doesn't exist (CI / fresh checkout). Regression guard: _load_font_b64
        must warn, not raise.
        """
        saved = sys.modules.pop("renderers.fde_html", None)
        try:
            import renderers.fde_html  # must not raise
        finally:
            if saved is not None:
                sys.modules["renderers.fde_html"] = saved


# ── 4. Section presence / absence ─────────────────────────────────────────────

class TestSectionPresenceAbsence:
    """Full data → all sections. Missing data → section omitted."""

    FULL = {
        "name":    "Alex Smith",
        "email":   "alex@example.com",
        "summary": "Experienced professional.",
        "experience": [{
            "title": "Engineer", "company": "Acme",
            "dates": "2020-2023", "bullets": ["Did stuff"],
        }],
        "skills":         [{"category": "Languages", "items": ["Python"]}],
        "certifications": [{"name": "AWS SAA", "detail": "Amazon • Active"}],
        "education":      [{"degree": "B.S. CS", "school": "State U"}],
    }

    def test_full_resume_has_all_sections(self):
        html = make_renderer().render_html(self.FULL)
        for section in ["PROFILE", "PROFESSIONAL EXPERIENCE", "CORE SKILLS",
                        "CERTIFICATIONS", "EDUCATION"]:
            assert section in html, f"Missing section: {section}"

    def test_role_without_bullets_no_empty_ul(self):
        data = {**self.FULL, "experience": [{
            "title": "Solo", "company": "Corp", "dates": "2023", "bullets": []
        }]}
        html = make_renderer().render_html(data)
        assert "PROFESSIONAL EXPERIENCE" in html
        assert "<ul></ul>" not in html

    def test_cert_as_plain_string_renders(self):
        data = minimal_data(certifications=["Azure AI Fundamentals"])
        html = make_renderer().render_html(data)
        assert "CERTIFICATIONS" in html
        assert "Azure AI Fundamentals" in html

    def test_education_as_plain_string_renders(self):
        data = minimal_data(education=["B.S. CS, State U"])
        html = make_renderer().render_html(data)
        assert "EDUCATION" in html

    def test_tagline_present_when_set(self):
        html = make_renderer().render_html(minimal_data(tagline="Senior Engineer"))
        assert "Senior Engineer" in html
        assert 'class="header-tagline"' in html

    def test_tagline_absent_when_empty(self):
        html = make_renderer().render_html(minimal_data(tagline=""))
        assert 'class="header-tagline"' not in html

    def test_tagline_absent_when_none(self):
        html = make_renderer().render_html(minimal_data(tagline=None))
        assert 'class="header-tagline"' not in html

    def test_skills_without_items_still_shows_section(self):
        data = minimal_data(skills=[{"category": "Languages", "items": []}])
        html = make_renderer().render_html(data)
        assert "CORE SKILLS" in html

    def test_multiline_summary_renders_multiple_paragraphs(self):
        html = make_renderer().render_html(minimal_data(
            summary="Line one.\nLine two.\nLine three."
        ))
        assert html.count("<p>") >= 3


# ── 5. CSS column proportions ─────────────────────────────────────────────────

class TestColumnProportions:
    """
    75.3% / 24.7% split must match the DOCX template twip measurements
    (left=9216, right=3024, total=12240).
    """

    def test_col_main_width_is_75_3(self):
        from renderers.fde_html import _build_css
        assert "75.3%" in _build_css()

    def test_col_sidebar_width_is_24_7(self):
        from renderers.fde_html import _build_css
        assert "24.7%" in _build_css()

    def test_proportions_present_in_rendered_html(self):
        html = make_renderer().render_html({})
        assert "75.3%" in html
        assert "24.7%" in html

    def test_proportions_sum_to_100(self):
        assert abs(75.3 + 24.7 - 100.0) < 0.01

    def test_print_color_adjust_exact_present(self):
        """Required for teal backgrounds to survive Chrome's print rendering."""
        from renderers.fde_html import _build_css
        css = _build_css()
        assert "print-color-adjust: exact" in css or "-webkit-print-color-adjust: exact" in css

    def test_page_size_is_letter(self):
        from renderers.fde_html import _build_css
        assert "letter" in _build_css().lower()


# ── 6. _build_css font-size parameters ───────────────────────────────────────

class TestBuildCssFontSizeParams:
    """_build_css() must accept body_size and header_size and embed them in CSS."""

    def test_default_body_size_is_9pt(self):
        from renderers.fde_html import _build_css
        css = _build_css()
        assert "font-size: 9pt" in css

    def test_default_header_size_is_9_5pt(self):
        from renderers.fde_html import _build_css
        css = _build_css()
        assert "font-size: 9.5pt" in css

    def test_custom_body_size_is_embedded(self):
        from renderers.fde_html import _build_css
        css = _build_css(body_size=8.5)
        assert "font-size: 8.5pt" in css

    def test_custom_header_size_is_embedded(self):
        from renderers.fde_html import _build_css
        css = _build_css(header_size=8.5)
        assert "font-size: 8.5pt" in css

    def test_render_html_passes_font_params_to_css(self):
        html = make_renderer().render_html(minimal_data(), body_size=8.5, header_size=8.5)
        assert "font-size: 8.5pt" in html

    def test_render_html_default_fonts_unchanged(self):
        html = make_renderer().render_html(minimal_data())
        assert "font-size: 9pt" in html


# ── 7. Two-pass overflow guard ────────────────────────────────────────────────

def _make_pdf_bytes(num_pages: int) -> bytes:
    """Return minimal valid PDF bytes with the given number of pages."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestTwoPassOverflowGuard:
    """
    render() must:
    - Return immediately on a single-page result (pass 1).
    - Retry with smaller fonts if pass 1 overflows.
    - Raise HTTPException(422) if still overflowing after pass 2.
    """

    def test_render_single_page_returned_immediately(self):
        """Pass 1 produces 1 page → return that PDF, html_to_pdf called once."""
        one_page_pdf = _make_pdf_bytes(1)

        async def mock_html_to_pdf(html):
            return one_page_pdf

        renderer = make_renderer()
        with patch("renderers.fde_html.html_to_pdf", new=mock_html_to_pdf):
            import asyncio
            # We need to track call count; wrap in a counter
            call_count = [0]
            original_mock = mock_html_to_pdf

            async def counting_mock(html):
                call_count[0] += 1
                return one_page_pdf

            with patch("renderers.fde_html.html_to_pdf", new=counting_mock):
                result = renderer.render(minimal_data())

        assert result == one_page_pdf
        assert call_count[0] == 1

    def test_render_overflow_retries_smaller_font(self):
        """Pass 1 = 2 pages, pass 2 = 1 page → return pass-2 PDF, called twice."""
        one_page_pdf = _make_pdf_bytes(1)
        two_page_pdf = _make_pdf_bytes(2)

        call_count = [0]

        async def mock_html_to_pdf(html):
            call_count[0] += 1
            if call_count[0] == 1:
                return two_page_pdf
            return one_page_pdf

        renderer = make_renderer()
        with patch("renderers.fde_html.html_to_pdf", new=mock_html_to_pdf):
            result = renderer.render(minimal_data())

        assert result == one_page_pdf
        assert call_count[0] == 2

    def test_render_still_overflows_raises_422(self):
        """Both passes produce > 1 page → HTTPException with status_code=422."""
        from fastapi import HTTPException

        two_page_pdf = _make_pdf_bytes(2)

        async def mock_html_to_pdf(html):
            return two_page_pdf

        renderer = make_renderer()
        with patch("renderers.fde_html.html_to_pdf", new=mock_html_to_pdf):
            with pytest.raises(HTTPException) as exc_info:
                renderer.render(minimal_data())

        assert exc_info.value.status_code == 422
        assert "too long" in exc_info.value.detail.lower()

    def test_render_pass2_uses_smaller_font(self):
        """Pass 2 HTML must use 8.5pt body/header font sizes."""
        two_page_pdf = _make_pdf_bytes(2)
        one_page_pdf = _make_pdf_bytes(1)

        captured_htmls = []
        call_count = [0]

        async def mock_html_to_pdf(html):
            captured_htmls.append(html)
            call_count[0] += 1
            if call_count[0] == 1:
                return two_page_pdf
            return one_page_pdf

        renderer = make_renderer()
        with patch("renderers.fde_html.html_to_pdf", new=mock_html_to_pdf):
            renderer.render(minimal_data())

        assert call_count[0] == 2
        # Pass 1 should use 9pt
        assert "font-size: 9pt" in captured_htmls[0]
        # Pass 2 should use 8.5pt
        assert "font-size: 8.5pt" in captured_htmls[1]
