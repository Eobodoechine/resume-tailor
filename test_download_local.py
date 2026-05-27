"""
Local download test — reproduces the exact Chrome macOS bug and confirms the fix.

HOW THIS WORKS
--------------
Playwright with channel="chrome" drives the real Chrome binary already installed
on your Mac (/Applications/Google Chrome.app). This is the only browser that
reproduces the download="" → UUID filename bug. Linux Playwright (all modes)
normalises both approaches to Content-Disposition and can't catch it.

RUN ON YOUR MAC (required for accurate results):
  pip install flask playwright
  python -m playwright install          # only needed once
  python test_download_local.py

WHAT TO EXPECT
--------------
  BROKEN  download=''   → FAIL  (filename = 475beac5-...UUID...  )
  FIXED   no attr       → PASS  (filename = BRG_Managing_Consultant.pdf)

If both PASS, Chrome isn't installed or Playwright fell back to Chromium.

THE BUG EXPLAINED
-----------------
URL:  /api/tailor/475beac5-a32f-4343-ac60-1649f6f5001a/pdf
With download="":
  Chrome derives filename from URL path.
  Last segment "pdf" has no extension → not a filename.
  Chrome walks up to parent: 475beac5-a32f-4343-ac60-1649f6f5001a → UUID.
  Content-Disposition is ignored.
With no download attribute:
  a.click() is a plain navigation.
  Server returns Content-Disposition: attachment; filename="BRG_Managing_Consultant.pdf"
  Chrome downloads with that filename. No URL derivation needed.
"""

import threading, time, sys, platform
from flask import Flask, Response, request as flask_req
from playwright.sync_api import sync_playwright

app = Flask(__name__)
RECORD_ID = "475beac5-a32f-4343-ac60-1649f6f5001a"
DELAY_SECS = 8   # exceeds Chrome's ~5s activation window

# ── Test server ───────────────────────────────────────────────────────────────

@app.route("/api/tailor/<uid>/pdf", methods=["GET","HEAD"])
def pdf_endpoint(uid):
    """Mirrors the real backend: HEAD is instant, GET sleeps to simulate LibreOffice."""
    if flask_req.method == "HEAD":
        return Response(b"", status=200, headers={
            "Content-Disposition": 'attachment; filename="BRG_Managing_Consultant.pdf"',
            "Content-Type": "application/pdf",
        })
    time.sleep(DELAY_SECS)
    body = b"%PDF-1.4 fake-content-for-testing"
    return Response(body, status=200, headers={
        "Content-Disposition": 'attachment; filename="BRG_Managing_Consultant.pdf"',
        "Content-Type": "application/pdf",
        "Content-Length": str(len(body)),
    })

@app.route("/broken")
def broken():
    """v4 bug: download='' causes Chrome to derive filename from URL (UUID segment)."""
    return f"""<!DOCTYPE html><html><body>
<button id="btn" onclick="run()">Download PDF</button>
<script>
function run() {{
  var a = document.createElement("a");
  a.href = "/api/tailor/{RECORD_ID}/pdf";
  a.setAttribute("download", "");  // ← THE BUG: empty string → UUID filename
  document.body.appendChild(a);
  a.click();
  setTimeout(function() {{ a.remove(); }}, 100);
  // HEAD runs after (mirrors real app.js v4 logic)
  fetch("/api/tailor/{RECORD_ID}/pdf", {{method: "HEAD"}});
}}
</script></body></html>"""

@app.route("/fixed")
def fixed():
    """v5 fix: no download attribute → Content-Disposition drives filename."""
    return f"""<!DOCTYPE html><html><body>
<button id="btn" onclick="run()">Download PDF</button>
<script>
function run() {{
  var a = document.createElement("a");
  a.href = "/api/tailor/{RECORD_ID}/pdf";
  // NO download attribute → server's Content-Disposition: attachment takes over
  document.body.appendChild(a);
  a.click();
  setTimeout(function() {{ a.remove(); }}, 100);
  fetch("/api/tailor/{RECORD_ID}/pdf", {{method: "HEAD"}});
}}
</script></body></html>"""

def serve():
    import logging; logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(port=7788, debug=False, use_reloader=False)

# ── Test runner ───────────────────────────────────────────────────────────────

def run_case(page, route, label, expected, timeout_ms):
    page.goto(f"http://localhost:7788{route}")
    page.wait_for_selector("#btn")
    print(f"    clicking... (server delays {DELAY_SECS}s)", flush=True)
    with page.expect_download(timeout=timeout_ms) as dl_info:
        page.click("#btn")
    filename = dl_info.value.suggested_filename
    passed = (filename == expected)
    icon = "PASS ✅" if passed else "FAIL ❌"
    print(f"    filename : '{filename}'")
    print(f"    result   : {icon}")
    if not passed:
        is_uuid = len(filename) == 36 and filename.count("-") == 4
        if is_uuid:
            print(f"    note     : UUID = record ID from URL — Chrome ignored Content-Disposition")
    return passed

def main():
    on_mac = platform.system() == "Darwin"

    print(f"\n{'='*62}")
    print(f"  Platform : {platform.system()}")
    print(f"  Browser  : {'Real Chrome (channel=chrome)' if on_mac else 'Chromium (accurate test requires Mac)'}")
    print(f"  Delay    : {DELAY_SECS}s  (Chrome activation window ~5s)")
    print(f"  Expected : BROKEN=FAIL, FIXED=PASS")
    print(f"{'='*62}\n")

    if not on_mac:
        print("  ⚠️  Not on macOS. Linux Playwright normalises both approaches to")
        print("     Content-Disposition and won't reproduce the UUID bug.")
        print("     Run this script on your Mac for accurate results.\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(1.5)

    timeout_ms = (DELAY_SECS + 20) * 1000
    expected = "BRG_Managing_Consultant.pdf"

    with sync_playwright() as p:
        # channel="chrome" on Mac → real Chrome binary
        # channel="chrome" not available on Linux → falls back gracefully
        launch_kwargs = dict(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        if on_mac:
            launch_kwargs["channel"] = "chrome"

        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception as e:
            print(f"  Chrome launch failed: {e}")
            print("  Make sure Chrome is installed: https://google.com/chrome")
            sys.exit(1)

        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        print("  [BROKEN — download='']")
        broken_ok = run_case(page, "/broken", "BROKEN", expected, timeout_ms)

        print(f"\n  [FIXED  — no attribute]")
        fixed_ok  = run_case(page, "/fixed",  "FIXED",  expected, timeout_ms)

        browser.close()

    print(f"\n{'='*62}")
    if on_mac:
        if not broken_ok and fixed_ok:
            print("  Result: CONFIRMED ✅  bug reproduced + fix works")
        elif broken_ok and fixed_ok:
            print("  Result: Both pass — Chrome may have been updated; fix is safe")
        else:
            print("  Result: NEEDS INVESTIGATION ❌")
    else:
        print("  Result: Run on Mac for definitive test.")
    print(f"{'='*62}\n")

    sys.exit(0 if fixed_ok else 1)

if __name__ == "__main__":
    main()
