"""
Tests for services/resume_parser.py — the Claude plain-text → ResumeData pipeline.

Covers:
  - _parse_skills:          Category: items format, edge cases
  - _parse_education:       Pipe-separated, multiple entries, plain-text fallback
  - _parse_certifications:  With/without pipe detail, empty input
  - text_to_resume_data():  Full orchestration — sections split, profile injected

resume_parser uses only stdlib + renderers.base (no external I/O), so no
stubs are needed beyond the conftest baseline.
"""
import sys
import pytest

from services.resume_parser import (
    text_to_resume_data,
    _parse_skills,
    _parse_education,
    _parse_certifications,
    _parse_experience,
)


SAMPLE_PROFILE = {
    "full_name":    "Jane Smith",
    "email":        "jane@example.com",
    "phone":        "404-555-0100",
    "location":     "Atlanta, GA",
    "linkedin_url": "linkedin.com/in/janesmith",
    "website":      "jane.dev",
    "github":       "github.com/janesmith",
}

SAMPLE_RESUME = """
SUMMARY
Results-driven engineer with 5 years building AI systems at scale.

EXPERIENCE
Senior Engineer | Acme Corp | Jan 2022 – Present
• Led migration of legacy monolith to microservices; cut deploy time by 60%.
• Cut infrastructure costs by 40%.

Engineer | StartupCo | Jan 2020 – Dec 2021
• Built core product from 0 to 10k users in 6 months.

SKILLS
AI & ML: Python, TensorFlow, Claude API
Dev Tools: Docker, Kubernetes, GitHub Actions

EDUCATION
B.S. Computer Science | Georgia Tech | 2019

CERTIFICATIONS
AWS Solutions Architect | Amazon | Active
Google Cloud Professional
"""


# ── _parse_skills ─────────────────────────────────────────────────────────────

class TestParseSkills:

    def test_single_category_single_item(self):
        result = _parse_skills("Languages: Python")
        assert len(result) == 1
        assert result[0]["category"] == "Languages"
        assert result[0]["items"] == ["Python"]

    def test_single_category_multiple_items(self):
        result = _parse_skills("AI & ML: Python, TensorFlow, Claude API")
        assert len(result) == 1
        assert result[0]["items"] == ["Python", "TensorFlow", "Claude API"]

    def test_multiple_categories(self):
        text = "AI & ML: Python, TensorFlow\nDev Tools: Docker, Kubernetes"
        result = _parse_skills(text)
        assert len(result) == 2
        assert result[0]["category"] == "AI & ML"
        assert result[1]["category"] == "Dev Tools"

    def test_empty_input_returns_empty_list(self):
        assert _parse_skills("") == []

    def test_line_without_colon_produces_uncategorized_group(self):
        """A plain line with no colon → category='' group so no data is lost."""
        result = _parse_skills("Python, SQL, R")
        assert len(result) == 1
        assert result[0]["category"] == ""
        assert "Python" in result[0]["items"]

    def test_items_are_stripped_of_whitespace(self):
        result = _parse_skills("Tools:  Docker ,  Kubernetes ")
        assert result[0]["items"] == ["Docker", "Kubernetes"]

    def test_empty_items_after_comma_split_are_dropped(self):
        """Trailing commas or double commas must not produce empty-string items."""
        result = _parse_skills("Tools: Docker, , Kubernetes,")
        items = result[0]["items"]
        assert "" not in items
        assert "Docker" in items
        assert "Kubernetes" in items

    def test_blank_lines_between_categories_are_ignored(self):
        text = "AI: Python\n\nTools: Docker"
        result = _parse_skills(text)
        assert len(result) == 2

    def test_category_name_preserves_ampersand_and_spaces(self):
        result = _parse_skills("Systems & Tools: CoStar, Oracle EBS")
        assert result[0]["category"] == "Systems & Tools"


# ── _parse_education ──────────────────────────────────────────────────────────

class TestParseEducation:

    def test_pipe_separated_entry(self):
        result = _parse_education("B.S. Computer Science | Georgia Tech | 2019")
        assert len(result) == 1
        assert result[0]["degree"] == "B.S. Computer Science"
        assert result[0]["school"] == "Georgia Tech"
        assert result[0]["detail"] == "2019"

    def test_two_part_entry_no_year(self):
        result = _parse_education("B.S. CS | MIT")
        assert result[0]["degree"] == "B.S. CS"
        assert result[0]["school"] == "MIT"
        assert result[0]["detail"] is None

    def test_multiple_entries(self):
        text = "B.S. CS | MIT | 2018\nM.S. AI | Stanford | 2020"
        result = _parse_education(text)
        assert len(result) == 2
        assert result[0]["degree"] == "B.S. CS"
        assert result[1]["degree"] == "M.S. AI"

    def test_empty_input_returns_empty_list(self):
        assert _parse_education("") == []

    def test_plain_text_no_pipe_still_parsed(self):
        """No-pipe line: entire line becomes degree, school='', detail=None."""
        result = _parse_education("Some certification course 2022")
        assert len(result) == 1
        assert result[0]["degree"] == "Some certification course 2022"
        assert result[0]["school"] == ""

    def test_blank_lines_skipped(self):
        result = _parse_education("B.S. CS | MIT | 2018\n\nM.S. AI | Stanford | 2020\n")
        assert len(result) == 2


