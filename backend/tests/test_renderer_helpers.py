"""
Unit tests for fde_docx.py XML helper functions.

These tests construct minimal lxml XML elements directly so they run without
python-docx, the real DOCX template, or LibreOffice — pure logic only.
"""
import sys
from lxml import etree
import pytest

# ── Remove the MagicMock docx stub so we can import the real renderer module.
# conftest.py uses setdefault, so if docx was already imported as a real module
# it stays. But lxml is always the real package. We import the helpers directly.
for _mod in list(sys.modules):
    if _mod.startswith("renderers"):
        del sys.modules[_mod]

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _tag(local):
    return f"{{{W}}}{local}"


def _make_para(texts: list[str]) -> etree._Element:
    """Build a minimal <w:p> with one <w:r><w:t> per text entry."""
    p = etree.Element(_tag("p"))
    for text in texts:
        r = etree.SubElement(p, _tag("r"))
        t = etree.SubElement(r, _tag("t"))
        t.text = text
    return p


def _get_all_texts(para: etree._Element) -> list[str]:
    return [t.text for t in para.findall(f".//{_tag('t')}") if t.text]


# ── Import helpers under test ─────────────────────────────────────────────────
# We import from the module's private functions via importlib so the docx stub
# in conftest doesn't interfere (those helpers don't use docx at all).

import importlib, types

def _load_helpers():
    """Load only the pure XML helpers from fde_docx without triggering docx import."""
    import sys
    # Temporarily stub docx so the module-level Document(TEMPLATE_PATH) doesn't run
    real_docx = sys.modules.get("docx")
    from unittest.mock import MagicMock
    sys.modules["docx"] = MagicMock()

    # Also stub the renderers.base import
    if "renderers.base" not in sys.modules:
        base_stub = types.ModuleType("renderers.base")
        base_stub.ResumeData = dict
        base_stub.Renderer = object
        sys.modules["renderers.base"] = base_stub

    try:
        import renderers.fde_docx as m
        return m
    finally:
        if real_docx is not None:
            sys.modules["docx"] = real_docx
        elif "docx" in sys.modules:
            del sys.modules["docx"]


fde = _load_helpers()
_set_text       = fde._set_text
_set_dual_text  = fde._set_dual_text
_set_bullet_text = fde._set_bullet_text
_children_between = fde._children_between
_remove_between   = fde._remove_between
_insert_after     = fde._insert_after
_section_anchors  = fde._section_anchors


# ── _set_text ─────────────────────────────────────────────────────────────────

class TestSetText:
    def test_sets_text_on_single_run(self):
        p = _make_para(["old text"])
        _set_text(p, "new text")
        assert _get_all_texts(p) == ["new text"]

    def test_blanks_extra_runs(self):
        """Extra w:t elements beyond the first must be cleared, not left as-is."""
        p = _make_para(["run0", "run1", "run2"])
        _set_text(p, "only first")
        texts = _get_all_texts(p)
        assert texts[0] == "only first"
        # Extra t elements exist but have empty text — only first survives get_all_texts filter
        all_t = p.findall(f".//{_tag('t')}")
        assert all_t[1].text == ""
        assert all_t[2].text == ""

    def test_preserves_xml_space_attribute(self):
        p = _make_para(["old"])
        _set_text(p, "  spaced  ")
        t = p.find(f".//{_tag('t')}")
        assert t.get(XML_SPACE) == "preserve"

    def test_no_crash_on_empty_para(self):
        """Paragraph with no w:t should not raise."""
        p = etree.Element(_tag("p"))
        _set_text(p, "anything")   # should silently return

    def test_empty_string(self):
        p = _make_para(["something"])
        _set_text(p, "")
        t = p.find(f".//{_tag('t')}")
        assert t.text == ""


# ── _set_dual_text ────────────────────────────────────────────────────────────

class TestSetDualText:
    def _make_dual_para(self, bold="Title", light="Subtitle"):
        p = etree.Element(_tag("p"))
        r0 = etree.SubElement(p, _tag("r"))
        t0 = etree.SubElement(r0, _tag("t"))
        t0.text = bold
        r1 = etree.SubElement(p, _tag("r"))
        t1 = etree.SubElement(r1, _tag("t"))
        t1.text = light
        return p

    def test_sets_both_runs(self):
        p = self._make_dual_para()
        _set_dual_text(p, "New Title", "New Sub")
        runs = p.findall(_tag("r"))
        assert runs[0].find(_tag("t")).text == "New Title"
        assert runs[1].find(_tag("t")).text == " — New Sub"

    def test_removes_run1_when_no_light_text(self):
        p = self._make_dual_para()
        _set_dual_text(p, "Solo Title", None)
        runs = p.findall(_tag("r"))
        assert len(runs) == 1
        assert runs[0].find(_tag("t")).text == "Solo Title"

    def test_falsy_light_text_removes_run(self):
        p = self._make_dual_para()
        _set_dual_text(p, "Title", "")   # empty string = falsy
        assert len(p.findall(_tag("r"))) == 1

    def test_single_run_para_doesnt_crash(self):
        p = _make_para(["only"])
        _set_dual_text(p, "Bold", "Light")   # run1 doesn't exist — should not crash

    def test_no_runs_doesnt_crash(self):
        p = etree.Element(_tag("p"))
        _set_dual_text(p, "X", "Y")


# ── _set_bullet_text ──────────────────────────────────────────────────────────

