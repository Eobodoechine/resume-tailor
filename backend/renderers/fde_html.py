"""
FDE HTML Renderer
=================
Converts ResumeData into a complete, self-contained HTML string that matches
the FDE two-column branded resume layout.

Visual spec (mirrored from fde_template.docx proportions):
  Header:    full-width teal bar  —  name (left) + contact (right)
             + full-width teal separator strip below
  Body left: white bg  —  PROFILE · PROFESSIONAL EXPERIENCE   (75.3%)
  Body right: teal bg  —  CORE SKILLS · CERTIFICATIONS · EDUCATION  (24.7%)

  Column ratio from DOCX twips: left=9216, right=3024, total=12240
    left  = 9216/12240 ≈ 75.3%     right = 3024/12240 ≈ 24.7%

  Primary teal: #1c3f3a  — matches fde_template.docx sidebar colour.
                           Update _TEAL if the DOCX template colour changes.

  Font: Liberation Sans — loaded from /usr/share/fonts/truetype/liberation/
        and embedded as base64 @font-face for deterministic rendering
        regardless of what fonts are installed on the server.
        Falls back to Arial/Helvetica if the font files are absent
        (e.g. running without the fonts-liberation package installed).

Usage:
    renderer = FDEHtmlRenderer()
    html     = renderer.render_html(data)   # → HTML string (for iframe preview)
    pdf      = renderer.render(data)        # → PDF bytes  (implements Renderer)
"""
from __future__ import annotations

import base64
import html as _html_module
import logging
import os
from typing import Optional

from renderers.base import ResumeData, Renderer

logger = logging.getLogger(__name__)

# ── FDE brand colours ─────────────────────────────────────────────────────────
# Exact values from fde_template_spec.md
_TEAL            = "#388978"   # Green accent — sidebar bg, section bar strip, dates
_MINT            = "#C8E8E2"   # Light mint — sidebar item text on green bg
_TEXT_ON_TEAL    = "#ffffff"   # White — sidebar section header text
_TEXT_MAIN       = "#1A1A1A"   # Near-black — name, section headers, job titles
_TEXT_MUTED      = "#3A3A3A"   # Dark gray — body text, bullet text
_TEXT_CONTACT    = "#777777"   # Medium gray — contact info, tagline
_TEXT_DATE       = "#388978"   # Green — job dates (same green as accent)

# ── Font loading ──────────────────────────────────────────────────────────────
# Liberation Sans has identical metrics to Arial — the resume was designed with
# Arial in mind.  We embed the TTF as base64 so the Playwright/Chrome PDF
# engine renders identically on any server regardless of installed fonts.
_FONT_DIR = "/usr/share/fonts/truetype/liberation"
_FONT_FILES = {
    "regular":     "LiberationSans-Regular.ttf",
    "bold":        "LiberationSans-Bold.ttf",
    "italic":      "LiberationSans-Italic.ttf",
    "bold_italic": "LiberationSans-BoldItalic.ttf",
}


def _load_font_b64(filename: str) -> Optional[str]:
    path = os.path.join(_FONT_DIR, filename)
    try:
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        logger.debug("[fde_html] font loaded  path=%s  b64_len=%d", path, len(data))
        return data
    except OSError as e:
        logger.warning(
            "[fde_html] font file not found — will fall back to system fonts  "
            "path=%s  error=%s",
            path, e, exc_info=True,
        )
        return None


# Load once at module import — ~100 KB each; acceptable startup cost.
_FONTS: dict[str, Optional[str]] = {
    key: _load_font_b64(filename)
    for key, filename in _FONT_FILES.items()
}
_FONTS_AVAILABLE = all(_FONTS.values())


def _font_face_css() -> str:
    """Return @font-face CSS block with embedded base64 fonts, or empty string."""
    if not _FONTS_AVAILABLE:
        logger.info("[fde_html] Liberation Sans not available — using system font fallback")
        return ""

    weight_map = {
        "regular":     ("400", "normal"),
        "bold":        ("700", "normal"),
        "italic":      ("400", "italic"),
        "bold_italic": ("700", "italic"),
    }
    blocks = []
    for key, (weight, style) in weight_map.items():
        data = _FONTS[key]
        if data:
            blocks.append(
                f"@font-face {{\n"
                f"  font-family: 'LiberationSans';\n"
                f"  font-weight: {weight};\n"
                f"  font-style: {style};\n"
                f"  src: url('data:font/truetype;base64,{data}') format('truetype');\n"
                f"}}"
            )
    return "\n".join(blocks)


