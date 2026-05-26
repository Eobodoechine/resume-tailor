"""
Renderer registry — add a new template by importing its class and adding one line.

To add a future renderer:
    from renderers.my_template import MyTemplateRenderer
    RENDERERS["my_template"] = MyTemplateRenderer
"""
from renderers.fde_docx import FDEDocxRenderer
# Future:
# from renderers.image_overlay import ImageOverlayRenderer
# from renderers.docx_template import DocxTemplateRenderer
# from renderers.html_css import HTMLCSSRenderer

RENDERERS: dict = {
    "fde_docx": FDEDocxRenderer,
    # "image_overlay": ImageOverlayRenderer,
    # "docx_template": DocxTemplateRenderer,
}

DEFAULT_TEMPLATE = "fde_docx"


def get_renderer(template_id: str = DEFAULT_TEMPLATE):
    """
    Return an instantiated renderer for the given template_id.
    Falls back to DEFAULT_TEMPLATE if the id is unknown.

    Future: look up template_id from profile.preferred_template, then call:
        generate_pdf(text, profile, template=profile.get("preferred_template", DEFAULT_TEMPLATE))
    """
    cls = RENDERERS.get(template_id) or RENDERERS[DEFAULT_TEMPLATE]
    return cls()