# ── _parse_certifications ─────────────────────────────────────────────────────

class TestParseCertifications:

    def test_pipe_with_detail(self):
        result = _parse_certifications("AWS Solutions Architect | Amazon | Active")
        assert len(result) == 1
        assert result[0]["name"] == "AWS Solutions Architect"
        assert "Amazon" in result[0]["detail"]
        assert "Active" in result[0]["detail"]

    def test_no_pipe_plain_name(self):
        result = _parse_certifications("Google Cloud Professional")
        assert len(result) == 1
        assert result[0]["name"] == "Google Cloud Professional"
        assert result[0]["detail"] is None

    def test_multiple_entries(self):
        text = "AWS Cert\nGCP Cert\nAzure Cert"
        result = _parse_certifications(text)
        assert len(result) == 3

    def test_empty_input(self):
        assert _parse_certifications("") == []

    def test_blank_lines_skipped(self):
        result = _parse_certifications("AWS Cert\n\nGCP Cert\n")
        assert len(result) == 2

    def test_bullet_prefix_stripped(self):
        """Leading • or - markers should not appear in the cert name."""
        result = _parse_certifications("• AWS Solutions Architect")
        assert result[0]["name"] == "AWS Solutions Architect"
        assert not result[0]["name"].startswith("•")


# ── text_to_resume_data() — full orchestration ────────────────────────────────

class TestTextToResumeData:
    """Full pipeline: plain text → ResumeData TypedDict."""

    def _parse(self, text=None, profile=None):
        return text_to_resume_data(
            text if text is not None else SAMPLE_RESUME,
            profile if profile is not None else SAMPLE_PROFILE,
        )

    # Contact fields come from profile dict, not text parsing
    def test_name_from_profile(self):
        assert self._parse()["name"] == "Jane Smith"

    def test_email_from_profile(self):
        assert self._parse()["email"] == "jane@example.com"

    def test_phone_from_profile(self):
        assert self._parse()["phone"] == "404-555-0100"

    def test_location_from_profile(self):
        assert self._parse()["location"] == "Atlanta, GA"

    def test_linkedin_from_profile(self):
        assert self._parse()["linkedin"] == "linkedin.com/in/janesmith"

    def test_website_from_profile(self):
        assert self._parse()["website"] == "jane.dev"

    def test_github_from_profile(self):
        assert self._parse()["github"] == "github.com/janesmith"

    # Section parsing
    def test_summary_extracted(self):
        result = self._parse()
        assert "Results-driven engineer" in result["summary"]

    def test_experience_parsed(self):
        result = self._parse()
        assert len(result["experience"]) == 2

    def test_experience_first_role_company(self):
        result = self._parse()
        assert result["experience"][0]["company"] == "Acme Corp"

    def test_experience_bullets_present(self):
        result = self._parse()
        bullets = result["experience"][0]["bullets"]
        assert any("microservices" in b for b in bullets)

    def test_skills_parsed(self):
        result = self._parse()
        categories = [s["category"] for s in result["skills"]]
        assert "AI & ML" in categories
        assert "Dev Tools" in categories

    def test_education_parsed(self):
        result = self._parse()
        assert len(result["education"]) == 1
        assert result["education"][0]["degree"] == "B.S. Computer Science"

    def test_certifications_parsed(self):
        result = self._parse()
        cert_names = [c["name"] for c in result["certifications"]]
        assert any("AWS" in n for n in cert_names)
        assert any("Google" in n for n in cert_names)

    def test_result_has_all_required_keys(self):
        result = self._parse()
        required = {
            "name", "email", "phone", "location", "linkedin", "website",
            "github", "tagline", "summary", "experience", "skills",
            "education", "certifications", "featured_project",
        }
        assert required.issubset(result.keys())

    def test_featured_project_defaults_to_none(self):
        """The parser doesn't produce a featured_project — renderer handles that."""
        assert self._parse()["featured_project"] is None

    def test_tagline_defaults_to_none(self):
        assert self._parse()["tagline"] is None

    # Edge cases
    def test_empty_text_doesnt_crash(self):
        result = self._parse(text="")
        assert result["name"] == "Jane Smith"   # profile always applies
        assert result["experience"] == []
        assert result["skills"] == []

    def test_empty_profile_doesnt_crash(self):
        result = self._parse(profile={})
        assert result["name"] == ""
        assert result["email"] == ""
        # Text sections still parsed
        assert len(result["experience"]) == 2

    def test_idempotent_on_same_input(self):
        """Calling parser twice on same text yields identical results."""
        r1 = self._parse()
        r2 = self._parse()
        assert r1["summary"] == r2["summary"]
        assert len(r1["experience"]) == len(r2["experience"])

    def test_section_header_with_qualifier_prefix(self):
        """'PROFESSIONAL EXPERIENCE' and 'CORE SKILLS' should resolve correctly."""
        resume = """
PROFESSIONAL SUMMARY
Strategic leader.

PROFESSIONAL EXPERIENCE
VP Engineering | BigCo | 2022 – Present
• Built the team.

CORE SKILLS
AI: Claude, GPT

EDUCATION
B.S. CS | MIT | 2018
"""
        result = self._parse(text=resume)
        assert "Strategic leader" in result["summary"]
        assert len(result["experience"]) == 1
        assert result["experience"][0]["company"] == "BigCo"
        assert any(s["category"] == "AI" for s in result["skills"])