# ── HTML escaping helper ──────────────────────────────────────────────────────

def _e(text) -> str:
    """HTML-escape and stringify a value for safe insertion into HTML."""
    return _html_module.escape(str(text or ""), quote=True)


# ── Section fragment builders ─────────────────────────────────────────────────

def _contact_items(data: ResumeData) -> list[str]:
    return [
        _e(v) for v in [
            data.get("location"),
            data.get("email"),
            data.get("phone"),
            data.get("linkedin"),
            data.get("website"),
            data.get("github"),
        ]
        if v
    ]


def _section_bar(title: str, sidebar: bool = False) -> str:
    """Render a section-title bar.
    Main column: narrow green left strip + bold dark text on white (mini-table style).
    Sidebar: bold white text on the existing green background — no strip needed.
    """
    if sidebar:
        return f'<div class="section-bar section-bar--sidebar">{_e(title)}</div>'
    return (
        f'<div class="section-bar section-bar--main">'
        f'<span class="section-bar-strip"></span>'
        f'<span class="section-bar-text">{_e(title)}</span>'
        f'</div>'
    )


def _build_summary(summary: str) -> str:
    if not summary:
        return ""
    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    paras = "".join(f"<p>{_e(ln)}</p>" for ln in lines) if lines else f"<p>{_e(summary)}</p>"
    return _section_bar("PROFILE") + f'<div class="summary">{paras}</div>'


def _build_experience(roles: list) -> str:
    if not roles:
        return ""
    parts = [_section_bar("PROFESSIONAL EXPERIENCE")]
    for role in roles:
        title   = _e(role.get("title", ""))
        company = _e(role.get("company", ""))
        dates   = _e(role.get("dates", ""))
        bullets = role.get("bullets") or []

        company_suffix = f" &mdash; {company}" if company else ""
        bullet_items = "".join(f"<li>{_e(b)}</li>" for b in bullets)
        bullet_html = f"<ul>{bullet_items}</ul>" if bullet_items else ""

        parts.append(
            f'<div class="role">'
            f'<div class="role-title">{title}{company_suffix}</div>'
            f'<div class="role-dates">{dates}</div>'
            f'{bullet_html}'
            f'</div>'
        )
    return "\n".join(parts)


def _build_skills(skills: list) -> str:
    if not skills:
        return ""
    parts = [_section_bar("CORE SKILLS", sidebar=True)]
    for group in skills:
        cat   = (group.get("category") or "").strip()
        items = group.get("items") or []
        if cat:
            parts.append(f'<div class="skill-category">{_e(cat)}</div>')
        for item in items:
            parts.append(f'<div class="skill-item">&#8226; {_e(item)}</div>')
    return "\n".join(parts)


def _build_certifications(certs: list) -> str:
    if not certs:
        return ""
    parts = [_section_bar("CERTIFICATIONS", sidebar=True)]
    for cert in certs:
        if isinstance(cert, dict):
            name   = cert.get("name") or ""
            detail = cert.get("detail")
        else:
            name   = str(cert)
            detail = None
        parts.append(f'<div class="cert-name">{_e(name)}</div>')
        if detail:
            parts.append(f'<div class="cert-detail">{_e(detail)}</div>')
    return "\n".join(parts)


def _build_education(education: list) -> str:
    if not education:
        return ""
    parts = [_section_bar("EDUCATION", sidebar=True)]
    for entry in education:
        if isinstance(entry, dict):
            degree = entry.get("degree") or ""
            school = entry.get("school") or ""
            detail = entry.get("detail")
        else:
            degree = str(entry)
            school = ""
            detail = None
        parts.append(f'<div class="edu-degree">{_e(degree)}</div>')
        if school:
            parts.append(f'<div class="edu-school">{_e(school)}</div>')
        if detail:
            parts.append(f'<div class="edu-detail">{_e(detail)}</div>')
    return "\n".join(parts)


