"""
Unit tests for renderers/registry.py

Covers:
  - get_renderer("fde_html") returns FDEHtmlRenderer (new path)
  - get_renderer("fde_docx") still works (regression)
  - Unknown key falls back to DEFAULT_TEMPLATE
  - DEFAULT_TEMPLATE has not drifted from "fde_docx"
  - Both renderers satisfy the Renderer protocol
"""
import pytest


class TestGetRenderer:

    def test_fde_html_returns_fde_html_renderer(self):
        from renderers.registry import get_renderer
        from renderers.fde_html import FDEHtmlRenderer
        assert isinstance(get_renderer("fde_html"), FDEHtmlRenderer)

    def test_fde_docx_returns_docx_renderer(self):
        """Regression: existing DOCX path must not break."""
        from renderers.registry import get_renderer
        renderer = get_renderer("fde_docx")
        assert renderer is not None
        assert hasattr(renderer, "render")

    def test_unknown_key_falls_back_to_default(self):
        from renderers.registry import get_renderer, DEFAULT_TEMPLATE
        renderer = get_renderer("does_not_exist_xyz")
        default_renderer = get_renderer(DEFAULT_TEMPLATE)
        assert type(renderer) is type(default_renderer)

    def test_none_key_does_not_raise(self):
        from renderers.registry import get_renderer
        assert get_renderer(None) is not None

    def test_empty_string_falls_back_to_default(self):
        from renderers.registry import get_renderer
        assert get_renderer("") is not None

    def test_default_template_is_fde_docx(self):
        """
        Production safety: DEFAULT_TEMPLATE must remain fde_docx unless
        deliberately changed. Changing it silently switches the render engine
        for all existing users on the next deploy.
        """
        from renderers.registry import DEFAULT_TEMPLATE
        assert DEFAULT_TEMPLATE == "fde_docx", (
            f"DEFAULT_TEMPLATE changed to {DEFAULT_TEMPLATE!r}. "
            "This changes the production rendering engine for all users. "
            "Update this test intentionally if the change is deliberate."
        )

    def test_both_renderers_registered(self):
        from renderers.registry import RENDERERS
        assert "fde_docx" in RENDERERS
        assert "fde_html" in RENDERERS

    def test_fde_html_renderer_has_render(self):
        from renderers.registry import get_renderer
        r = get_renderer("fde_html")
        assert hasattr(r, "render") and callable(r.render)

    def test_fde_html_renderer_has_render_html(self):
        """render_html() is exposed for the preview endpoint — must exist."""
        from renderers.registry import get_renderer
        r = get_renderer("fde_html")
        assert hasattr(r, "render_html") and callable(r.render_html)

    def test_fde_html_satisfies_renderer_protocol(self):
        from renderers.registry import get_renderer
        from renderers.base import Renderer
        assert isinstance(get_renderer("fde_html"), Renderer)

    def test_fde_docx_satisfies_renderer_protocol(self):
        from renderers.registry import get_renderer
        from renderers.base import Renderer
        assert isinstance(get_renderer("fde_docx"), Renderer)
