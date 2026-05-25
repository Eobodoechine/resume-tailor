"""
Generate a formatted PDF resume from plain text content using WeasyPrint.
"""
import io
import re
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
import os

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


def generate_pdf(tailored_text: str, profile: dict) -> bytes:
    """
    Parse the tailored resume text and render it as a two-column PDF.
    Returns raw PDF bytes.
    """
    parsed = _parse_resume_text(tailored_text, profile)

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
    template = env.get_template("resume.html")
    html_content = template.render(**parsed)

    pdf_bytes = HTML(string=html_content).write_pdf()
    return pdf_bytes


def _parse_resume_text(text: str, profile: dict) -> dict:
    """
    Parse plain-text resume into structured sections for the template.
    """
    sections = {}
    current_section = "header"
    current_lines = []

    section_keywords = {
        "SUMMARY": "summary",
        "EXPERIENCE": "experience",
        "SKILLS": "skills",
        "EDUCATION": "education",
        "CERTIFICATIONS": "certifications",
        "PROJECTS": "projects",
        "AWARDS": "awards",
    }

    lines = text.strip().split("\n")
    header_lines = []
    body_started = False

    for line in lines:
        line = line.strip()
        if not line:
            continue

        matched_section = None
        for keyword, section_name in section_keywords.items():
            if line.upper().startswith(keyword):
                matched_section = section_name
                break

        if matched_section:
            if current_lines:
                sections[current_section] = "\n".join(current_lines)
            current_section = matched_section
            current_lines = []
            body_started = True
        elif not body_started:
            header_lines.append(line)
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines)

    # Parse experience into structured entries
    experience_entries = []
    if "experience" in sections:
        experience_entries = _parse_experience(sections["experience"])

    # Parse skills into categories
    skills_data = []
    if "skills" in sections:
        skills_data = _parse_skills(sections["skills"])

    # Parse education
    education_entries = []
    if "education" in sections:
        education_entries = _parse_education(sections["education"])

    return {
        "name": profile.get("full_name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "location": profile.get("location", ""),
        "linkedin": profile.get("linkedin_url", ""),
        "website": profile.get("website", ""),
        "summary": sections.get("summary", ""),
        "experience": experience_entries,
        "skills": skills_data,
        "education": education_entries,
        "certifications": sections.get("certifications", ""),
        "projects": sections.get("projects", ""),
    }


def _parse_experience(text: str) -> list:
    """Parse experience block into list of role dicts."""
    entries = []
    current = None
    bullets = []

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Detect a new role: line with | separator or starts with a company-like pattern
        if "|" in line or (len(line) < 80 and not line.startswith(("-", "•", "*"))):
            if current:
                current["bullets"] = bullets
                entries.append(current)
                bullets = []
            current = {"header": line, "bullets": []}
        elif line.startswith(("-", "•", "*")):
            bullets.append(line.lstrip("-•* "))
        else:
            bullets.append(line)

    if current:
        current["bullets"] = bullets
        entries.append(current)

    return entries


def _parse_skills(text: str) -> list:
    """Parse skills into category/items pairs."""
    categories = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            parts = line.split(":", 1)
            categories.append({"category": parts[0].strip(), "items": parts[1].strip()})
        else:
            categories.append({"category": "", "items": line})
    return categories


def _parse_education(text: str) -> list:
    entries = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            entries.append(line)
    return entries
