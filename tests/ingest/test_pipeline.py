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
    _EM_DASH_LOST_RE,
    _REPLACEMENT_CHAR,
    _blocks_to_chunks,
    _build_cards,
    _repair_mojibake,
    _resolve_identity,
    _retag_references,
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

    def test_numerics_extracted_per_chunk(self):
        # Path-2 lexical numeric-token index: every body chunk
        # carries a ``numerics`` array of recognized
        # ``<number><unit>`` tokens for the chunks.numerics GIN
        # index to consume.
        blocks = [
            {
                "type": "paragraph",
                "text": "bandgap of 1.523 eV with 12% efficiency at 25 °C",
                "page": 1,
                "section_path": [],
            }
        ]
        chunks = _blocks_to_chunks(blocks)
        assert "1.523 eV" in chunks[0].numerics
        assert "12%" in chunks[0].numerics
        assert "25 °C" in chunks[0].numerics

    def test_numerics_empty_when_text_has_no_units(self):
        blocks = [
            {
                "type": "paragraph",
                "text": "just prose with no numbers",
                "section_path": [],
            }
        ]
        chunks = _blocks_to_chunks(blocks)
        assert chunks[0].numerics == []


# ---------------------------------------------------------------------------
# _retag_references — references-section promotion
# ---------------------------------------------------------------------------


class TestRetagReferences:
    """Storage-v2 contract: bibliography chunks must land with
    ``chunk_kind='references'`` so the embedder worker can skip them
    via ``skip_chunk_kinds``. Marker tags the heading but leaves the
    following paragraphs as text — :func:`_retag_references` closes
    that gap by delegating to the boilerplate classifier.
    """

    def _bibliography(self) -> list[str]:
        # Real bibliographies usually land as one chunk with many
        # citation lines — boilerplate's density heuristic
        # (matches >= 3 AND matches/lines >= 0.3) requires multi-line
        # input. We test the realistic shape: one heading chunk plus
        # one big multi-citation chunk.
        return [
            "# References",
            (
                "1. Smith, J. et al. (2020). Metal-organic frameworks "
                "for CO2 reduction. Nature Chem. 12, 100-110.\n"
                "2. Johnson, A. & Lee, B. (2021). Cu-MOF synthesis "
                "and characterization. JACS 143, 5000-5010.\n"
                "3. Brown, C. (2022). Electrocatalysis at the "
                "nanoscale. Chem. Rev. 122, 8000-8050.\n"
                "4. Chen, D. & Wang, E. (2023). Faradaic efficiency "
                "improvements. J. Am. Chem. Soc. 145, 9000-9010."
            ),
        ]

    def _body_paragraph(self) -> str:
        return (
            "We synthesized Cu-MOF nanocrystals via solvothermal methods "
            "in DMF at 120 degrees Celsius for 24 hours. The resulting "
            "crystals were characterized by powder XRD, FTIR, and SEM "
            "to confirm phase purity and morphology. Yields exceeded "
            "85 percent in all batches."
        )

    def test_references_chunks_retagged(self):
        body = self._body_paragraph()
        bib = self._bibliography()
        chunks = [
            ChunkToWrite(ord=0, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=1, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=2, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=3, chunk_kind="heading", text=bib[0]),
            ChunkToWrite(ord=4, chunk_kind="paragraph", text=bib[1]),
        ]
        out = _retag_references(chunks)
        # Body chunks unchanged.
        assert out[0].chunk_kind == "paragraph"
        assert out[1].chunk_kind == "paragraph"
        assert out[2].chunk_kind == "paragraph"
        # The references heading flips via the heading regex; the
        # multi-line bibliography chunk flips via citation-density.
        assert out[3].chunk_kind == "references", (
            f"references heading should be retagged; "
            f"got chunk_kind={out[3].chunk_kind!r}"
        )
        assert out[4].chunk_kind == "references", (
            f"bibliography block should be retagged; "
            f"got chunk_kind={out[4].chunk_kind!r}"
        )

    def test_empty_input_passes_through(self):
        assert _retag_references([]) == []

    def test_no_references_section_no_change(self):
        body = self._body_paragraph()
        chunks = [
            ChunkToWrite(ord=0, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=1, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=2, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=3, chunk_kind="paragraph", text=body),
        ]
        out = _retag_references(chunks)
        for c in out:
            assert c.chunk_kind == "paragraph"

    def test_already_tagged_idempotent(self):
        # A second pass on already-correctly-tagged chunks must not
        # rewrite or re-allocate them.
        body = self._body_paragraph()
        bib = self._bibliography()
        chunks = [
            ChunkToWrite(ord=0, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=1, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=2, chunk_kind="paragraph", text=body),
            ChunkToWrite(ord=3, chunk_kind="references", text=bib[0]),
            ChunkToWrite(ord=4, chunk_kind="references", text=bib[1]),
        ]
        out = _retag_references(chunks)
        # All references entries stay references; body stays body.
        assert [c.chunk_kind for c in out] == [
            "paragraph",
            "paragraph",
            "paragraph",
            "references",
            "references",
        ]

    def test_no_body_chunks_unchanged(self):
        # Edge case: a ref with only a references-section block
        # (e.g. metadata-only ingest that later got bibliography
        # appended) — single-chunk papers are skipped by the
        # boilerplate classifier (too short for the heuristic to
        # fire), so this stays as-is.
        bib = self._bibliography()
        chunks = [
            ChunkToWrite(ord=0, chunk_kind="paragraph", text=bib[1]),
        ]
        out = _retag_references(chunks)
        # Boilerplate classifier returns BODY for tiny papers; no
        # retag happens. This is by design — see classify_chunks.
        assert out[0].chunk_kind == "paragraph"


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

    def test_printable_only_skips_marker_entirely(self, tmp_path: Path) -> None:
        """gr161905: a PDF fetched alongside a markup trigger must never
        run Marker — it's the attach-only printable, never a second,
        order-dependent body candidate."""
        pdf = tmp_path / "companion.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 200)

        fake_meta = PdfMetadata(
            pdf_path=pdf, doi="10.1038/s41567-024-1234-5", title="X", year=2024
        )

        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=fake_meta,
            ),
            patch(
                "precis.ingest.pipeline.extract_blocks_marker",
                side_effect=AssertionError("Marker must not run"),
            ) as marker,
            patch(
                "precis.ingest.pipeline._pdf_page_count",
                return_value=7,
            ),
        ):
            paper = extract_paper(pdf, printable_only=True)

        assert marker.call_count == 0
        assert paper.chunks == []
        assert paper.pdf_sha256 is not None and len(paper.pdf_sha256) == 64
        assert paper.pdf_page_count == 7
        assert paper.pdf_pages_first is None
        assert paper.pdf_pages_last is None
        assert paper.doi == "10.1038/s41567-024-1234-5"


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


