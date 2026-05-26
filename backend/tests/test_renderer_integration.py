"""
Integration tests for FDEDocxRenderer.

Uses the REAL python-docx and the actual fde_template.docx, but mocks
LibreOffice so the tests don't require the binary to be installed.
Validates that the renderer correctly populates the DOCX structure before
handing off to LibreOffice.
"""
import sys
import types
from copy import deepcopy
from unittest.mock import patch, MagicMock
import pytest

# ── Ensure real docx module is available, not the MagicMock stub ─────────────
# conftest uses setdefault, so if docx is already a MagicMock we swap it out.
import importlib
if isinstance(sys.modules.get("docx"), MagicMock):
    del sys.modules["docx"]
if isinstance(sys.modules.get("docx.api"), MagicMock):
    del sys.modules["docx.api"]

# Stub renderers.base if not real
if "renderers.base" not in sys.modules or not hasattr(sys.modules["renderers.base"], "ResumeData"):
    base_stub = types.ModuleType("renderers.base")
    base_stub.ResumeData = dict
    base_stub.Renderer = object
    sys.modules["renderers.base"] = base_stub

# Clear cached renderer module so it re-imports with real docx
for _mod in list(sys.modules):
    if _mod.startswith("renderers.fde_docx"):
        del sys.modules[_mod]

import renderers.fde_docx as fde_mod
from renderers.fde_docx import FDEDocxRenderer
from docx import Document
import io


MINIMAL_DATA = {
    "name": "Jane Smith",
    "email": "jane@example.com",
    "phone": "555-0100",
    "location": "Atlanta, GA",
    "linkedin": "linkedin.com/in/janesmith",
    "tagline": "AI Product Manager",
    "summary": "Experienced PM with 5 years driving AI products.",
    "experience": [
        {
            "title": "Senior PM",
            "company": "Acme Corp",
            "location": "Atlanta, GA",
            "dates": "Jan 2022 – Present",
            "bullets": [
                "Led roadmap for AI feature set with $2M revenue impact.",
                "Managed cross-functional team of 8 engineers.",
            ],
        }
    ],
    "skills": [
        {"category": "AI & ML", "items": ["Vertex AI", "Claude APIs", "Prompt Engineering"]},
        {"category": "Tools", "items": ["Jira", "Notion", "Figma"]},
    ],
    "certifications": [
        {"name": "AWS Cloud Practitioner", "detail": "Amazon • Active"},
    ],
    "education": [
        {"degree": "B.S. Computer Science", "school": "Georgia Tech • 2018"},
    ],
}


def _render_to_doc(data: dict) -> Document:
    """Run the renderer but intercept LibreOffice, return the in-memory Document."""
    renderer = FDEDocxRenderer()
    docx_bytes_captured = {}

    def fake_docx_to_pdf(docx_bytes):
        docx_bytes_captured["bytes"] = docx_bytes
        return b"%PDF-1.4 fake"

    with patch.object(fde_mod, "_docx_to_pdf", side_effect=fake_docx_to_pdf):
        result = renderer.render(data)

    assert result == b"%PDF-1.4 fake"
    # Re-open the captured DOCX bytes for inspection
    return Document(io.BytesIO(docx_bytes_captured["bytes"]))


def _all_text(doc: Document) -> str:
    """Collect all text from the document for assertions."""
    return " ".join(
        t.text
        for t in doc.element.findall(
            f".//{{{fde_mod.W}}}t"
        )
        if t.text
    )


