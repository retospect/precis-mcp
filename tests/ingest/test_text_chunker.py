"""Tests for precis.ingest.text_chunker (vendored from acatome_extract.chunker)."""

import pytest

from precis.ingest.text_chunker import (
    DEFAULT_HARD_MAX_CHARS,
    enforce_hard_max,
    split_table,
    split_text,
)


class TestSplitText:
    def test_short_text_unchanged(self):
        result = split_text("Hello world.", chunk_size=100)
        assert result == ["Hello world."]

    def test_empty_text(self):
        assert split_text("") == []
        assert split_text("   ") == []

    def test_splits_on_paragraphs(self):
        text = "Para one.\n\nPara two.\n\nPara three."
        result = split_text(text, chunk_size=20, chunk_overlap=0)
        assert len(result) >= 2
        assert "Para one." in result[0]

    def test_respects_chunk_size(self):
        text = " ".join(["word"] * 500)  # ~2500 chars
        result = split_text(text, chunk_size=200, chunk_overlap=0)
        for chunk in result:
            assert len(chunk) <= 200 + 10  # small tolerance for separator

    def test_overlap_present(self):
        # Build text with many small paragraphs that force multiple chunks
        paras = [f"Paragraph {i} content here." for i in range(20)]
        text = "\n\n".join(paras)
        result = split_text(text, chunk_size=200, chunk_overlap=50)
        assert len(result) >= 2
        # Some overlap text from end of chunk N should appear in chunk N+1
        for i in range(len(result) - 1):
            tail = result[i][-50:]
            # At least part of the tail should appear in the next chunk
            # (overlap means shared content)
            assert any(
                word in result[i + 1] for word in tail.split() if len(word) > 3
            ), f"No overlap between chunk {i} and {i + 1}"

    def test_sentence_splitting(self):
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = split_text(text, chunk_size=40, chunk_overlap=0)
        assert len(result) >= 2

    def test_long_word_not_split(self):
        # A single word longer than chunk_size should be kept whole
        text = "x" * 1000
        result = split_text(text, chunk_size=200, chunk_overlap=0)
        assert len(result) == 1
        assert result[0] == "x" * 1000

    def test_academic_text(self):
        text = (
            "1. Introduction\n\n"
            "We present a novel approach to quantum error correction. "
            "Our method combines surface codes with machine learning "
            "to achieve fault-tolerant quantum computation.\n\n"
            "2. Methods\n\n"
            "The experimental setup consists of a superconducting "
            "quantum processor with 127 qubits arranged in a heavy-hex "
            "lattice topology.\n\n"
            "3. Results\n\n"
            "We observe a significant reduction in logical error rates "
            "compared to previous approaches."
        )
        result = split_text(text, chunk_size=200, chunk_overlap=50)
        assert len(result) >= 3
        # All chunks should be non-empty
        assert all(chunk.strip() for chunk in result)


class TestSplitTable:
    HEADER = "| col A | col B | col C |\n|-------|-------|-------|"

    def _make_table(self, n_rows: int, row_text: str = "data") -> str:
        rows = [f"| {row_text}{i:03d} | x | y |" for i in range(n_rows)]
        return self.HEADER + "\n" + "\n".join(rows)

    def test_short_table_unchanged(self):
        text = self._make_table(3)
        result = split_table(text, chunk_size=10_000)
        assert result == [text.strip()]

    def test_empty_table(self):
        assert split_table("") == []
        assert split_table("   ") == []

    def test_split_preserves_header_in_each_chunk(self):
        text = self._make_table(50)
        result = split_table(text, chunk_size=200)
        assert len(result) >= 2
        for chunk in result:
            # Both header lines should be present at the top of every chunk
            assert chunk.startswith("| col A | col B | col C |")
            assert "|-------|-------|-------|" in chunk.splitlines()[1]

    def test_split_respects_chunk_size_with_hard_max_safety(self):
        text = self._make_table(100)
        hard_max = 5_000
        result = split_table(text, chunk_size=400, hard_max=hard_max)
        for chunk in result:
            assert len(chunk) <= hard_max

    def test_no_separator_row_still_splits_with_first_row_as_header(self):
        # Some markdown emitters drop the alignment row. We should still
        # treat row 0 as header.
        rows = [f"| r{i} | data |" for i in range(40)]
        text = "| H1 | H2 |\n" + "\n".join(rows)
        result = split_table(text, chunk_size=200)
        assert len(result) >= 2
        for chunk in result:
            assert chunk.startswith("| H1 | H2 |")

    def test_corrupted_single_row_falls_back_to_hard_max(self):
        # Mimic the Marker-OCR failure mode that triggered this work: one
        # giant "row" with no newlines, classified as a table.
        garbage = "| Pol<br>ym<br>er | data " * 5_000  # ~150K chars, no \n
        result = split_table(garbage, hard_max=8_000)
        assert all(len(chunk) <= 8_000 for chunk in result)
        # Should have produced multiple chunks (split, not lost).
        assert len(result) > 1

    def test_oversized_single_row_emits_with_header(self):
        big_row = "| " + ("x" * 1_000) + " | y | z |"
        text = self.HEADER + "\n" + big_row + "\n| small | small | small |"
        result = split_table(text, chunk_size=400, hard_max=10_000)
        # Every chunk must start with the header.
        for chunk in result:
            assert chunk.startswith("| col A | col B | col C |")


class TestEnforceHardMax:
    def test_short_chunks_pass_through(self):
        chunks = ["alpha", "beta", "gamma"]
        assert enforce_hard_max(chunks, hard_max=100) == chunks

    def test_oversized_chunk_is_split(self):
        big = "word " * 5_000  # 25K chars
        result = enforce_hard_max([big], hard_max=2_000)
        assert all(len(c) <= 2_000 for c in result)
        assert len(result) > 1

    def test_strips_empty_inputs(self):
        assert enforce_hard_max(["   ", "real content"]) == ["real content"]

    def test_default_hard_max_is_safe_for_bge_m3(self):
        # Sanity bound on the default — must be small enough that
        # 8192-token caps aren't blown by 2-char-per-token OCR.
        assert DEFAULT_HARD_MAX_CHARS <= 32_000
        assert DEFAULT_HARD_MAX_CHARS >= 8_000

    def test_rejects_nonpositive_hard_max(self):
        with pytest.raises(ValueError):
            enforce_hard_max(["x"], hard_max=0)
        with pytest.raises(ValueError):
            enforce_hard_max(["x"], hard_max=-5)
