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

    @staticmethod
    def _addrinfo(*ips):
        """Build a socket.getaddrinfo()-style return value for the given IPs.
        Each entry: (family, type, proto, canonname, sockaddr) with sockaddr=(ip, port)."""
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]

    def test_rejects_private_ip(self):
        with patch("routes.tailor.socket.getaddrinfo", return_value=self._addrinfo("192.168.1.1")):
            with pytest.raises(HTTPException) as exc:
                self._validate("http://internal.company.com/jobs")
            assert exc.value.status_code == 400

    def test_rejects_loopback_ip(self):
        with patch("routes.tailor.socket.getaddrinfo", return_value=self._addrinfo("127.0.0.1")):
            with pytest.raises(HTTPException) as exc:
                self._validate("http://something.internal/jobs")
            assert exc.value.status_code == 400

    def test_rejects_when_any_resolved_ip_is_private(self):
        """A host that resolves to a public AND a private address must be blocked
        — the getaddrinfo 'validate every address' hardening (was bypassable when
        only the first gethostbyname() IPv4 was checked)."""
        with patch("routes.tailor.socket.getaddrinfo",
                   return_value=self._addrinfo("13.33.44.55", "10.0.0.5")):
            with pytest.raises(HTTPException) as exc:
                self._validate("http://rebind.example.com/jobs")
            assert exc.value.status_code == 400

    def test_rejects_private_ipv6(self):
        """Unique-local IPv6 (fc00::/7) must also be blocked."""
        with patch("routes.tailor.socket.getaddrinfo",
                   return_value=[(10, 1, 6, "", ("fd00::1", 0, 0, 0))]):
            with pytest.raises(HTTPException) as exc:
                self._validate("http://v6.internal/jobs")
            assert exc.value.status_code == 400

    def test_accepts_valid_public_url(self):
        with patch("routes.tailor.socket.getaddrinfo", return_value=self._addrinfo("13.33.44.55")):
            # Should not raise
            self._validate("https://jobs.greenhouse.io/somecompany/12345")

    def test_accepts_https_lever(self):
        with patch("routes.tailor.socket.getaddrinfo", return_value=self._addrinfo("104.26.10.78")):
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

    def test_handles_graph_wrapped_jobposting(self):
        """WordPress/Yoast/Drupal pages wrap JobPosting in {"@graph": [...]}."""
        html = """
        <script type="application/ld+json">
        {"@context": "https://schema.org",
         "@graph": [
            {"@type": "WebPage", "name": "Careers"},
            {"@type": "JobPosting", "title": "Backend Engineer",
             "description": "Build APIs and own services end to end."}
         ]}
        </script>
        """
        result = self._extract(html)
        assert "Backend Engineer" in result
        assert "Build APIs" in result

    def test_array_fields_do_not_leak_python_repr(self):
        """responsibilities/qualifications as JSON arrays must not become "['x']"."""
        html = """
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "QA",
         "description": "Test things.",
         "responsibilities": ["Write tests", "File bugs"],
         "qualifications": ["3y experience"]}
        </script>
        """
        result = self._extract(html)
        assert "[" not in result and "'" not in result
        assert "Write tests" in result
        assert "File bugs" in result


# ── Open Graph / meta fallback ──────────────────────────────────────────────────

class TestExtractOgMeta:

    def _og(self, html: str) -> str:
        from routes.tailor import _extract_og_meta
        return _extract_og_meta(html)

    def test_recovers_jd_from_spa_shell_og_tags(self):
        """The reported bug: a React shell with empty body still carries the JD
        in og:* meta. content may appear before OR after the property attr."""
        html = (
            "<html><head>"
            "<meta property='og:title' content='Data Analyst - Atlanta, GA | Workday'>"
            "<meta content='This is a Hybrid position. Job Summary: data-driven strategist...' "
            "property='og:description'>"
            "</head><body><div id='root'></div></body></html>"
        )
        result = self._og(html)
        assert "Data Analyst" in result
        assert "Hybrid position" in result

    def test_unescapes_entities(self):
        html = "<meta name='description' content='Build &amp; ship R&amp;D tools'>"
        assert "Build & ship R&D tools" in self._og(html)

    def test_empty_when_no_meta(self):
        assert self._og("<html><body><div id='root'></div></body></html>") == ""


# ── Encoding ────────────────────────────────────────────────────────────────────

class TestDecodeHtml:

    def _decode(self, content: bytes, charset):
        from routes.tailor import _decode_html
        return _decode_html(content, charset)

    def test_meta_only_cp1252_no_mojibake(self):
        """charset only in <meta>, none in HTTP header (header_charset=None)."""
        raw = "<meta charset='windows-1252'><p>Café — naïve résumé</p>".encode("cp1252")
        out = self._decode(raw, None)
        assert "Café" in out and "résumé" in out
        assert "�" not in out  # no replacement chars

    def test_header_charset_wins(self):
        raw = "<p>Café</p>".encode("cp1252")
        assert "Café" in self._decode(raw, "cp1252")


# ── ATS JSON endpoints ──────────────────────────────────────────────────────────

