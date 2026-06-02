"""
Playwright PDF Printer
======================
Converts an HTML string → PDF bytes using Playwright headless Chromium.

Critical ordering requirement (Playwright quirk):
  page.emulate_media(media="print") MUST be called BEFORE page.set_content().
  If called after, @media print CSS is evaluated post-layout and colour rules
  are ignored — the teal sidebar and section bars disappear from the PDF.

Error philosophy:
  Every failure path logs the full traceback plus the specific context that
  helps diagnose it (first 500 chars of HTML on set_content failure, all
  pdf() params on pdf() failure, byte count on suspiciously small output).
  No silent except-pass. Any exception is re-raised as RuntimeError so the
  caller (download_pdf route) can surface a clean 500 with logging.

Usage:
    pdf_bytes = await html_to_pdf(html_string)
"""
from __future__ import annotations

import logging
import traceback

logger = logging.getLogger(__name__)

# PDFs smaller than this are treated as failures —
# Playwright occasionally returns a near-empty file if page rendering fails.
_MIN_PDF_BYTES = 1_024   # 1 KB


async def html_to_pdf(
    html: str,
    *,
    format: str = "Letter",
    print_background: bool = True,
    prefer_css_page_size: bool = False,
    margin_top: str = "0",
    margin_bottom: str = "0",
    margin_left: str = "0",
    margin_right: str = "0",
) -> bytes:
    """
    Convert an HTML string to PDF bytes using Playwright headless Chromium.

    Args:
        html:                 Complete self-contained HTML string.
        format:               Page size — 'Letter' or 'A4'.
        print_background:     MUST be True — otherwise teal backgrounds vanish.
        prefer_css_page_size: False — use format= for physical page size.
        margin_*:             Page margins; default 0 since the HTML controls
                              its own padding.

    Returns:
        PDF bytes.

    Raises:
        RuntimeError on any failure (Playwright import error, browser crash,
        set_content failure, pdf() failure, or suspiciously small output).
    """
    pdf_params = dict(
        format=format,
        print_background=print_background,
        prefer_css_page_size=prefer_css_page_size,
        margin={
            "top":    margin_top,
            "bottom": margin_bottom,
            "left":   margin_left,
            "right":  margin_right,
        },
    )

    logger.info(
        "[playwright_pdf] START  html_len=%d  format=%s  "
        "print_background=%s  prefer_css_page_size=%s",
        len(html), format, print_background, prefer_css_page_size,
    )

    # ── Import check ──────────────────────────────────────────────────────────
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        logger.error(
            "[playwright_pdf] playwright package not installed: %s\n%s",
            exc, traceback.format_exc(),
        )
        raise RuntimeError(
            "playwright is not installed. "
            "Add it to requirements.txt and run 'playwright install chromium'."
        ) from exc

    # ── Launch → render → close ───────────────────────────────────────────────
    try:
        async with async_playwright() as pw:
            # --no-sandbox is required on Linux containers (Render, Docker).
            # --disable-dev-shm-usage prevents crashes on low /dev/shm.
            browser = await pw.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                page = await browser.new_page()

                # ── Step 1: emulate_media BEFORE set_content ──────────────
                # This is not optional. Playwright applies media emulation at
                # page creation — reversing the order causes @media print
                # rules to be ignored, which removes all coloured backgrounds.
                try:
                    await page.emulate_media(media="print")
                    logger.debug("[playwright_pdf] emulate_media(print) OK")
                except Exception as exc:
                    logger.error(
                        "[playwright_pdf] emulate_media FAILED  error=%s\n%s",
                        exc, traceback.format_exc(),
                    )
                    raise RuntimeError(
                        f"page.emulate_media(media='print') failed: {exc}"
                    ) from exc

                # ── Step 2: set_content ───────────────────────────────────
                try:
                    await page.set_content(html, wait_until="domcontentloaded")
                    logger.debug("[playwright_pdf] set_content OK")
                except Exception as exc:
                    logger.error(
                        "[playwright_pdf] set_content FAILED  error=%s  "
                        "html_preview(first_500)=%r\n%s",
                        exc, html[:500], traceback.format_exc(),
                    )
                    raise RuntimeError(
                        f"page.set_content failed: {exc}"
                    ) from exc

                # ── Step 3: pdf() ─────────────────────────────────────────
                try:
                    pdf_bytes = await page.pdf(**pdf_params)
                    logger.info(
                        "[playwright_pdf] page.pdf() returned %d bytes",
                        len(pdf_bytes),
                    )
                except Exception as exc:
                    logger.error(
                        "[playwright_pdf] page.pdf() FAILED  error=%s  "
                        "pdf_params=%r\n%s",
                        exc, pdf_params, traceback.format_exc(),
                    )
                    raise RuntimeError(
                        f"page.pdf({pdf_params!r}) failed: {exc}"
                    ) from exc

                # ── Step 4: sanity-check output size ──────────────────────
                if not pdf_bytes or len(pdf_bytes) < _MIN_PDF_BYTES:
                    size = len(pdf_bytes) if pdf_bytes else 0
                    logger.error(
                        "[playwright_pdf] PDF too small — likely empty/broken  "
                        "size=%d bytes (threshold=%d)  html_len=%d  pdf_params=%r",
                        size, _MIN_PDF_BYTES, len(html), pdf_params,
                    )
                    raise RuntimeError(
                        f"Playwright returned a {size}-byte PDF (min {_MIN_PDF_BYTES} bytes). "
                        "This usually means the page failed to render — check the HTML for errors."
                    )

                logger.info("[playwright_pdf] COMPLETE  size=%d bytes", len(pdf_bytes))
                return pdf_bytes

            finally:
                await browser.close()

    except RuntimeError:
        raise   # already logged above — let it propagate cleanly
    except Exception as exc:
        # Log the full error on one line (Render log search truncates at newlines).
        # Playwright BrowserType.launch errors are multiline — squash to single line
        # so the "Executable doesn't exist" or resource-limit reason is visible.
        full_msg = str(exc).replace("\n", " | ")
        logger.error(
            "[playwright_pdf] unexpected error  error=%s  traceback=%s",
            full_msg, traceback.format_exc().replace("\n", " | "),
        )
        raise RuntimeError(
            f"Playwright PDF generation failed unexpectedly: {full_msg}"
        ) from exc
