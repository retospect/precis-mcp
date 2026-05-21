"""Tests for ``precis.ingest.pipeline``.

The module orchestrates Marker + the metadata cascade + the lookup
clients. None of those run in CI (Marker is a multi-GB ML model;
CrossRef/S2 are network calls). Tests stub them with
``unittest.mock.patch`` and exercise the data-shape contract
between :func:`precis.ingest.pipeline.extract_paper` /
``fetch_paper_by_*`` and :class:`precis.ingest.db_writer.PaperToWrite`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.ingest.pdf_metadata import DoiProvenance, PdfMetadata
from precis.ingest.pipeline import (
    _blocks_to_chunks,
    _build_cards,
    _resolve_identity,
    extract_paper,
    fetch_paper_by_arxiv,
    fetch_paper_by_doi,
)

# ---------------------------------------------------------------------------
# _blocks_to_chunks — pure mapping
# ---------------------------------------------------------------------------


class TestBlocksToChunks:
    def test_empty_input_yields_empty_list(self):
        assert _blocks_to_chunks([]) == []

    def test_simple_paragraph_block(self):
        blocks = [
            {"type": "paragraph", "text": "Hello world.", "page": 1, "section_path": []}
        ]
        chunks = _blocks_to_chunks(blocks)
        assert len(chunks) == 1
        assert chunks[0].ord == 0
        assert chunks[0].chunk_kind == "paragraph"
        assert chunks[0].text == "Hello world."
        assert chunks[0].page_first == 1

    def test_skipped_types_dont_consume_ord(self):
        blocks = [
            {"type": "junk", "text": "garbage"},
            {"type": "paragraph", "text": "first"},
            {"type": "page_header", "text": "header"},
            {"type": "paragraph", "text": "second"},
            {"type": "title", "text": "ignored title block"},
        ]
        chunks = _blocks_to_chunks(blocks)
        assert [c.text for c in chunks] == ["first", "second"]
        # ord must be contiguous starting at 0.
        assert [c.ord for c in chunks] == [0, 1]

    def test_unknown_type_falls_back_to_paragraph(self):
        blocks = [{"type": "frobnicator", "text": "weird"}]
        chunks = _blocks_to_chunks(blocks)
        assert chunks[0].chunk_kind == "paragraph"

    def test_empty_text_is_dropped(self):
        blocks = [
            {"type": "paragraph", "text": ""},
            {"type": "paragraph", "text": "    "},
            {"type": "paragraph", "text": "real"},
        ]
        chunks = _blocks_to_chunks(blocks)
        assert [c.text for c in chunks] == ["real"]

    def test_section_path_passed_through(self):
        blocks = [
            {
                "type": "paragraph",
                "text": "Body text",
                "page": 2,
                "section_path": ["1", "Introduction"],
            }
        ]
        chunks = _blocks_to_chunks(blocks)
        assert chunks[0].section_path == ["1", "Introduction"]


# ---------------------------------------------------------------------------
# _build_cards — synthetic ref-level chunks
# ---------------------------------------------------------------------------


class TestBuildCards:
    def test_minimal_yields_card_combined_only(self):
        cards = _build_cards(title="", authors=[], abstract="", keywords=[])
        assert len(cards) == 1
        assert cards[0].chunk_kind == "card_combined"
        # ord must be < 0 per the schema CHECK.
        assert cards[0].ord < 0
        # Empty inputs collapse to a placeholder so the row never carries
        # an empty text (the chunks.text column is NOT NULL).
        assert cards[0].text == "[no metadata]"

    def test_card_kinds_only_emitted_when_input_present(self):
        cards = _build_cards(
            title="A Paper",
            authors=[{"name": "Smith, John"}],
            abstract="A study of X.",
            keywords=["x", "y"],
        )
        kinds = [c.chunk_kind for c in cards]
        assert kinds == [
            "card_combined",
            "card_title",
            "card_authors",
            "card_abstract",
            "card_keywords",
        ]
        # ords are strictly decreasing and all negative.
        ords = [c.ord for c in cards]
        assert all(o < 0 for o in ords)
        assert ords == sorted(ords, reverse=True)

    def test_card_combined_concatenates_fields(self):
        cards = _build_cards(
            title="Paper",
            authors=[{"name": "A"}, {"name": "B"}],
            abstract="abs",
            keywords=["k1"],
        )
        combined = next(c for c in cards if c.chunk_kind == "card_combined")
        assert "Paper" in combined.text
        assert "A; B" in combined.text
        assert "abs" in combined.text
        assert "k1" in combined.text


# ---------------------------------------------------------------------------
# _resolve_identity
# ---------------------------------------------------------------------------


class TestResolveIdentity:
    def test_doi_input_yields_pub_id(self):
        paper_id, pub_id, cite_key = _resolve_identity(
            doi="10.1038/x",
            arxiv_id=None,
            pdf_sha256=None,
            authors=[{"name": "Smith, John"}],
            year=2024,
        )
        assert paper_id  # non-empty
        assert pub_id is not None  # DOI present → pub_id minted
        assert cite_key.startswith("smith24")

    def test_pdf_only_has_no_pub_id(self):
        paper_id, pub_id, _ = _resolve_identity(
            doi=None,
            arxiv_id=None,
            pdf_sha256="a" * 64,
            authors=[],
            year=None,
        )
        assert paper_id
        # No public handle → pub_id stays None.
        assert pub_id is None

    def test_arxiv_input_takes_priority(self):
        paper_id, pub_id, _ = _resolve_identity(
            doi="10.1/x",
            arxiv_id="2401.12345",
            pdf_sha256=None,
            authors=[],
            year=None,
        )
        assert "arxiv:" in paper_id
        assert pub_id is not None


# ---------------------------------------------------------------------------
# extract_paper — local PDF, Marker stubbed
# ---------------------------------------------------------------------------


class TestExtractPaper:
    def test_missing_pdf_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            extract_paper(tmp_path / "does-not-exist.pdf")

    def test_full_flow_with_stubbed_marker(self, tmp_path: Path):
        pdf = tmp_path / "smith2024.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fake content for hashing\n")

        fake_meta = PdfMetadata(
            pdf_path=pdf,
            pdf_hash="ignored",
            title="Quantum Error Correction in Practice",
            authors=["Smith, John", "Jones, Alice"],
            doi="10.1038/s41567-024-1234-5",
            doi_provenance=DoiProvenance.SECONDARY_VALIDATOR,
            year=2024,
            journal="Nature Physics",
            abstract="We present a new approach.",
            keywords=["surface codes", "fault tolerance"],
        )

        fake_blocks = [
            {
                "type": "title",
                "text": "Quantum Error Correction in Practice",
                "page": 1,
                "section_path": [],
            },
            {
                "type": "paragraph",
                "text": "Surface codes have emerged as the leading candidate.",
                "page": 1,
                "section_path": ["1", "Introduction"],
            },
            {"type": "junk", "text": "© 2024 Reserved", "page": 1},
            {
                "type": "paragraph",
                "text": "We achieve a logical error rate of 1e-6.",
                "page": 2,
                "section_path": ["2", "Results"],
            },
            {
                "type": "figure",
                "text": "Figure 1: Surface code diagram.",
                "page": 2,
            },
        ]

        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=fake_meta,
            ),
            patch(
                "precis.ingest.pipeline.extract_blocks_marker",
                return_value=fake_blocks,
            ),
        ):
            paper = extract_paper(pdf)

        assert isinstance(paper, PaperToWrite)
        assert paper.title == "Quantum Error Correction in Practice"
        assert paper.year == 2024
        assert paper.doi == "10.1038/s41567-024-1234-5"
        assert paper.provider == "crossref"
        assert paper.cite_key_prefix.startswith("smith24")
        assert paper.pdf_sha256 is not None
        assert len(paper.pdf_sha256) == 64
        assert paper.content_hash is not None
        assert paper.pdf_role == "main"
        assert paper.pdf_pages_first == 1
        assert paper.pdf_pages_last == 2
        assert paper.pdf_size_bytes == len(pdf.read_bytes())
        # 5 cards (combined + title + authors + abstract + keywords)
        # plus 3 body chunks (the junk block and the title block are dropped).
        cards = [c for c in paper.chunks if c.ord < 0]
        body = [c for c in paper.chunks if c.ord >= 0]
        assert len(cards) == 5
        assert len(body) == 3
        assert [c.chunk_kind for c in body] == ["paragraph", "paragraph", "figure"]
        assert paper.meta["abstract"] == "We present a new approach."
        assert paper.meta["journal"] == "Nature Physics"

    def test_no_metadata_still_yields_card_combined(self, tmp_path: Path):
        """Empty metadata + zero blocks must still produce one card_combined
        so the worker queue has *something* to embed."""
        pdf = tmp_path / "anon.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        empty_meta = PdfMetadata(pdf_path=pdf)

        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=empty_meta,
            ),
            patch("precis.ingest.pipeline.extract_blocks_marker", return_value=[]),
        ):
            paper = extract_paper(pdf)

        assert paper.title == ""
        assert paper.year is None
        cards = [c for c in paper.chunks if c.ord < 0]
        body = [c for c in paper.chunks if c.ord >= 0]
        assert len(cards) == 1
        assert cards[0].chunk_kind == "card_combined"
        assert cards[0].text == "[no metadata]"
        assert body == []


# ---------------------------------------------------------------------------
# fetch_paper_by_doi — CrossRef stubbed
# ---------------------------------------------------------------------------


class TestFetchPaperByDoi:
    def test_invalid_doi_raises(self):
        with pytest.raises(ValueError):
            fetch_paper_by_doi("not-a-doi")

    def test_crossref_miss_raises(self):
        with patch("precis.ingest.pipeline.lookup_doi", return_value=None):
            with pytest.raises(ValueError, match="CrossRef miss"):
                fetch_paper_by_doi("10.1038/missing")

    def test_full_flow(self):
        cr_result = {
            "title": "A Paper",
            "authors": [{"name": "Smith, John"}],
            "year": 2024,
            "abstract": "abs",
            "journal": "Nature",
            "keywords": ["x"],
        }
        with patch("precis.ingest.pipeline.lookup_doi", return_value=cr_result):
            paper = fetch_paper_by_doi("10.1038/test")

        assert paper.doi == "10.1038/test"
        assert paper.arxiv_id is None
        assert paper.pdf_sha256 is None  # metadata-only
        assert paper.provider == "crossref"
        assert paper.cite_key_prefix.startswith("smith24")
        body = [c for c in paper.chunks if c.ord >= 0]
        assert body == []  # no body chunks for metadata-only
        cards = [c for c in paper.chunks if c.ord < 0]
        assert any(c.chunk_kind == "card_combined" for c in cards)


# ---------------------------------------------------------------------------
# fetch_paper_by_arxiv — S2 stubbed
# ---------------------------------------------------------------------------


class TestFetchPaperByArxiv:
    def test_invalid_arxiv_raises(self):
        with pytest.raises(ValueError):
            fetch_paper_by_arxiv("not-an-arxiv-id")

    def test_s2_miss_raises(self):
        with patch("precis.ingest.pipeline.lookup_s2", return_value=None):
            with pytest.raises(ValueError, match="S2 miss"):
                fetch_paper_by_arxiv("2401.99999")

    def test_arxiv_pulls_doi_when_present(self):
        s2_result = {
            "title": "Preprint",
            "authors": [{"name": "Wei, Lin"}],
            "year": 2024,
            "abstract": "",
            "doi": "10.1038/preprint",
            "s2_id": "abc123",
        }
        with patch("precis.ingest.pipeline.lookup_s2", return_value=s2_result):
            paper = fetch_paper_by_arxiv("2401.12345")

        assert paper.arxiv_id == "2401.12345"
        assert paper.doi == "10.1038/preprint"  # S2's DOI carried through
        assert paper.s2_id == "abc123"
        assert paper.provider == "s2"
        assert paper.cite_key_prefix.startswith("wei24")


# ---------------------------------------------------------------------------
# Type-check: returned objects are valid input to db_writer.write_paper
# ---------------------------------------------------------------------------


class TestPaperToWriteContract:
    """Quick structural assertions that the pipeline outputs match
    ``write_paper``'s contract (paper_id non-empty, cite_key_prefix
    non-empty, ord/chunk_kind invariants)."""

    def test_extract_paper_satisfies_writer_invariants(self, tmp_path: Path):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=PdfMetadata(pdf_path=pdf, title="T", authors=["A, B"]),
            ),
            patch("precis.ingest.pipeline.extract_blocks_marker", return_value=[]),
        ):
            paper = extract_paper(pdf)

        assert paper.paper_id  # required by write_paper
        assert paper.cite_key_prefix  # required by write_paper
        for c in paper.chunks:
            assert isinstance(c, ChunkToWrite)
            if c.ord < 0:
                assert c.chunk_kind.startswith("card_")
            else:
                assert not c.chunk_kind.startswith("card_")
