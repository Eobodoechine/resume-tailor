"""
Renderer registry — add a new template by importing its class and adding one line.

Current renderers:
    "fde_docx"  — DOCX template + LibreOffice PDF conversion (production default)
    "fde_html"  — HTML/CSS template + Playwright headless Chrome PDF (new)

To add a future renderer:
    from renderers.my_template import MyTemplateRenderer
    RENDERERS["my_template"] = MyTemplateRenderer
"""
from renderers.fde_docx import FDEDocxRenderer
from renderers.fde_html import FDEHtmlRenderer

RENDERERS: dict = {
    "fde_docx": FDEDocxRenderer,   # DOCX + LibreOffice (default)
    "fde_html": FDEHtmlRenderer,   # HTML + Playwright
}

DEFAULT_TEMPLATE = "fde_docx"


def get_renderer(template_id: str = DEFAULT_TEMPLATE):
    """
    Return an instantiated renderer for the given template_id.
    Falls back to DEFAULT_TEMPLATE if the id is unknown.
    """
    cls = RENDERERS.get(template_id) or RENDERERS[DEFAULT_TEMPLATE]
    return cls()