class TestExtractAtsJson:

    def _j(self, raw: bytes) -> str:
        from routes.tailor import _extract_ats_json
        return _extract_ats_json(raw)

    def test_parses_lever_style_json(self):
        raw = (b'[{"text":"Senior CSM","descriptionPlain":"Own the book of business.",'
               b'"lists":[{"text":"What you bring","content":"<li>5y experience</li>"}]}]')
        out = self._j(raw)
        assert "Senior CSM" in out
        assert "Own the book of business" in out
        assert "5y experience" in out
        assert "<li>" not in out

    def test_parses_greenhouse_style_json(self):
        raw = b'{"title":"Data Engineer","content":"<p>Build pipelines</p>"}'
        out = self._j(raw)
        assert "Data Engineer" in out and "Build pipelines" in out

    def test_invalid_json_returns_empty(self):
        assert self._j(b"not json") == ""


# ── Truncation ──────────────────────────────────────────────────────────────────

class TestTruncateJd:

    def test_no_truncation_under_limit(self):
        from routes.tailor import _truncate_jd
        txt, cut = _truncate_jd("short jd")
        assert cut is False and txt == "short jd"

    def test_truncates_on_word_boundary(self):
        from routes.tailor import _truncate_jd, MAX_JD_LENGTH
        txt, cut = _truncate_jd("word " * (MAX_JD_LENGTH // 3))
        assert cut is True
        assert len(txt) <= MAX_JD_LENGTH
        assert not txt.endswith("wor")  # not cut mid-word at the seam


# ── DNS fail-closed (SSRF) ──────────────────────────────────────────────────────

class TestDnsFailClosed:

    def test_dns_failure_is_rejected_not_deferred(self):
        """A name Python can't resolve must FAIL CLOSED, not be treated as allowed
        (httpx would otherwise resolve it independently with no SSRF guard)."""
        from routes.tailor import _validate_fetch_url
        with patch("routes.tailor.socket.getaddrinfo", side_effect=OSError("SERVFAIL")):
            with pytest.raises(HTTPException) as exc:
                _validate_fetch_url("https://weird-name.example/jobs")
            assert exc.value.status_code == 400


# ── TalentNet SPA extractor ─────────────────────────────────────────────────────

class _FakeStream:
    """Async-context-manager stand-in for httpx's client.stream(...)."""
    def __init__(self, raw: bytes, status: int = 200):
        self._raw = raw
        self.status_code = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        yield self._raw


class TestExtractTalentnet:

    def _client_returning(self, payload, status=200):
        """A MagicMock httpx client whose .stream() yields a fixed JSON payload."""
        import json as _json
        raw = _json.dumps(payload).encode("utf-8")
        client = MagicMock()
        client.stream = MagicMock(return_value=_FakeStream(raw, status))
        return client

    async def test_non_talentnet_url_returns_empty_and_no_call(self):
        from routes.tailor import _extract_talentnet
        client = self._client_returning({})
        out = await _extract_talentnet("https://boards.greenhouse.io/x/jobs/123", client)
        assert out == ""
        client.stream.assert_not_called()

    async def test_extracts_jd_and_uses_hardcoded_host_plus_both_headers(self):
        from routes.tailor import _extract_talentnet, _TALENTNET_API_HOST
        payload = {"title": {"name": "Data Analyst"},
                   "location": "Atlanta, GA",
                   "description": "<p>Hybrid role. Build dashboards.</p>",
                   "skills": [{"name": "SQL"}, {"name": "Power BI"}]}
        client = self._client_returning(payload)
        url = "https://workday.talentnet.community/jobs/7d75925a-410b-4947-b3df-0ad4f14269c4"
        with patch("routes.tailor.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("13.33.44.55", 0))]):
            out = await _extract_talentnet(url, client)
        assert "Data Analyst" in out
        assert "Build dashboards" in out
        assert "SQL" in out and "Power BI" in out
        assert "<p>" not in out
        # SSRF guards: host is the hardcoded constant; both headers present; tenant = subdomain
        method, called_url = client.stream.call_args.args[0], client.stream.call_args.args[1]
        assert method == "GET"
        assert called_url == f"https://{_TALENTNET_API_HOST}/api/community/job/7d75925a-410b-4947-b3df-0ad4f14269c4"
        headers = client.stream.call_args.kwargs["headers"]
        assert headers["x-tenant"] == "workday"
        assert headers["x-spa-type"] == "community"

    async def test_malformed_uuid_returns_empty(self):
        from routes.tailor import _extract_talentnet
        client = self._client_returning({})
        out = await _extract_talentnet(
            "https://workday.talentnet.community/jobs/not-a-uuid-here", client)
        assert out == ""
        client.stream.assert_not_called()

    async def test_api_non_200_falls_through(self):
        from routes.tailor import _extract_talentnet
        client = self._client_returning({}, status=404)
        url = "https://workday.talentnet.community/jobs/7d75925a-410b-4947-b3df-0ad4f14269c4"
        with patch("routes.tailor.socket.getaddrinfo",
                   return_value=[(2, 1, 6, "", ("13.33.44.55", 0))]):
            out = await _extract_talentnet(url, client)
        assert out == ""
