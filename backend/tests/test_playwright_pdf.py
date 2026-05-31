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
"""
import sys
import importlib
import pytest
from unittest.mock import AsyncMock, MagicMock


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


def _make_pw_mock(page):
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

    mock_mod = MagicMock()
    mock_mod.async_playwright = MagicMock(return_value=cm)
    return mock_mod


def _load_fresh(mock_mod):
    """Patch playwright.async_api and reload playwright_pdf to pick it up."""
    sys.modules.pop("renderers.playwright_pdf", None)
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        sys.modules, {"playwright.async_api": mock_mod}
    ):
        import renderers.playwright_pdf as mod
        return mod


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
        page    = _make_page_mock(pdf_bytes=b"")
        mod_api = _make_pw_mock(page)
        mod     = _load_fresh(mod_api)
        with pytest.raises(RuntimeError) as exc:
            await mod.html_to_pdf("<html></html>")
        msg = str(exc.value)
        assert "0" in msg or "empty" in msg.lower()

    @pytest.mark.asyncio
    async def test_raises_when_pdf_under_1kb(self):
        small = b"%PDF-1.4" + b"x" * 100   # 108 bytes
        page  = _make_page_mock(pdf_bytes=small)
        mod   = _load_fresh(_make_pw_mock(page))
        with pytest.raises(RuntimeError) as exc:
            await mod.html_to_pdf("<html></html>")
        msg = str(exc.value)
        assert str(len(small)) in msg or "108" in msg

    @pytest.mark.asyncio
    async def test_no_error_when_pdf_exactly_1kb(self):
        exact = b"%PDF-1.4" + b"x" * (1024 - 8)
        page  = _make_page_mock(pdf_bytes=exact)
        mod   = _load_fresh(_make_pw_mock(page))
        result = await mod.html_to_pdf("<html></html>")
        assert result == exact

    @pytest.mark.asyncio
    async def test_no_error_for_large_pdf(self):
        large = b"%PDF-1.4" + b"x" * 50_000
        page  = _make_page_mock(pdf_bytes=large)
        mod   = _load_fresh(_make_pw_mock(page))
        result = await mod.html_to_pdf("<html></html>")
        assert result == large

    @pytest.mark.asyncio
    async def test_error_message_mentions_render_failure(self):
        """Error must explain what likely went wrong, not just 'small file'."""
        page  = _make_page_mock(pdf_bytes=b"tiny")
        mod   = _load_fresh(_make_pw_mock(page))
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
        call_order = []
        page = _make_page_mock(call_order=call_order)
        mod  = _load_fresh(_make_pw_mock(page))
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
        call_order = []
        page = _make_page_mock(call_order=call_order)
        mod  = _load_fresh(_make_pw_mock(page))
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
        pdf_kwargs = []
        page = _make_page_mock()
        page.pdf = AsyncMock(
            side_effect=lambda **kw: pdf_kwargs.append(kw) or (b"%PDF" + b"x" * 2000)
        )
        mod = _load_fresh(_make_pw_mock(page))
        await mod.html_to_pdf("<html></html>")

        assert pdf_kwargs, "page.pdf() was never called"
        assert pdf_kwargs[0].get("print_background") is True, (
            f"pdf() must be called with print_background=True. Got: {pdf_kwargs[0]}"
        )

    @pytest.mark.asyncio
    async def test_wait_until_domcontentloaded(self):
        """set_content must use wait_until='domcontentloaded'."""
        sc_kwargs = []
        page = _make_page_mock()
        page.set_content = AsyncMock(
            side_effect=lambda *a, **kw: sc_kwargs.append(kw)
        )
        mod = _load_fresh(_make_pw_mock(page))
        await mod.html_to_pdf("<html></html>")

        assert sc_kwargs, "set_content was never called"
        assert sc_kwargs[0].get("wait_until") == "domcontentloaded", (
            f"Expected wait_until='domcontentloaded'. Got: {sc_kwargs[0]}"
        )

    @pytest.mark.asyncio
    async def test_format_is_letter(self):
        """Default format must be 'Letter', not 'A4'."""
        pdf_kwargs = []
        page = _make_page_mock()
        page.pdf = AsyncMock(
            side_effect=lambda **kw: pdf_kwargs.append(kw) or (b"%PDF" + b"x" * 2000)
        )
        mod = _load_fresh(_make_pw_mock(page))
        await mod.html_to_pdf("<html></html>")

        assert pdf_kwargs[0].get("format") == "Letter"


# ── 4. Error message content ──────────────────────────────────────────────────

class TestErrorMessageContent:
    """Every failure path must include actionable diagnostic context."""

    @pytest.mark.asyncio
    async def test_set_content_failure_mentions_set_content(self):
        page = _make_page_mock()
        page.set_content = AsyncMock(side_effect=Exception("boom"))
        mod = _load_fresh(_make_pw_mock(page))
        with pytest.raises(RuntimeError) as exc:
            await mod.html_to_pdf("<html></html>")
        assert "set_content" in str(exc.value)

    @pytest.mark.asyncio
    async def test_pdf_call_failure_mentions_pdf(self):
        page = _make_page_mock()
        page.pdf = AsyncMock(side_effect=Exception("pdf crash"))
        mod = _load_fresh(_make_pw_mock(page))
        with pytest.raises(RuntimeError) as exc:
            await mod.html_to_pdf("<html></html>")
        assert "pdf" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_emulate_media_failure_mentions_emulate_media(self):
        page = _make_page_mock()
        page.emulate_media = AsyncMock(side_effect=Exception("media fail"))
        mod = _load_fresh(_make_pw_mock(page))
        with pytest.raises(RuntimeError) as exc:
            await mod.html_to_pdf("<html></html>")
        assert "emulate_media" in str(exc.value)