class TestSetBulletText:
    def _make_bullet_para(self, symbol="• ", content="Old bullet"):
        p = etree.Element(_tag("p"))
        r0 = etree.SubElement(p, _tag("r"))
        t0 = etree.SubElement(r0, _tag("t"))
        t0.text = symbol
        r1 = etree.SubElement(p, _tag("r"))
        t1 = etree.SubElement(r1, _tag("t"))
        t1.text = content
        return p

    def test_sets_run1_content(self):
        p = self._make_bullet_para()
        _set_bullet_text(p, "New content")
        runs = p.findall(_tag("r"))
        assert runs[1].find(_tag("t")).text == "New content"

    def test_preserves_bullet_symbol_in_run0(self):
        p = self._make_bullet_para(symbol="• ")
        _set_bullet_text(p, "New content")
        runs = p.findall(_tag("r"))
        assert runs[0].find(_tag("t")).text == "• "

    def test_fallback_to_run0_when_single_run(self):
        p = _make_para(["old"])
        _set_bullet_text(p, "new")
        t = p.find(f".//{_tag('t')}")
        assert t.text == "new"

    def test_no_runs_doesnt_crash(self):
        p = etree.Element(_tag("p"))
        _set_bullet_text(p, "anything")


# ── _children_between ─────────────────────────────────────────────────────────

class TestChildrenBetween:
    def _make_tc(self, n=5):
        tc = etree.Element(_tag("tc"))
        children = [etree.SubElement(tc, _tag("p")) for _ in range(n)]
        return tc, children

    def test_returns_elements_between_start_and_end(self):
        tc, ch = self._make_tc(5)
        result = _children_between(tc, ch[1], ch[4])
        assert result == [ch[2], ch[3]]

    def test_no_end_returns_everything_after_start(self):
        tc, ch = self._make_tc(4)
        result = _children_between(tc, ch[1], None)
        assert result == [ch[2], ch[3]]

    def test_adjacent_start_end_returns_empty(self):
        tc, ch = self._make_tc(4)
        result = _children_between(tc, ch[1], ch[2])
        assert result == []

    def test_start_at_last_child_returns_empty(self):
        tc, ch = self._make_tc(3)
        result = _children_between(tc, ch[2], None)
        assert result == []


# ── _remove_between ───────────────────────────────────────────────────────────

class TestRemoveBetween:
    def test_removes_elements_between_anchors(self):
        tc = etree.Element(_tag("tc"))
        ch = [etree.SubElement(tc, _tag("p")) for _ in range(5)]
        _remove_between(tc, ch[1], ch[4])
        remaining = list(tc)
        assert ch[2] not in remaining
        assert ch[3] not in remaining
        assert ch[0] in remaining
        assert ch[1] in remaining
        assert ch[4] in remaining

    def test_removes_everything_after_start_when_no_end(self):
        tc = etree.Element(_tag("tc"))
        ch = [etree.SubElement(tc, _tag("p")) for _ in range(4)]
        _remove_between(tc, ch[1], None)
        remaining = list(tc)
        assert ch[0] in remaining
        assert ch[1] in remaining
        assert ch[2] not in remaining
        assert ch[3] not in remaining


# ── _insert_after ─────────────────────────────────────────────────────────────

class TestInsertAfter:
    def test_inserts_at_correct_position(self):
        tc = etree.Element(_tag("tc"))
        ch = [etree.SubElement(tc, _tag("p")) for _ in range(3)]
        new1 = etree.Element(_tag("p"))
        new2 = etree.Element(_tag("p"))
        _insert_after(tc, ch[1], [new1, new2])
        children = list(tc)
        assert children == [ch[0], ch[1], new1, new2, ch[2]]

    def test_appends_to_end_if_anchor_missing(self):
        """Should not raise ValueError — fallback to append."""
        tc = etree.Element(_tag("tc"))
        ch = etree.SubElement(tc, _tag("p"))
        orphan = etree.Element(_tag("p"))   # not in tc
        new = etree.Element(_tag("p"))
        _insert_after(tc, orphan, [new])   # should not raise
        assert new in list(tc)

    def test_no_elements_is_noop(self):
        tc = etree.Element(_tag("tc"))
        ch = etree.SubElement(tc, _tag("p"))
        _insert_after(tc, ch, [])
        assert list(tc) == [ch]


# ── _section_anchors ──────────────────────────────────────────────────────────

class TestSectionAnchors:
    def _make_tc_with_tbls(self, labels: list[str]):
        tc = etree.Element(_tag("tc"))
        tbls = []
        for label in labels:
            tbl = etree.SubElement(tc, _tag("tbl"))
            r = etree.SubElement(tbl, _tag("r"))
            t = etree.SubElement(r, _tag("t"))
            t.text = label
            tbls.append(tbl)
        return tc, tbls

    def test_finds_all_three_anchors(self):
        tc, tbls = self._make_tc_with_tbls([
            "PROFILE SUMMARY", "FEATURED PROJECT", "PROFESSIONAL EXPERIENCE"
        ])
        anchors = _section_anchors(tc)
        assert anchors["profile"]  is tbls[0]
        assert anchors["featured"] is tbls[1]
        assert anchors["experience"] is tbls[2]

    def test_missing_featured_returns_partial_dict(self):
        tc, tbls = self._make_tc_with_tbls(["PROFILE", "PROFESSIONAL EXPERIENCE"])
        anchors = _section_anchors(tc)
        assert "profile" in anchors
        assert "experience" in anchors
        assert "featured" not in anchors

    def test_non_tbl_children_ignored(self):
        tc = etree.Element(_tag("tc"))
        p = etree.SubElement(tc, _tag("p"))   # not a tbl
        t_el = etree.SubElement(p, _tag("t"))
        t_el.text = "PROFILE"
        anchors = _section_anchors(tc)
        assert anchors == {}   # p elements are skipped
