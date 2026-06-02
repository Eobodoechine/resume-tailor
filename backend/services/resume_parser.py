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

import logging
import re
from typing import Optional
from renderers.base import (
    ResumeData, ExperienceRole, SkillGroup, EducationEntry,
    CertificationEntry, TrainingEntry, GenericSection,
)

logger = logging.getLogger(__name__)


# Section header keywords → internal ResumeData key.
# Typed sections get a dedicated key; catch-all sections become GenericSection.
_SECTION_MAP: dict[str, str] = {
    # Typed — have dedicated parsers
    "SUMMARY":         "summary",
    "EXPERIENCE":      "experience",
    "SKILLS":          "skills",
    "EDUCATION":       "education",
    "CERTIFICATIONS":  "certifications",
    "TRAINING":        "training",
    # Generic main-column sections (title preserved, bullets extracted)
    "PROJECTS":        "_generic_main",
    "AWARDS":          "_generic_main",
    "VOLUNTEER":       "_generic_main",
    "PUBLICATIONS":    "_generic_main",
    "LEADERSHIP":      "_generic_main",
    "ACTIVITIES":      "_generic_main",
    "RESEARCH":        "_generic_main",
    "SPEAKING":        "_generic_main",
    # Generic sidebar sections
    "LANGUAGES":       "_generic_sidebar",
    "INTERESTS":       "_generic_sidebar",
    "HOBBIES":         "_generic_sidebar",
}

# Strip common qualifier prefixes Claude sometimes adds, e.g.
# "PROFESSIONAL SUMMARY" → "SUMMARY", "CORE SKILLS" → "SKILLS"
# NOTE (B1): do NOT add VOLUNTEER here. _SECTION_MAP routes "VOLUNTEER" to its
# own generic section; if the prefix were stripped, "VOLUNTEER EXPERIENCE" would
# normalize to "EXPERIENCE" and be merged into the paid work history.
_PREFIX_RE = re.compile(
    r'^(PROFESSIONAL|CORE|KEY|TECHNICAL|ADDITIONAL|RELEVANT|WORK|CAREER)\s+',
    re.IGNORECASE,
)


def _run_parser(section: str, parser, raw: str):
    """Run a section parser with structured logging.

    On failure, logs the section name + a head snippet of the offending block
    (so the exact section is pinpointed in the Render logs) and re-raises so the
    calling route still returns its 500 — we surface, never silently swallow.
    """
    try:
        result = parser(raw)
        logger.debug(
            "[parser] section=%s OK  in_chars=%d  out_items=%s",
            section, len(raw or ""),
            len(result) if isinstance(result, list) else "n/a",
        )
        return result
    except Exception:
        logger.error(
            "[parser] section=%s FAILED  in_chars=%d  head=%r",
            section, len(raw or ""), (raw or "")[:200], exc_info=True,
        )
        raise


def text_to_resume_data(text: str, profile: dict) -> ResumeData:
    """
    Parse a Claude-produced plain-text resume into a ResumeData TypedDict.
    The profile dict fills in contact fields (name, email, phone, location,
    linkedin_url, website).
    """
    logger.info(
        "[parser] text_to_resume_data START  text_chars=%d  has_profile=%s",
        len(text or ""), bool(profile),
    )
    sections, generic_main, generic_sidebar = _split_sections(text)
    logger.info(
        "[parser] sections split  typed=%s  generic_main=%d  generic_sidebar=%d",
        sorted(sections.keys()), len(generic_main), len(generic_sidebar),
    )

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
        "experience":      _run_parser("experience", _parse_experience, sections.get("experience", "")),
        "skills":          _run_parser("skills", _parse_skills, sections.get("skills", "")),
        "education":       _run_parser("education", _parse_education, sections.get("education", "")),
        "certifications":  _run_parser("certifications", _parse_certifications, sections.get("certifications", "")),
        "training":        _run_parser("training", _parse_training, sections.get("training", "")),
        "featured_project": None,
        "extra_main_sections":    generic_main or None,
        "extra_sidebar_sections": generic_sidebar or None,
    }
    logger.info(
        "[parser] text_to_resume_data COMPLETE  roles=%d  skill_groups=%d  edu=%d  "
        "certs=%d  training=%d  generic_main=%d  generic_sidebar=%d  summary_chars=%d",
        len(data["experience"]), len(data["skills"]), len(data["education"] or []),
        len(data["certifications"] or []), len(data["training"] or []),
        len(generic_main), len(generic_sidebar), len(data["summary"] or ""),
    )
    role_count = len(data["experience"])
    if role_count < 2:
        logger.warning(
            "[parser] suspicious role count — possible truncation or parse failure  "
            "roles=%d  input_chars=%d",
            role_count, len(text),
        )
    return data


