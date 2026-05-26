"""
Tests for JD fetch logic: URL validation (SSRF), HTML stripping,
JSON-LD extraction, and httpx error handling.
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch


# ── SSRF / URL validation ──────────────────────────────────────────────────────

class TestValidateFetchUrl:
    """_validate_fetch_url must block dangerous URLs before any network call."""

    def _validate(self, url: str):
        from routes.tailor import _validate_fetch_url
        return _validate_fetch_url(url)

    def test_rejects_non_http_scheme_ftp(self):
        with pytest.raises(HTTPException) as exc:
            self._validate("ftp://example.com/job")
        assert exc.value.status_code == 400

    def test_rejects_file_scheme(self):
        with pytest.raises(HTTPException) as exc:
            self._validate("file:///etc/passwd")
        assert exc.value.status_code == 400

    def test_rejects_localhost(self):
        with pytest.raises(HTTPException) as exc:
            self._validate("http://localhost:8000/admin")
        assert exc.value.status_code == 400

    def test_rejects_metadata_endpoint(self):
        with pytest.raises(HTTPException) as exc:
            self._validate("http://metadata.google.internal/computeMetadata/v1/")
        assert exc.value.status_code == 400

    def test_rejects_private_ip(self):
        with patch("routes.tailor.socket.gethostbyname", return_value="192.168.1.1"):
            with pytest.raises(HTTPException) as exc:
                self._validate("http://internal.company.com/jobs")
            assert exc.value.status_code == 400

    def test_rejects_loopback_ip(self):
        with patch("routes.tailor.socket.gethostbyname", return_value="127.0.0.1"):
            with pytest.raises(HTTPException) as exc:
                self._validate("http://something.internal/jobs")
            assert exc.value.status_code == 400

    def test_accepts_valid_public_url(self):
        with patch("routes.tailor.socket.gethostbyname", return_value="13.33.44.55"):
            # Should not raise
            self._validate("https://jobs.greenhouse.io/somecompany/12345")

    def test_accepts_https_lever(self):
        with patch("routes.tailor.socket.gethostbyname", return_value="104.26.10.78"):
            self._validate("https://jobs.lever.co/somecompany/abc-123")


# ── HTML stripping ──────────────────────────────────────────────────────────────

class TestStripHtml:

    def _strip(self, html: str) -> str:
        from routes.tailor import _strip_html
        return _strip_html(html)

    def test_strips_basic_tags(self):
        result = self._strip("<p>Hello <b>world</b></p>")
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_skips_script_content(self):
        result = self._strip("<script>var x = 'SHOULD_NOT_APPEAR';</script><p>Real content</p>")
        assert "SHOULD_NOT_APPEAR" not in result
        assert "Real content" in result

    def test_skips_style_content(self):
        result = self._strip("<style>.btn { color: red; } /* SHOULD_NOT_APPEAR */</style><p>Job Title</p>")
        assert "SHOULD_NOT_APPEAR" not in result
        assert "Job Title" in result

    def test_skips_nav_content(self):
        result = self._strip("<nav>Home | About | Jobs</nav><main>Senior Engineer</main>")
        # nav content filtered, main content present
        assert "Senior Engineer" in result


# ── JSON-LD extraction ──────────────────────────────────────────────────────────

class TestExtractJsonLdJob:

    def _extract(self, html: str) -> str:
        from routes.tailor import _extract_jsonld_job
        return _extract_jsonld_job(html)

    def test_extracts_greenhouse_style_jsonld(self):
        html = """
        <html><body>
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Senior Software Engineer",
          "hiringOrganization": {"name": "Acme Corp"},
          "description": "We are looking for a Senior Software Engineer to join our team."
        }
        </script>
        </body></html>
        """
        result = self._extract(html)
        assert "Senior Software Engineer" in result
        assert "Acme Corp" in result
        assert "We are looking for" in result

    def test_returns_empty_for_non_jobposting_type(self):
        html = """
        <script type="application/ld+json">
        {"@type": "WebPage", "name": "About Us"}
        </script>
        """
        result = self._extract(html)
        assert result == ""

    def test_returns_empty_for_invalid_json(self):
        html = """
        <script type="application/ld+json">
        {this is not: valid json}
        </script>
        """
        result = self._extract(html)
        assert result == ""

    def test_handles_jsonld_array_with_jobposting(self):
        html = """
        <script type="application/ld+json">
        [
          {"@type": "BreadcrumbList"},
          {"@type": "JobPosting", "title": "Data Engineer", "description": "Great role"}
        ]
        </script>
        """
        result = self._extract(html)
        assert "Data Engineer" in result

    def test_strips_html_inside_description(self):
        html = """
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "PM",
          "description": "<ul><li>Lead product</li><li>Work with eng</li></ul>"
        }
        </script>
        """
        result = self._extract(html)
        assert "<ul>" not in result
        assert "Lead product" in result
