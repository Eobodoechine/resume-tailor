"""
Stable contract between JSON extraction and template rendering.

ResumeData is the single TypedDict every renderer consumes.
Renderer is the Protocol every renderer must implement.

Rules for evolving ResumeData over time:
  - Add new fields as Optional — old renderers ignore unknown keys safely.
  - Never remove or rename an existing field — deprecate by keeping the key.
"""
from typing import Optional, Protocol, runtime_checkable
from typing_extensions import TypedDict


class ExperienceRole(TypedDict, total=False):
    title:   str
    company: str
    dates:   str
    bullets: list


class SkillGroup(TypedDict, total=False):
    category: str
    items:    list   # list[str]


class FeaturedProject(TypedDict, total=False):
    name:        str            # project name (bold)
    description: Optional[str]  # one-line description (gray subtitle)
    url:         Optional[str]  # github / live URL
    bullets:     list           # list[str]


class EducationEntry(TypedDict, total=False):
    degree: str            # e.g. "B.B.A., Economics & Management"
    school: str            # e.g. "Georgia Southern University • 2019"
    detail: Optional[str]  # e.g. "Minors: Business Analytics & ERP Systems"


class CertificationEntry(TypedDict, total=False):
    name:   str            # e.g. "Azure AI Fundamentals (AZ-900)"
    detail: Optional[str]  # e.g. "Microsoft • Active"


class TrainingEntry(TypedDict, total=False):
    name:   str            # e.g. "ML with Python & SQL"
    detail: Optional[str]  # e.g. "Harvard Extension • 2022"


class GenericSection(TypedDict, total=False):
    """Catch-all for any section the renderer doesn't have a typed builder for.
    title  — the section heading as it should appear on the resume.
    items  — list of strings rendered as bullet points, OR
             list of dicts with keys: text (str), detail (str), bullets (list[str])
             for structured entries like projects or awards.
    """
    title: str
    items: list


class ResumeData(TypedDict, total=False):
    # Contact — always present
    name:     str
    email:    str
    phone:    str
    location: str
    linkedin: Optional[str]
    website:  Optional[str]
    github:   Optional[str]

    # Body sections — all optional so renderers handle missing gracefully
    tagline:          Optional[str]
    summary:          str
    experience:       list   # list[ExperienceRole]
    featured_project: Optional[FeaturedProject]
    skills:           list   # list[SkillGroup]
    training:         Optional[list]   # list[TrainingEntry] — sidebar
    education:        Optional[list]   # list[EducationEntry]
    certifications:   Optional[list]   # list[CertificationEntry | str]

    # Flexible catch-all: any section not covered by a typed field above.
    # Renderers iterate these in order and append to the appropriate column.
    extra_main_sections:    Optional[list]   # list[GenericSection] — main column
    extra_sidebar_sections: Optional[list]   # list[GenericSection] — sidebar


@runtime_checkable
class Renderer(Protocol):
    def render(self, data: ResumeData) -> bytes:
        """Consume structured resume data and return PDF bytes."""
        ...
