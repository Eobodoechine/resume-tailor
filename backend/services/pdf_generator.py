"""
ARCHIVED — moved to backend/_archive/pdf_generator.py
Superseded by: backend/renderers/fde_docx.py + backend/services/resume_parser.py

This stub is kept so any forgotten import surfaces a clear DeprecationWarning
rather than an AttributeError or silent failure.  Safe to delete permanently
once all imports have been migrated.

NOTE: The old _parse_skills / _parse_education signatures are INCOMPATIBLE with
the new resume_parser equivalents (SkillGroup.items is now list[str] not str;
_parse_education returns list[EducationEntry] not list[str]).  Re-exporting them
here would let callers silently receive the wrong type, so they are intentionally
NOT re-exported.  Update all callers to import directly from services.resume_parser.
"""
import warnings as _warnings

_warnings.warn(
    "services.pdf_generator is archived. "
    "Use renderers.registry.get_renderer() for PDF generation "
    "and services.resume_parser for parsing.",
    DeprecationWarning,
    stacklevel=2,
)