# ── Section splitter ──────────────────────────────────────────────────────────

def _split_sections(text: str) -> tuple[dict[str, str], list[GenericSection], list[GenericSection]]:
    """Split the flat resume text into named sections.

    Returns:
        typed_sections  — dict mapping ResumeData key → raw text block
        generic_main    — list[GenericSection] for extra main-column sections
        generic_sidebar — list[GenericSection] for extra sidebar sections
    """
    typed: dict[str, str] = {}
    generic_main: list[GenericSection] = []
    generic_sidebar: list[GenericSection] = []

    current_key = "header"
    current_section_type = "typed"  # "typed" | "_generic_main" | "_generic_sidebar"
    current_title = ""
    current_lines: list[str] = []

    def _flush():
        nonlocal current_lines
        if not current_lines:
            return
        block = "\n".join(current_lines)
        if current_section_type == "typed":
            typed[current_key] = block
        elif current_section_type == "_generic_main":
            generic_main.append({"title": current_title, "items": _parse_generic_items(block)})
        elif current_section_type == "_generic_sidebar":
            generic_sidebar.append({"title": current_title, "items": _parse_generic_items(block)})
        current_lines = []

    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        normalized = _PREFIX_RE.sub("", stripped.upper())
        matched_key = None
        matched_type = None
        original_title = stripped  # preserve original capitalisation for display

        for k, v in _SECTION_MAP.items():
            if normalized.startswith(k):
                matched_key = k
                matched_type = v
                break

        if matched_key:
            _flush()
            current_key = matched_type if matched_type not in ("_generic_main", "_generic_sidebar") else matched_key.lower()
            current_section_type = matched_type if matched_type in ("_generic_main", "_generic_sidebar") else "typed"
            current_title = original_title
            logger.debug(
                "[parser] header %r  normalized=%r  -> key=%s  type=%s",
                original_title, normalized, current_key, current_section_type,
            )
        else:
            current_lines.append(stripped)

    _flush()
    return typed, generic_main, generic_sidebar


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


def _parse_training(text: str) -> list[TrainingEntry]:
    """
    Parse TRAINING block.  Each line: "Course Name | Institution | Year" or plain.
    """
    if not text:
        return []

    entries: list[TrainingEntry] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•* ")
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        detail = " • ".join(parts[1:]) if len(parts) > 1 else None
        entries.append(TrainingEntry(name=name, detail=detail))
    return entries


def _parse_generic_items(text: str) -> list:
    """
    Parse a generic section block into a list of items.
    Lines starting with a bullet (•, -, *) become plain strings.
    Lines with pipe separators become dicts: {text, detail, bullets}.
    Consecutive bullet lines after a header line are grouped under that header.
    """
    if not text:
        return []

    items = []
    current_header: dict | None = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        is_bullet = line.startswith(("•", "-", "*", "·"))
        clean = line.lstrip("-•*· ").strip()

        if is_bullet:
            if current_header is not None:
                current_header.setdefault("bullets", []).append(clean)
            else:
                items.append(clean)
        elif "|" in line:
            if current_header is not None:
                items.append(current_header)
            parts = [p.strip() for p in line.split("|")]
            current_header = {"text": parts[0], "detail": " • ".join(parts[1:]) if len(parts) > 1 else None, "bullets": []}
        else:
            if current_header is not None:
                items.append(current_header)
            current_header = {"text": clean, "bullets": []}

    if current_header is not None:
        items.append(current_header)

    return items
