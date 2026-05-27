"""
resume_parser.py
~~~~~~~~~~~~~~~~
Convert the structured plain-text resume Claude produces into a ResumeData
TypedDict ready for any renderer (FDEDocxRenderer, etc.).

The plain-text format contract (enforced by the Claude prompt in claude.py):

    Nnamdi Obodoechine | email | phone | location | linkedin

    SUMMARY
    <paragraph>

    EXPERIENCE
    Job Title | Company Name | Month Year – Month Year
    • bullet 1
    • bullet 2

    SKILLS
    Category Name: item1, item2, item3

    EDUCATION
    Degree | School | Year

    CERTIFICATIONS
    Cert name | Issuer | Status
"""
from __future__ import annotations

import re
from typing import Optional
from renderers.base import ResumeData, ExperienceRole, SkillGroup, EducationEntry, CertificationEntry


# Section header keywords and the ResumeData key they map to
_SECTION_MAP = {
    "SUMMARY":         "summary",
    "EXPERIENCE":      "experience",
    "SKILLS":          "skills",
    "EDUCATION":       "education",
    "CERTIFICATIONS":  "certifications",
    "PROJECTS":        "projects",
    "AWARDS":          "awards",
}

# Strip common qualifier prefixes Claude sometimes adds, e.g.
# "PROFESSIONAL SUMMARY" → "SUMMARY", "CORE SKILLS" → "SKILLS"
_PREFIX_RE = re.compile(
    r'^(PROFESSIONAL|CORE|KEY|TECHNICAL|ADDITIONAL|RELEVANT|WORK|CAREER)\s+',
    re.IGNORECASE,
)


def text_to_resume_data(text: str, profile: dict) -> ResumeData:
    """
    Parse a Claude-produced plain-text resume into a ResumeData TypedDict.
    The profile dict fills in contact fields (name, email, phone, location,
    linkedin_url, website).
    """
    sections = _split_sections(text)

    data: ResumeData = {
        "name":     profile.get("full_name", ""),
        "email":    profile.get("email", ""),
        "phone":    profile.get("phone", ""),
        "location": profile.get("location", ""),
        "linkedin": profile.get("linkedin_url"),
        "website":  profile.get("website"),
        "github":   profile.get("github"),
        "tagline":  None,
        "summary":  sections.get("summary", ""),
        "experience":      _parse_experience(sections.get("experience", "")),
        "skills":          _parse_skills(sections.get("skills", "")),
        "education":       _parse_education(sections.get("education", "")),
        "certifications":  _parse_certifications(sections.get("certifications", "")),
        "featured_project": None,
    }
    return data


# ── Section splitter ──────────────────────────────────────────────────────────

def _split_sections(text: str) -> dict[str, str]:
    """Split the flat resume text into named sections."""
    sections: dict[str, str] = {}
    current_key = "header"
    current_lines: list[str] = []

    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        normalized = _PREFIX_RE.sub("", stripped.upper())
        matched = next(
            (v for k, v in _SECTION_MAP.items() if normalized.startswith(k)),
            None,
        )

        if matched:
            if current_lines:
                sections[current_key] = "\n".join(current_lines)
            current_key = matched
            current_lines = []
        else:
            current_lines.append(stripped)

    if current_lines:
        sections[current_key] = "\n".join(current_lines)

    return sections


# ── Section parsers ───────────────────────────────────────────────────────────

def _parse_experience(text: str) -> list[ExperienceRole]:
    """
    Parse the EXPERIENCE block into a list of ExperienceRole dicts.
    Role headers are identified by having exactly 2 pipe characters and NOT
    starting with a bullet marker — same heuristic as pdf_generator._parse_experience().
    """
    if not text:
        return []

    entries: list[ExperienceRole] = []
    current: Optional[ExperienceRole] = None
    bullets: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        is_bullet = line.startswith(("-", "•", "*"))
        is_header = (not is_bullet) and line.count("|") >= 2

        if is_header:
            if current is not None:
                current["bullets"] = bullets
                entries.append(current)
                bullets = []
            parts = [p.strip() for p in line.split("|", 2)]
            current = ExperienceRole(
                title=parts[0] if len(parts) > 0 else line,
                company=parts[1] if len(parts) > 1 else "",
                dates=parts[2] if len(parts) > 2 else "",
                bullets=[],
            )
        elif is_bullet:
            bullets.append(line.lstrip("-•* "))
        else:
            # Plain continuation line — treat as bullet under current role
            if current is not None:
                bullets.append(line)

    if current is not None:
        current["bullets"] = bullets
        entries.append(current)

    return entries


def _parse_skills(text: str) -> list[SkillGroup]:
    """
    Parse SKILLS block.  Each line is expected in "Category: item1, item2" format.
    Items are split on commas to produce a list[str] per group.
    """
    if not text:
        return []

    groups: list[SkillGroup] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            category, _, items_str = line.partition(":")
            items = [i.strip() for i in items_str.split(",") if i.strip()]
            groups.append(SkillGroup(category=category.strip(), items=items))
        else:
            # No category label — treat whole line as a flat skill list
            items = [i.strip() for i in line.split(",") if i.strip()]
            groups.append(SkillGroup(category="", items=items or [line]))
    return groups


def _parse_education(text: str) -> list[EducationEntry]:
    """
    Parse EDUCATION block.  Each line: "Degree | School | Year" or plain text.
    """
    if not text:
        return []

    entries: list[EducationEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        entries.append(EducationEntry(
            degree=parts[0] if len(parts) > 0 else line,
            school=parts[1] if len(parts) > 1 else "",
            detail=parts[2] if len(parts) > 2 else None,
        ))
    return entries


def _parse_certifications(text: str) -> list[CertificationEntry]:
    """
    Parse CERTIFICATIONS block.  Each line: "Cert Name | Issuer | Status" or plain.
    """
    if not text:
        return []

    entries: list[CertificationEntry] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•* ")
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        detail = " • ".join(parts[1:]) if len(parts) > 1 else None
        entries.append(CertificationEntry(name=name, detail=detail))
    return entries