class TestHeaderPopulation:
    def test_name_appears_uppercased(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "JANE SMITH" in text

    def test_tagline_appears(self):
        doc = _render_to_doc(MINIMAL_DATA)
        assert "AI Product Manager" in _all_text(doc)

    def test_contact_fields_appear(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "jane@example.com" in text
        assert "555-0100" in text
        assert "Atlanta, GA" in text
        assert "linkedin.com/in/janesmith" in text

    def test_both_website_and_github_appear(self):
        data = {**MINIMAL_DATA, "website": "https://jane.dev", "github": "github.com/janesmith"}
        doc = _render_to_doc(data)
        text = _all_text(doc)
        assert "jane.dev" in text
        assert "github.com/janesmith" in text

    def test_missing_optional_contact_fields_dont_crash(self):
        data = {**MINIMAL_DATA, "linkedin": None, "website": None, "github": None}
        doc = _render_to_doc(data)   # should not raise


class TestMainColumnPopulation:
    def test_summary_text_appears(self):
        doc = _render_to_doc(MINIMAL_DATA)
        assert "Experienced PM with 5 years" in _all_text(doc)

    def test_job_title_appears(self):
        doc = _render_to_doc(MINIMAL_DATA)
        assert "Senior PM" in _all_text(doc)

    def test_company_appears(self):
        doc = _render_to_doc(MINIMAL_DATA)
        assert "Acme Corp" in _all_text(doc)

    def test_dates_appear(self):
        doc = _render_to_doc(MINIMAL_DATA)
        assert "Jan 2022" in _all_text(doc)

    def test_bullets_appear(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "Led roadmap for AI feature set" in text
        assert "Managed cross-functional team" in text

    def test_multiple_roles_all_appear(self):
        data = {**MINIMAL_DATA, "experience": [
            {"title": "Role A", "company": "Company X", "dates": "2020–2021", "bullets": ["Bullet A"]},
            {"title": "Role B", "company": "Company Y", "dates": "2021–2022", "bullets": ["Bullet B"]},
        ]}
        doc = _render_to_doc(data)
        text = _all_text(doc)
        assert "Role A" in text and "Company X" in text
        assert "Role B" in text and "Company Y" in text

    def test_many_bullets_all_rendered(self):
        """More bullets than in the template — cloning must handle N bullets."""
        data = {**MINIMAL_DATA, "experience": [
            {
                "title": "Engineer",
                "company": "Corp",
                "dates": "2020–Present",
                "bullets": [f"Bullet {i}" for i in range(6)],
            }
        ]}
        doc = _render_to_doc(data)
        text = _all_text(doc)
        for i in range(6):
            assert f"Bullet {i}" in text

    def test_no_experience_doesnt_crash(self):
        data = {**MINIMAL_DATA, "experience": []}
        _render_to_doc(data)

    def test_empty_bullets_list_doesnt_crash(self):
        data = {**MINIMAL_DATA, "experience": [
            {"title": "PM", "company": "X", "dates": "2020–2021", "bullets": []}
        ]}
        _render_to_doc(data)


class TestFeaturedProject:
    FP = {
        "name": "ResumeAI",
        "description": "Production AI SaaS on GCP",
        "url": "github.com/jane/resumeai",
        "bullets": ["Built with FastAPI + Claude", "10k users in 3 months"],
    }

    def test_featured_project_text_appears(self):
        data = {**MINIMAL_DATA, "featured_project": self.FP}
        doc = _render_to_doc(data)
        text = _all_text(doc)
        assert "ResumeAI" in text
        assert "Production AI SaaS" in text
        assert "github.com/jane/resumeai" in text
        assert "Built with FastAPI" in text

    def test_featured_project_bullets_all_rendered(self):
        data = {**MINIMAL_DATA, "featured_project": self.FP}
        doc = _render_to_doc(data)
        text = _all_text(doc)
        assert "10k users in 3 months" in text

    def test_no_featured_project_section_header_removed(self):
        """When fp=None the 'FEATURED PROJECT' header table should be gone."""
        data = {**MINIMAL_DATA, "featured_project": None}
        doc = _render_to_doc(data)
        text = _all_text(doc)
        assert "FEATURED PROJECT" not in text

    def test_featured_project_without_url_doesnt_crash(self):
        fp = {**self.FP, "url": None}
        data = {**MINIMAL_DATA, "featured_project": fp}
        _render_to_doc(data)


class TestSidebarPopulation:
    def test_skill_categories_appear(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "AI & ML" in text
        assert "Tools" in text

    def test_skill_items_appear(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "Vertex AI" in text
        assert "Claude APIs" in text
        assert "Jira" in text

    def test_certification_appears(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "AWS Cloud Practitioner" in text

    def test_education_appears(self):
        doc = _render_to_doc(MINIMAL_DATA)
        text = _all_text(doc)
        assert "B.S. Computer Science" in text
        assert "Georgia Tech" in text

    def test_empty_skills_doesnt_crash(self):
        data = {**MINIMAL_DATA, "skills": []}
        _render_to_doc(data)

    def test_empty_certs_doesnt_crash(self):
        data = {**MINIMAL_DATA, "certifications": []}
        _render_to_doc(data)

    def test_string_cert_fallback(self):
        """certifications can be plain strings (legacy), not just dicts."""
        data = {**MINIMAL_DATA, "certifications": ["Some Cert Name"]}
        doc = _render_to_doc(data)
        assert "Some Cert Name" in _all_text(doc)

    def test_cert_without_detail_doesnt_crash(self):
        data = {**MINIMAL_DATA, "certifications": [{"name": "No Detail Cert"}]}
        _render_to_doc(data)


class TestRendererOutput:
    def test_render_returns_bytes(self):
        renderer = FDEDocxRenderer()
        with patch.object(fde_mod, "_docx_to_pdf", return_value=b"fake pdf"):
            result = renderer.render(MINIMAL_DATA)
        assert isinstance(result, bytes)

    def test_template_bytes_cached_at_class_level(self):
        """_template_bytes is a class attribute loaded once — non-empty bytes."""
        assert FDEDocxRenderer._template_bytes is not None
        assert isinstance(FDEDocxRenderer._template_bytes, bytes)
        assert len(FDEDocxRenderer._template_bytes) > 1000

    def test_two_renders_dont_share_state(self):
        """Each render gets a fresh deepcopy — mutations don't bleed between calls."""
        renderer = FDEDocxRenderer()
        data_a = {**MINIMAL_DATA, "name": "Alice Jones"}
        data_b = {**MINIMAL_DATA, "name": "Bob Smith"}
        captured = []

        def fake_pdf(docx_bytes):
            captured.append(Document(io.BytesIO(docx_bytes)))
            return b"pdf"

        with patch.object(fde_mod, "_docx_to_pdf", side_effect=fake_pdf):
            renderer.render(data_a)
            renderer.render(data_b)

        text_a = _all_text(captured[0])
        text_b = _all_text(captured[1])
        assert "ALICE JONES" in text_a
        assert "ALICE JONES" not in text_b
        assert "BOB SMITH" in text_b
