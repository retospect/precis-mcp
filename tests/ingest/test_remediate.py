"""Unit tests for the metadata-remediation pure logic.

The DB-mutating paths (``remediate_one`` / ``_apply_fix``) are exercised
end-to-end by the dry-run against prod; here we pin the decision logic and
the derived-text helpers that decide what gets written.
"""

from __future__ import annotations

from pathlib import Path

from precis.ingest.remediate import (
    Outcome,
    _combined_card_text,
    _corpus_pdf_dest,
    _title_is_junk,
    classify,
)


class TestTitleIsJunk:
    def test_blank_and_defaults_are_junk(self):
        assert _title_is_junk("") is True
        assert _title_is_junk("   ") is True
        assert _title_is_junk(None) is True
        assert _title_is_junk("No Job Name") is True
        assert _title_is_junk("Microsoft Word - draft.doc") is True

    def test_real_title_is_not_junk(self):
        assert _title_is_junk("The rise of graphene") is False


class TestClassify:
    def test_usable_title_fixes(self):
        assert classify("Ballistic carbon nanotube transistors") == "fix"

    def test_junk_title_triages(self):
        assert classify("No Job Name") == "triage"
        assert classify("") == "triage"


class TestCombinedCardText:
    def test_all_fields(self):
        text = _combined_card_text(
            "A Title", ["Smith, J.", "Doe, A."], "An abstract.", ["kw1", "kw2"]
        )
        assert text == "A Title\n\nSmith, J.; Doe, A.\n\nAn abstract.\n\nkw1; kw2"

    def test_title_only(self):
        assert _combined_card_text("A Title", [], "", []) == "A Title"

    def test_empty_is_placeholder(self):
        assert _combined_card_text("", [], "", []) == "[no metadata]"


class TestCorpusPdfDest:
    def test_letter_shard(self):
        root = Path("/corpus")
        assert _corpus_pdf_dest("smith23x", root) == root / "s" / "smith23x.pdf"

    def test_non_alnum_first_char_underscores(self):
        root = Path("/corpus")
        assert _corpus_pdf_dest("_weird", root) == root / "_" / "_weird.pdf"

    def test_suffix_override(self):
        root = Path("/corpus")
        assert _corpus_pdf_dest("a23b", root, suffix=".PDF") == root / "a" / "a23b.PDF"


class TestOutcomeLine:
    def test_fixed_with_rename(self):
        o = Outcome(
            ref_id=10,
            action="fixed",
            old_cite_key="anon10foo",
            new_cite_key="stenning10",
            new_title="A real title",
        )
        line = o.line()
        assert "FIXED" in line and "anon10foo -> stenning10" in line

    def test_triaged(self):
        o = Outcome(
            ref_id=11, action="triaged", old_cite_key="anon24x", detail="S2 miss"
        )
        assert o.line().startswith("TRIAGE  #11")