def _build_training(training: list) -> str:
    if not training:
        return ""
    parts = [_section_bar("TRAINING", sidebar=True)]
    for entry in training:
        if isinstance(entry, dict):
            name   = entry.get("name") or ""
            detail = entry.get("detail")
        else:
            name   = str(entry)
            detail = None
        parts.append(f'<div class="cert-name">{_e(name)}</div>')
        if detail:
            parts.append(f'<div class="cert-detail">{_e(detail)}</div>')
    return "\n".join(parts)


def _build_featured_project(fp: dict) -> str:
    if not fp:
        return ""
    name        = _e(fp.get("name") or "")
    description = fp.get("description")
    url         = fp.get("url")
    bullets     = fp.get("bullets") or []

    desc_html   = f' <span class="fp-desc">&mdash; {_e(description)}</span>' if description else ""
    url_html    = f'<div class="fp-url">{_e(url)}</div>' if url else ""
    bullet_html = "".join(f"<li>{_e(b)}</li>" for b in bullets)
    ul_html     = f"<ul>{bullet_html}</ul>" if bullet_html else ""

    return (
        _section_bar("FEATURED PROJECT") +
        f'<div class="fp-header"><span class="fp-name">{name}</span>{desc_html}</div>'
        f'{url_html}{ul_html}'
    )


def _build_generic_main_section(section: dict) -> str:
    """Render a catch-all main-column section (Projects, Awards, Volunteer, etc.)."""
    title = section.get("title") or ""
    items = section.get("items") or []
    if not title and not items:
        return ""

    parts = [_section_bar(title.upper())]
    for item in items:
        if isinstance(item, str):
            parts.append(f'<div class="generic-item">&#8226; {_e(item)}</div>')
        elif isinstance(item, dict):
            text    = item.get("text") or ""
            detail  = item.get("detail")
            bullets = item.get("bullets") or []
            # Wrap each structured entry in a `.role` div so the existing
            # `.role ul` / `.role li` descendant CSS applies (B2). A bare
            # `<ul class="role ul">` does NOT match `.role ul`, so bullets
            # would render with no disc marker or indent.
            block = [f'<div class="role-title">{_e(text)}</div>']
            if detail:
                block.append(f'<div class="role-dates">{_e(detail)}</div>')
            if bullets:
                bullet_html = "".join(f"<li>{_e(b)}</li>" for b in bullets)
                block.append(f'<ul>{bullet_html}</ul>')
            parts.append(f'<div class="role">{"".join(block)}</div>')
    return "\n".join(parts)


def _build_generic_sidebar_section(section: dict) -> str:
    """Render a catch-all sidebar section (Languages, Interests, etc.)."""
    title = section.get("title") or ""
    items = section.get("items") or []
    if not title and not items:
        return ""

    parts = [_section_bar(title.upper(), sidebar=True)]
    for item in items:
        text = item if isinstance(item, str) else (item.get("text") or "")
        parts.append(f'<div class="skill-item">&#8226; {_e(text)}</div>')
    return "\n".join(parts)


# ── CSS ───────────────────────────────────────────────────────────────────────

