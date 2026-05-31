"""
Tests for file upload validation: magic bytes, size limit, extension checks.
"""
import pytest
from fastapi import HTTPException


# ── Magic byte validation (TD-07) ─────────────────────────────────────────────

class TestMagicBytes:

    def _check(self, data: bytes, ext: str) -> bool:
        from routes.resumes import _check_magic_bytes
        return _check_magic_bytes(data, ext)

    # PDF
    def test_valid_pdf_magic(self):
        assert self._check(b"%PDF-1.7\nsome content", "pdf") is True

    def test_invalid_pdf_magic_zip_header(self):
        # DOCX (ZIP) content declared as PDF
        assert self._check(b"PK\x03\x04" + b"\x00" * 100, "pdf") is False

    def test_invalid_pdf_magic_random_bytes(self):
        assert self._check(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "pdf") is False

    # DOCX
    def test_valid_docx_magic(self):
        # ZIP/OOXML magic: PK\x03\x04
        assert self._check(b"PK\x03\x04" + b"\x00" * 100, "docx") is True

    def test_invalid_docx_magic_pdf_header(self):
        assert self._check(b"%PDF-1.4\nfake content", "docx") is False

    # .doc legacy — unsupported (B4): python-docx can't read OLE2 .doc, so it's
    # rejected (the upload route blocks the .doc extension up front).
    def test_doc_extension_rejected(self):
        assert self._check(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", "doc") is False

    # Edge cases
    def test_empty_file_pdf_fails(self):
        assert self._check(b"", "pdf") is False

    def test_empty_file_docx_fails(self):
        assert self._check(b"", "docx") is False

    def test_pdf_content_truncated_to_3_bytes_fails(self):
        # Only 3 bytes — can't match %PDF (4 bytes)
        assert self._check(b"%PD", "pdf") is False


# ── PDF parser (TD-05 regression guard) ───────────────────────────────────────

class TestParseExperience:
    """Ensure _parse_experience no longer misclassifies short bullets as headers."""

    def _parse(self, text: str):
        # resume_parser is the live module; pdf_generator is archived
        from services.resume_parser import _parse_experience
        return _parse_experience(text)

    def test_pipe_separator_detected_as_header(self):
        text = "Senior Manager | Acme Corp | Jan 2020 – Dec 2022\n• Led a team of 10"
        entries = self._parse(text)
        assert len(entries) == 1
        # resume_parser returns ExperienceRole with title/company/dates keys (not "header")
        assert entries[0]["company"] == "Acme Corp"
        assert entries[0]["title"] == "Senior Manager"
        assert entries[0]["bullets"] == ["Led a team of 10"]

    def test_short_bullet_not_mistaken_for_header(self):
        """A short bullet like '• Built MVP' must stay as a bullet, not start a new entry."""
        text = (
            "Software Engineer | StartupCo | 2021 – 2023\n"
            "• Built MVP\n"
            "• Cut costs\n"
            "• Small win"
        )
        entries = self._parse(text)
        # Should be one entry, not multiple
        assert len(entries) == 1
        assert len(entries[0]["bullets"]) == 3

    def test_multiple_roles_parsed_correctly(self):
        text = (
            "VP of Engineering | BigCo | 2022 – Present\n"
            "• Managed 30 engineers\n"
            "• Drove 40% cost reduction\n"
            "\n"
            "Senior Engineer | SmallCo | 2019 – 2022\n"
            "• Built microservices architecture"
        )
        entries = self._parse(text)
        assert len(entries) == 2
        # resume_parser returns ExperienceRole with title/company/dates keys (not "header")
        assert entries[0]["company"] == "BigCo"
        assert entries[1]["company"] == "SmallCo"
        assert len(entries[0]["bullets"]) == 2
        assert len(entries[1]["bullets"]) == 1

    def test_empty_experience_returns_empty_list(self):
        assert self._parse("") == []

    def test_no_pipe_in_text_no_entries(self):
        """Text with no pipe separators produces no role entries."""
        entries = self._parse("Just a bunch of text without any pipe separators here")
        assert len(entries) == 0

    def test_bullet_containing_pipe_stays_a_bullet(self):
        """A bullet like '• Built X | reduced costs 30%' must not be promoted to a new role header.

        The old single-pipe heuristic would split one job into two and orphan the rest of the
        bullets. Now we require 2+ pipes AND no leading bullet marker.
        """
        text = (
            "Engineer | Acme | 2020 – 2023\n"
            "• Built ingest pipeline | reduced ETL costs 30%\n"
            "• Migrated to GCP | saved $400k/year\n"
            "• Mentored 4 juniors"
        )
        entries = self._parse(text)
        assert len(entries) == 1, "Bullets with pipes must not split the role"
        assert len(entries[0]["bullets"]) == 3
        assert "Built ingest pipeline" in entries[0]["bullets"][0]

    def test_single_pipe_paragraph_is_not_a_header(self):
        """A continuation line with only one pipe should not be treated as a role header."""
        text = (
            "VP Engineering | BigCo | 2022 – Present\n"
            "Notable wins: shipped product | grew team"
        )
        entries = self._parse(text)
        # The "Notable wins" line has 1 pipe — must remain a continuation, not a new role.
        assert len(entries) == 1


# ── DOCX extractor (table support) ────────────────────────────────────────────

class TestDocxExtraction:
    """Verify the extractor also reads tables — many real resumes use Word tables."""

    def test_docx_extractor_reads_tables(self, monkeypatch):
        """
        Patch the docx module to simulate a doc with both paragraphs AND tables;
        the extracted text must include both.

        Implementation note: conftest.py pre-stubs services.extractor as a
        MagicMock so route imports don't crash.  This test needs the *real*
        module, so we:
          1. Replace the docx stub with a controlled fake (monkeypatch restores).
          2. Evict the services.extractor MagicMock stub so Python re-imports it.
          3. Import fresh — the real code runs against the fake docx.
        monkeypatch restores both sys.modules entries when the test exits.
        """
        import sys
        import importlib
        from unittest.mock import MagicMock

        para = MagicMock()
        para.text = "Jane Doe — Senior Engineer"

        cell_a = MagicMock(); cell_a.text = "Python"
        cell_b = MagicMock(); cell_b.text = "10 years"
        row = MagicMock(); row.cells = [cell_a, cell_b]
        table = MagicMock(); table.rows = [row]

        # Empty header/footer
        section = MagicMock(); section.header.paragraphs = []; section.footer.paragraphs = []

        fake_doc = MagicMock()
        fake_doc.paragraphs = [para]
        fake_doc.tables   = [table]
        fake_doc.sections = [section]

        fake_docx = MagicMock()
        fake_docx.Document.return_value = fake_doc

        # 1. Swap in the controlled fake docx (monkeypatch restores on teardown)
        monkeypatch.setitem(sys.modules, "docx", fake_docx)

        # 2. Evict the conftest MagicMock stub so the real module is loaded next
        monkeypatch.delitem(sys.modules, "services.extractor", raising=False)

        # 3. Fresh import — real extractor code, fake docx dependency
        extractor_module = importlib.import_module("services.extractor")

        text = extractor_module._extract_docx(b"fake bytes")
        assert "Jane Doe" in text
        assert "Python" in text
        assert "10 years" in text
