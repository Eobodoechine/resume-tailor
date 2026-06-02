"""
Route smoke tests — every frontend page route must return 200 and HTML.

These are the simplest possible tests and the ones most likely to catch
mis-wiring between a nav link and the actual registered route.  T18.1 in
the June 2 test run was a direct consequence of having no test in this file:
/admin returned 404 because the route was registered as /admin-panel.

No auth override needed — page routes serve static FileResponse with no
require_user dependency.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def page_client():
    """Plain TestClient — no auth, no dependency overrides."""
    from main import app
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# Every path the nav bar links to.  If you add a new page, add it here.
PAGE_ROUTES = [
    "/",
    "/dashboard",
    "/tailor",
    "/history",
    "/profile",
    "/improve",
    "/admin",
]


class TestPageRoutes:

    @pytest.mark.parametrize("path", PAGE_ROUTES)
    def test_returns_200(self, page_client, path):
        """Every page route must respond 200, not 404."""
        resp = page_client.get(path)
        assert resp.status_code == 200, (
            f"GET {path} returned {resp.status_code} — "
            "route may be missing from main.py or the HTML file may not exist"
        )

    @pytest.mark.parametrize("path", PAGE_ROUTES)
    def test_returns_html(self, page_client, path):
        """Every page route must return text/html (not JSON, not binary)."""
        resp = page_client.get(path)
        ct = resp.headers.get("content-type", "")
        assert "text/html" in ct, (
            f"GET {path} returned content-type={ct!r} — expected text/html"
        )

    @pytest.mark.parametrize("path", PAGE_ROUTES)
    def test_body_contains_doctype(self, page_client, path):
        """Every page must be a real HTML document, not an empty response."""
        resp = page_client.get(path)
        assert b"<!DOCTYPE" in resp.content or b"<!doctype" in resp.content, (
            f"GET {path} response body has no DOCTYPE — may be empty or broken"
        )

    def test_admin_panel_legacy_url_redirects(self, page_client):
        """/admin-panel (old URL) must 301 to /admin — browsers have it cached."""
        resp = page_client.get("/admin-panel", follow_redirects=False)
        assert resp.status_code == 301, (
            f"/admin-panel returned {resp.status_code}, expected 301 redirect"
        )
        assert resp.headers.get("location") == "/admin"

    def test_health_endpoint(self, page_client):
        """Health endpoint used by uptime-ping crons must stay up."""
        resp = page_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
