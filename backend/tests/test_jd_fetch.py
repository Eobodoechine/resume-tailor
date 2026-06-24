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


# ── DNS-rebinding / IP pinning transport ────────────────────────────────────────

import httpx  # noqa: E402  (after the section comment so it reads close to its use)


def _addrinfo(*ips):
    """socket.getaddrinfo()-style return value for the given IPs."""
    return [(2, 1, 6, "", (ip, 0)) for ip in ips]


class TestPinnedSSRFTransport:
    """_PinnedSSRFTransport must resolve once, reject internal targets at connect
    time, and pin the connection to the validated IP while keeping TLS SNI/cert
    verification bound to the hostname. These unit tests mock the parent
    transport so no real socket is opened."""

    def _transport(self):
        from routes.tailor import _PinnedSSRFTransport
        return _PinnedSSRFTransport()

    async def test_pins_to_resolved_ip_and_preserves_host_and_sni(self):
        """The crux of the fix: at connect time the URL host is the validated IP,
        but the Host header and the TLS sni_hostname extension still carry the
        ORIGINAL hostname — so cert verification runs against the hostname, not
        the bare IP. After the call the URL is restored to the hostname."""
        captured = {}

        async def fake_super(self, request):
            captured["connect_host"] = request.url.host
            captured["sni"] = request.extensions.get("sni_hostname")
            captured["host_header"] = request.headers.get("host")
            return MagicMock()

        t = self._transport()
        req = httpx.Request("GET", "https://jobs.example.com/listing")
        with patch("routes.tailor.socket.getaddrinfo", return_value=_addrinfo("13.33.44.55")), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", fake_super):
            await t.handle_async_request(req)

        assert captured["connect_host"] == "13.33.44.55"          # connect to the pinned IP
        assert captured["sni"] == "jobs.example.com"               # TLS verified against hostname
        assert captured["host_header"] == "jobs.example.com"       # Host header preserved
        assert req.url.host == "jobs.example.com"                  # URL restored after connect

    async def test_blocks_when_resolution_is_private(self):
        """Rebind to a single internal answer: must raise and never reach super()."""
        from routes.tailor import _PinnedSSRFTransport
        super_mock = AsyncMock()
        t = _PinnedSSRFTransport()
        req = httpx.Request("GET", "https://rebind.example.com/x")
        with patch("routes.tailor.socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", super_mock):
            with pytest.raises(httpx.ConnectError):
                await t.handle_async_request(req)
        super_mock.assert_not_awaited()

    async def test_blocks_when_any_resolved_ip_is_private(self):
        """Split-horizon answer (public + private) must be rejected wholesale."""
        from routes.tailor import _PinnedSSRFTransport
        super_mock = AsyncMock()
        t = _PinnedSSRFTransport()
        req = httpx.Request("GET", "https://mixed.example.com/x")
        with patch("routes.tailor.socket.getaddrinfo",
                   return_value=_addrinfo("13.33.44.55", "10.0.0.5")), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", super_mock):
            with pytest.raises(httpx.ConnectError):
                await t.handle_async_request(req)
        super_mock.assert_not_awaited()

    async def test_blocks_internal_ip_literal_without_dns(self):
        """A raw internal-IP URL is blocked directly (no resolution needed)."""
        from routes.tailor import _PinnedSSRFTransport
        super_mock = AsyncMock()
        t = _PinnedSSRFTransport()
        for ip in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "[::1]"):
            req = httpx.Request("GET", f"http://{ip}:80/admin")
            with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", super_mock):
                with pytest.raises(httpx.ConnectError):
                    await t.handle_async_request(req)
        super_mock.assert_not_awaited()

    async def test_allows_public_ip_literal(self):
        """A public raw-IP URL is allowed through unchanged (SNI left to default)."""
        from routes.tailor import _PinnedSSRFTransport
        captured = {}

        async def fake_super(self, request):
            captured["sni"] = request.extensions.get("sni_hostname")
            captured["host"] = request.url.host
            return MagicMock()

        t = _PinnedSSRFTransport()
        req = httpx.Request("GET", "https://13.33.44.55/listing")
        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", fake_super):
            await t.handle_async_request(req)
        assert captured["host"] == "13.33.44.55"
        assert captured["sni"] is None  # raw-IP URL: don't fake a hostname SNI


