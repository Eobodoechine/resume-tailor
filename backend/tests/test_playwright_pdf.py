"""
Unit tests for renderers/playwright_pdf.py

HIGHEST VALUE: test_emulate_media_called_before_set_content
  Reversing the call order silently drops teal backgrounds from the PDF.
  No exception is raised. No log helps. Only visual review catches it.
  This test is the only automated guard against that failure.

Covers:
  - RuntimeError when playwright is not installed
  - RuntimeError when PDF bytes < 1 KB
  - emulate_media(print) called BEFORE set_content (critical ordering)
  - emulate_media called with media="print" (not "screen")
  - pdf() called with print_background=True
  - Error messages contain diagnostic context

HOW MOCKING WORKS HERE
  `async_playwright` is imported inside html_to_pdf() on every call, not at
  module level.  That means patching sys.modules at import time has no effect
  because the import already happened (or happens after the patch context ends).
  The correct target is the attribute on the already-loaded playwright.async_api
  module: patch("playwright.async_api.async_playwright", mock_fn).
  When html_to_pdf() does `from playwright.async_api import async_playwright`
  it reads that attribute — the mock — rather than the real browser launcher.
"""
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Mock factory ──────────────────────────────────────────────────────────────

def _make_page_mock(pdf_bytes: bytes = b"%PDF-1.4" + b"x" * 2000,
                    call_order: list = None):
    """
    Build a mock Playwright page that records emulate_media / set_content order.
    """
    page = AsyncMock()
    if call_order is not None:
        page.emulate_media = AsyncMock(
            side_effect=lambda **kw: call_order.append(("emulate_media", kw))
        )
        page.set_content = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append(("set_content",))
        )
    else:
        page.emulate_media = AsyncMock()
        page.set_content   = AsyncMock()
    page.pdf = AsyncMock(return_value=pdf_bytes)
    return page


def _make_async_playwright_mock(page):
    """
    Build a mock that can replace playwright.async_api.async_playwright.
    Returns the callable that html_to_pdf() will call as: async with async_playwright() as pw:
    """
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    browser.close    = AsyncMock()

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    pw = AsyncMock()
    pw.chromium = chromium

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=pw)
    cm.__aexit__  = AsyncMock(return_value=False)

    return MagicMock(return_value=cm)


# ── 1. Playwright not installed ───────────────────────────────────────────────

class TestPlaywrightImportMissing:
    @pytest.mark.asyncio
    async def test_raises_runtime_error(self):
        sys.modules.pop("renderers.playwright_pdf", None)
        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            sys.modules, {"playwright": None, "playwright.async_api": None}
        ):
            import renderers.playwright_pdf as mod
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
            assert "playwright" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_error_message_is_actionable(self):
        """Message must tell the operator what to do, not just 'import failed'."""
        sys.modules.pop("renderers.playwright_pdf", None)
        with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            sys.modules, {"playwright": None, "playwright.async_api": None}
        ):
            import renderers.playwright_pdf as mod
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
            msg = str(exc.value)
            assert "install" in msg.lower() or "requirements.txt" in msg


# ── 2. PDF size sanity check ──────────────────────────────────────────────────

class TestPdfSizeCheck:
    @pytest.mark.asyncio
    async def test_raises_when_pdf_is_empty(self):
        import renderers.playwright_pdf as mod
        page    = _make_page_mock(pdf_bytes=b"")
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
        msg = str(exc.value)
        assert "0" in msg or "empty" in msg.lower()

    @pytest.mark.asyncio
    async def test_raises_when_pdf_under_1kb(self):
        import renderers.playwright_pdf as mod
        small   = b"%PDF-1.4" + b"x" * 100   # 108 bytes
        page    = _make_page_mock(pdf_bytes=small)
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
        msg = str(exc.value)
        assert str(len(small)) in msg or "108" in msg

    @pytest.mark.asyncio
    async def test_no_error_when_pdf_exactly_1kb(self):
        import renderers.playwright_pdf as mod
        exact   = b"%PDF-1.4" + b"x" * (1024 - 8)
        page    = _make_page_mock(pdf_bytes=exact)
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            result = await mod.html_to_pdf("<html></html>")
        assert result == exact

    @pytest.mark.asyncio
    async def test_no_error_for_large_pdf(self):
        import renderers.playwright_pdf as mod
        large   = b"%PDF-1.4" + b"x" * 50_000
        page    = _make_page_mock(pdf_bytes=large)
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            result = await mod.html_to_pdf("<html></html>")
        assert result == large

    @pytest.mark.asyncio
    async def test_error_message_mentions_render_failure(self):
        """Error must explain what likely went wrong, not just 'small file'."""
        import renderers.playwright_pdf as mod
        page    = _make_page_mock(pdf_bytes=b"tiny")
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
        msg = str(exc.value).lower()
        assert "render" in msg or "html" in msg