# ---------------------------------------------------------------------------
# _repair_or_fail_mojibake — U+FFFD survival check
# ---------------------------------------------------------------------------


class TestRepairMojibake:
    """Pins the U+FFFD em-dash repair pass.

    The high-precision ``LETTER ␣ FFFD ␣ LETTER`` pattern is rewritten
    to ``LETTER ␣ — ␣ LETTER``. Anything else is left as U+FFFD —
    that character is itself the canonical Unicode "byte sequence I
    could not decode" sentinel and is the most honest representation
    when the ToUnicode map gap can't be guessed safely.
    """

    @staticmethod
    def _block(text: str, *, page: int = 0, btype: str = "text") -> dict:
        return {"page": page, "type": btype, "text": text}

    def test_repairs_em_dash_between_words(self) -> None:
        blocks = [self._block(f"compound {_REPLACEMENT_CHAR} silver")]
        result = _repair_mojibake(blocks)
        assert result[0]["text"] == "compound — silver"

    def test_passes_through_text_without_fffd(self) -> None:
        blocks = [
            self._block("normal prose"),
            self._block("already has — em-dash"),
        ]
        result = _repair_mojibake(blocks)
        assert result[0]["text"] == "normal prose"
        assert result[1]["text"] == "already has — em-dash"

    def test_unrepairable_fffd_stays_in_text(self) -> None:
        """FFFD inside numbers / next to punctuation / mid-word is
        left in place — we don't guess what was lost, and FFFD is
        the standard Unicode sentinel for exactly this situation.
        """
        cases = [
            f"value{_REPLACEMENT_CHAR}, then more",
            f"page 1{_REPLACEMENT_CHAR}2 of report",
            f"compound{_REPLACEMENT_CHAR}silver",
        ]
        for bad in cases:
            blocks = [self._block(bad, page=3)]
            result = _repair_mojibake(blocks)
            assert result[0]["text"] == bad  # untouched

    def test_em_dash_repair_runs_per_block(self) -> None:
        # Repair walks every block; non-FFFD blocks are skipped fast.
        blocks = [
            self._block("clean prose"),
            self._block(f"x {_REPLACEMENT_CHAR} y", page=4),
            self._block(f"still {_REPLACEMENT_CHAR}gap unrepairable"),
        ]
        result = _repair_mojibake(blocks)
        assert result[0]["text"] == "clean prose"
        assert result[1]["text"] == "x — y"
        # Mid-block FFFD without flanking spaces stays as-is.
        assert _REPLACEMENT_CHAR in result[2]["text"]

    def test_empty_input_returns_empty(self) -> None:
        assert _repair_mojibake([]) == []

    def test_regex_does_not_match_digit_or_punctuation(self) -> None:
        # Direct regex assertions — useful for diagnosing future failures.
        assert _EM_DASH_LOST_RE.search(f"a {_REPLACEMENT_CHAR} b")
        assert not _EM_DASH_LOST_RE.search(f"1 {_REPLACEMENT_CHAR} 2")
        assert not _EM_DASH_LOST_RE.search(f"a{_REPLACEMENT_CHAR}b")
        assert not _EM_DASH_LOST_RE.search(f"a {_REPLACEMENT_CHAR} ,")