def _build_css() -> str:
    font_family = (
        "'LiberationSans', Arial, 'Helvetica Neue', sans-serif"
        if _FONTS_AVAILABLE
        else "Arial, 'Helvetica Neue', sans-serif"
    )
    font_faces = _font_face_css()

    return f"""
{font_faces}

/* ── Reset ── */
*, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}

/* ── Page ── */
@page {{ size: letter; margin: 0; }}

html, body {{
  width: 8.5in;
  font-family: {font_family};
  font-size: 9pt;
  color: {_TEXT_MAIN};
  background: white;
  /* Force exact colour reproduction in print — required or teal bg disappears */
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}}

.page {{
  width: 8.5in;
  min-height: 11in;
  display: flex;
  flex-direction: column;
}}

/* ── Header — white bg, dark name, gray contact/tagline ── */
.header {{
  background: white;
  padding: 12pt 20pt 8pt 20pt;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12pt;
}}

.header-name {{
  font-size: 20pt;
  font-weight: 700;
  letter-spacing: 0.5pt;
  text-transform: uppercase;
  color: {_TEXT_MAIN};
  line-height: 1.15;
}}

.header-tagline {{
  font-size: 8pt;
  font-weight: 400;
  color: {_TEXT_CONTACT};
  margin-top: 4pt;
}}

.header-contact {{
  text-align: right;
  font-size: 7.5pt;
  line-height: 1.85;
  color: {_TEXT_CONTACT};
  white-space: nowrap;
  padding-top: 2pt;
}}

/* Full-width green separator bar below header */
.header-sep {{ height: 5pt; background: {_TEAL}; }}

/* ── Body columns ── */
.body {{ display: flex; flex: 1; }}

.col-main {{
  /* 9216 / 12240 ≈ 75.3% — matches DOCX body left-column width */
  width: 75.3%;
  padding: 14pt 16pt 14pt 20pt;
  background: white;
}}

.col-sidebar {{
  /* 3024 / 12240 ≈ 24.7% — matches DOCX body right-column width */
  width: 24.7%;
  background: {_TEAL};
  color: {_TEXT_ON_TEAL};
  padding: 14pt 12pt;
}}

/* ── Section header bars ── */
.section-bar {{ margin-top: 10pt; margin-bottom: 6pt; }}
.section-bar:first-child {{ margin-top: 0; }}

/* Main column: narrow green left strip + bold dark text */
.section-bar--main {{
  display: flex;
  align-items: stretch;
}}
.section-bar-strip {{
  width: 7pt;
  background: {_TEAL};
  flex-shrink: 0;
}}
.section-bar-text {{
  font-size: 9.5pt;
  font-weight: 700;
  letter-spacing: 0.5pt;
  text-transform: uppercase;
  color: {_TEXT_MAIN};
  padding: 1pt 0 1pt 6pt;
}}

/* Sidebar: bold white text — no strip (bg is already green) */
.section-bar--sidebar {{
  font-size: 8pt;
  font-weight: 700;
  letter-spacing: 1pt;
  text-transform: uppercase;
  color: {_TEXT_ON_TEAL};
  margin-top: 10pt;
  margin-bottom: 4pt;
}}
.section-bar--sidebar:first-child {{ margin-top: 0; }}

/* ── Summary / Profile ── */
.summary {{ margin-bottom: 4pt; }}
.summary p {{
  font-size: 8.5pt;
  line-height: 1.5;
  color: {_TEXT_MUTED};
  margin-bottom: 3pt;
}}

/* ── Experience ── */
.role {{
  margin-bottom: 10pt;
  page-break-inside: avoid;
}}

.role-title {{
  font-size: 9pt;
  font-weight: 700;
  color: {_TEXT_MAIN};
  line-height: 1.3;
}}

.role-dates {{
  font-size: 7.5pt;
  font-style: italic;
  color: {_TEAL};
  margin-bottom: 3pt;
}}

.role ul {{
  list-style: disc;
  padding-left: 12pt;
  margin-top: 2pt;
}}

.role li {{
  font-size: 8pt;
  line-height: 1.4;
  margin-bottom: 2pt;
  color: {_TEXT_MUTED};
}}

/* ── Sidebar: Skills ── */
.skill-category {{
  font-size: 7pt;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.6pt;
  color: {_TEXT_ON_TEAL};
  margin-top: 8pt;
  margin-bottom: 2pt;
}}

.skill-item {{
  font-size: 8pt;
  line-height: 1.55;
  color: {_MINT};
}}

/* ── Sidebar: Certifications ── */
.cert-name {{
  font-size: 8pt;
  font-weight: 600;
  color: {_TEXT_ON_TEAL};
  margin-top: 5pt;
  margin-bottom: 1pt;
}}

.cert-detail {{
  font-size: 7.5pt;
  color: {_MINT};
  margin-bottom: 2pt;
}}

/* ── Sidebar: Education ── */
.edu-degree {{
  font-size: 8pt;
  font-weight: 600;
  color: {_TEXT_ON_TEAL};
  margin-top: 5pt;
  margin-bottom: 1pt;
}}

.edu-school {{
  font-size: 7.5pt;
  color: {_MINT};
}}

.edu-detail {{
  font-size: 7.5pt;
  font-style: italic;
  color: {_MINT};
  margin-bottom: 3pt;
}}

/* ── Generic catch-all sections ── */
.generic-item {{
  font-size: 8pt;
  line-height: 1.4;
  margin-bottom: 2pt;
  color: {_TEXT_MUTED};
}}

/* ── Featured Project ── */
.fp-header {{
  margin-bottom: 2pt;
}}
.fp-name {{
  font-size: 9pt;
  font-weight: 700;
  color: {_TEXT_MAIN};
}}
.fp-desc {{
  font-size: 8.5pt;
  color: {_TEXT_CONTACT};
}}
.fp-url {{
  font-size: 8pt;
  font-style: italic;
  color: {_TEAL};
  margin-bottom: 3pt;
}}

/* ── Print media ── */
@media print {{
  html, body {{ margin: 0; padding: 0; }}
}}
"""