class TestFetchJdRebindEndpoint:
    """End-to-end: a low-TTL rebind where the pre-flight validator sees a PUBLIC
    address but the connection-time resolution returns a PRIVATE one must be
    blocked. Proves the fix isn't defeated by the validator/connector using two
    separate resolutions."""

    @staticmethod
    def _app_client():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from dependencies.auth import require_user, AuthContext
        from routes import tailor

        user = MagicMock()
        user.id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        user.email = "t@example.com"
        app = FastAPI()
        app.include_router(tailor.router)
        app.dependency_overrides[require_user] = lambda: AuthContext(user=user, token="tok")
        return TestClient(app, raise_server_exceptions=False)

    def test_rebind_public_then_private_is_blocked(self):
        calls = {"n": 0}

        def rebinding_getaddrinfo(host, *a, **k):
            # 1st lookup (pre-flight _validate_fetch_url) → public; afterwards
            # (the pinned transport's connect-time resolution) → internal.
            calls["n"] += 1
            if calls["n"] == 1:
                return _addrinfo("13.33.44.55")
            return _addrinfo("169.254.169.254")

        client = self._app_client()
        with patch("routes.tailor.socket.getaddrinfo", side_effect=rebinding_getaddrinfo):
            resp = client.post("/api/tailor/fetch-jd",
                               json={"url": "https://rebind.attacker.example/job"})
        # Pre-flight passed (public) but the connection was refused (private) →
        # the route maps the ConnectError to a 4xx, NOT a fetched 200.
        assert resp.status_code == 400
        assert calls["n"] >= 2  # proves a second, connect-time resolution happened


class TestTlsHostnameVerificationUnderPinning:
    """SECURITY-CRITICAL: pinning to an IP must NOT disable TLS hostname
    verification. Uses a real loopback HTTPS server with trustme-issued certs.
    _is_blocked_ip is patched so 127.0.0.1 is permitted purely for the test
    server; getaddrinfo is patched so the hostname resolves to loopback."""

    @staticmethod
    def _https_server(server_ssl_ctx):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from contextlib import contextmanager

        class _OK(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><body>job description body content here</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        @contextmanager
        def _run():
            srv = HTTPServer(("127.0.0.1", 0), _OK)
            srv.socket = server_ssl_ctx.wrap_socket(srv.socket, server_side=True)
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                yield srv.server_address[1]
            finally:
                srv.shutdown()
                srv.server_close()

        return _run()

    @staticmethod
    def _server_ctx(cert):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert.configure_cert(ctx)
        return ctx

    async def _fetch(self, url, ca):
        """Run a request through the real pinned transport, trusting only `ca`,
        with the hostname resolved to loopback and loopback temporarily allowed."""
        import tempfile, os
        from routes.tailor import _PinnedSSRFTransport
        fd, ca_path = tempfile.mkstemp(suffix=".pem")
        os.close(fd)
        ca.cert_pem.write_to_path(ca_path)
        try:
            transport = _PinnedSSRFTransport(verify=ca_path)
            async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
                with patch("routes.tailor._is_blocked_ip", return_value=False), \
                     patch("routes.tailor.socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
                    return await client.get(url)
        finally:
            os.unlink(ca_path)

    async def test_valid_cert_for_hostname_succeeds_when_pinned_to_ip(self):
        """Cert SAN matches the hostname → handshake verifies against the
        hostname (not 127.0.0.1) → request succeeds. This is case (a)."""
        trustme = pytest.importorskip("trustme")
        ca = trustme.CA()
        cert = ca.issue_cert("pinned.test")
        with self._https_server(self._server_ctx(cert)) as port:
            resp = await self._fetch(f"https://pinned.test:{port}/job", ca)
        assert resp.status_code == 200
        assert "job description body content" in resp.text

    async def test_cert_for_wrong_hostname_is_rejected(self):
        """Server presents a cert for a DIFFERENT hostname. If verification were
        silently disabled by pinning, this would wrongly succeed. It must raise."""
        trustme = pytest.importorskip("trustme")
        ca = trustme.CA()
        wrong_cert = ca.issue_cert("not-the-host.test")  # valid CA, wrong name
        with self._https_server(self._server_ctx(wrong_cert)) as port:
            with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                await self._fetch(f"https://pinned.test:{port}/job", ca)

    async def test_untrusted_ca_is_rejected(self):
        """Cert name matches the hostname but is signed by a CA we don't trust →
        must still fail (verify=True is genuinely on, not bypassed)."""
        trustme = pytest.importorskip("trustme")
        server_ca = trustme.CA()
        cert = server_ca.issue_cert("pinned.test")
        client_trust_ca = trustme.CA()  # a DIFFERENT CA the client trusts
        with self._https_server(self._server_ctx(cert)) as port:
            with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                await self._fetch(f"https://pinned.test:{port}/job", client_trust_ca)