# ── 3. emulate_media call order (HIGHEST VALUE TEST) ─────────────────────────

class TestEmulateMediaOrdering:
    """
    CRITICAL: page.emulate_media(media="print") MUST be called BEFORE
    page.set_content(). Reversing this silently drops teal backgrounds
    from every generated PDF — no exception, no log, just wrong output.
    """

    @pytest.mark.asyncio
    async def test_emulate_media_called_before_set_content(self):
        import renderers.playwright_pdf as mod
        call_order = []
        page    = _make_page_mock(call_order=call_order)
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            await mod.html_to_pdf("<html><body>test</body></html>")

        names = [c[0] for c in call_order]
        assert "emulate_media" in names, "emulate_media was never called"
        assert "set_content"   in names, "set_content was never called"

        em_idx = names.index("emulate_media")
        sc_idx = names.index("set_content")
        assert em_idx < sc_idx, (
            f"emulate_media must come BEFORE set_content. "
            f"Actual order: {names}. "
            "Reversing this silently drops teal backgrounds from the PDF."
        )

    @pytest.mark.asyncio
    async def test_emulate_media_called_with_print(self):
        """media= argument must be 'print', not 'screen' or omitted."""
        import renderers.playwright_pdf as mod
        call_order = []
        page    = _make_page_mock(call_order=call_order)
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            await mod.html_to_pdf("<html></html>")

        em_calls = [c for c in call_order if c[0] == "emulate_media"]
        assert em_calls, "emulate_media was never called"
        kwargs = em_calls[0][1]
        assert kwargs.get("media") == "print", (
            f"Expected emulate_media(media='print'), got kwargs: {kwargs}"
        )

    @pytest.mark.asyncio
    async def test_print_background_is_true(self):
        """pdf() must be called with print_background=True — else backgrounds vanish."""
        import renderers.playwright_pdf as mod
        pdf_kwargs = []
        page = _make_page_mock()
        page.pdf = AsyncMock(
            side_effect=lambda **kw: pdf_kwargs.append(kw) or (b"%PDF" + b"x" * 2000)
        )
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            await mod.html_to_pdf("<html></html>")

        assert pdf_kwargs, "page.pdf() was never called"
        assert pdf_kwargs[0].get("print_background") is True, (
            f"pdf() must be called with print_background=True. Got: {pdf_kwargs[0]}"
        )

    @pytest.mark.asyncio
    async def test_wait_until_domcontentloaded(self):
        """set_content must use wait_until='domcontentloaded'."""
        import renderers.playwright_pdf as mod
        sc_kwargs = []
        page = _make_page_mock()
        page.set_content = AsyncMock(
            side_effect=lambda *a, **kw: sc_kwargs.append(kw)
        )
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            await mod.html_to_pdf("<html></html>")

        assert sc_kwargs, "set_content was never called"
        assert sc_kwargs[0].get("wait_until") == "domcontentloaded", (
            f"Expected wait_until='domcontentloaded'. Got: {sc_kwargs[0]}"
        )

    @pytest.mark.asyncio
    async def test_format_is_letter(self):
        """Default format must be 'Letter', not 'A4'."""
        import renderers.playwright_pdf as mod
        pdf_kwargs = []
        page = _make_page_mock()
        page.pdf = AsyncMock(
            side_effect=lambda **kw: pdf_kwargs.append(kw) or (b"%PDF" + b"x" * 2000)
        )
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            await mod.html_to_pdf("<html></html>")

        assert pdf_kwargs[0].get("format") == "Letter"


# ── 4. Error message content ──────────────────────────────────────────────────

class TestErrorMessageContent:
    """Every failure path must include actionable diagnostic context."""

    @pytest.mark.asyncio
    async def test_set_content_failure_mentions_set_content(self):
        import renderers.playwright_pdf as mod
        page = _make_page_mock()
        page.set_content = AsyncMock(side_effect=Exception("boom"))
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
        assert "set_content" in str(exc.value)

    @pytest.mark.asyncio
    async def test_pdf_call_failure_mentions_pdf(self):
        import renderers.playwright_pdf as mod
        page = _make_page_mock()
        page.pdf = AsyncMock(side_effect=Exception("pdf crash"))
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
        assert "pdf" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_emulate_media_failure_mentions_emulate_media(self):
        import renderers.playwright_pdf as mod
        page = _make_page_mock()
        page.emulate_media = AsyncMock(side_effect=Exception("media fail"))
        mock_ap = _make_async_playwright_mock(page)
        with patch("playwright.async_api.async_playwright", mock_ap):
            with pytest.raises(RuntimeError) as exc:
                await mod.html_to_pdf("<html></html>")
        assert "emulate_media" in str(exc.value)