# ── Public renderer ───────────────────────────────────────────────────────────

class FDEHtmlRenderer:
    """
    FDE branded resume renderer: ResumeData → HTML or PDF.

    render_html(data)  → complete self-contained HTML string
                          exposed at GET /api/tailor/{id}/preview for the
                          iframe preview when RESUME_PDF_ENGINE=playwright.

    render(data)       → PDF bytes via Playwright headless Chrome.
                          Implements the Renderer protocol.
    """

    def render_html(self, data: ResumeData) -> str:
        """Return a complete, self-contained HTML string for this resume."""
        name    = data.get("name") or ""
        tagline = data.get("tagline") or ""

        logger.info("[fde_html] render_html START  name=%r", name)

        contact_html  = "".join(f"<div>{item}</div>" for item in _contact_items(data))
        tagline_html  = f'<div class="header-tagline">{_e(tagline)}</div>' if tagline else ""

        # Main column — ordered: summary, featured project, experience, then any extras
        main_parts = [
            _build_summary(data.get("summary") or ""),
            _build_featured_project(data.get("featured_project") or {}),
            _build_experience(data.get("experience") or []),
        ]
        for sec in (data.get("extra_main_sections") or []):
            main_parts.append(_build_generic_main_section(sec))
        main_html = "\n".join(p for p in main_parts if p)

        # Sidebar — ordered: skills, certifications, training, education, then any extras
        sidebar_parts = [
            _build_skills(data.get("skills") or []),
            _build_certifications(data.get("certifications") or []),
            _build_training(data.get("training") or []),
            _build_education(data.get("education") or []),
        ]
        for sec in (data.get("extra_sidebar_sections") or []):
            sidebar_parts.append(_build_generic_sidebar_section(sec))
        sidebar_html = "\n".join(p for p in sidebar_parts if p)

        logger.info(
            "[fde_html] sections assembled  name=%r  experience=%d  skills=%d  "
            "certs=%d  training=%d  education=%d  extra_main=%d  extra_sidebar=%d  "
            "featured=%s  fonts_embedded=%s",
            name,
            len(data.get("experience") or []),
            len(data.get("skills") or []),
            len(data.get("certifications") or []),
            len(data.get("training") or []),
            len(data.get("education") or []),
            len(data.get("extra_main_sections") or []),
            len(data.get("extra_sidebar_sections") or []),
            bool(data.get("featured_project")),
            _FONTS_AVAILABLE,
        )

        css = _build_css()

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=8.5in">
<style>
{css}
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <div class="header-left">
      <div class="header-name">{_e(name)}</div>
      {tagline_html}
    </div>
    <div class="header-contact">{contact_html}</div>
  </div>
  <div class="header-sep"></div>

  <div class="body">
    <div class="col-main">
{main_html}
    </div>
    <div class="col-sidebar">
{sidebar_html}
    </div>
  </div>

</div>
</body>
</html>"""

        logger.info(
            "[fde_html] render_html COMPLETE  name=%r  html_len=%d",
            name, len(html),
        )
        return html

    def render(self, data: ResumeData) -> bytes:
        """
        Render HTML then convert to PDF bytes via Playwright.
        Implements the Renderer protocol so this class is a drop-in for
        FDEDocxRenderer in the renderer registry.

        Called from asyncio.to_thread() in the tailor route — runs sync,
        starts its own event loop for the Playwright async calls.
        """
        import asyncio
        from renderers.playwright_pdf import html_to_pdf

        html = self.render_html(data)
        logger.info("[fde_html] render → playwright  html_len=%d", len(html))

        # If called from an already-running event loop (e.g. in tests),
        # asyncio.get_event_loop().run_until_complete() would deadlock.
        # asyncio.run() always creates a fresh loop — safe for thread contexts.
        return asyncio.run(html_to_pdf(html))
