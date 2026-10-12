"""Multi-article page spreads keep only the target article (gr463414).

The block shapes mirror real Marker output from prod: pa478893 (Kroto 1987, a
catalysis letter on each side) and pa42559 / pa44468 (four Nature 1998
correspondence pieces on one spread, bylines at the end).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

from precis.ingest.add import (
    ARTICLE_SPREAD_SOURCE,
    MULTI_ARTICLE_TAG,
    IngestResult,
    PdfInput,
    precis_add,
)
from precis.ingest.article_spread import trim_to_article
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.ingest.pdf_metadata import PdfMetadata
from precis.ingest.pipeline import extract_paper

KROTO = "The stability of the fullerenes Cn, with n = 24, 28, 32, 36, 50, 60 and 70"
PROSE = (
    "It has been proposed that the geodesic and chemical properties inherent in "
    "a closed, hollow, spheroidal carbon cage account for the stability of the "
    "fullerenes observed in cluster beams, and that the pentagon rule explains "
    "why some cluster sizes dominate the mass spectra recorded under gentle ionization."
)
OTHER_PROSE = (
    "The catalytic activation of hydrocarbon C-H bonds is a fundamental process "
    "in heterogeneous catalysis. Although silicon dioxide is usually considered "
    "inert, we find that thin overlayers on silicon are active for the exchange "
    "of hydrogen with deuterium at moderate temperatures in a flow reactor."
)


def _b(type_: str, text: str, page: int = 0) -> dict[str, Any]:
    return {"type": type_, "text": text, "page": page, "section_path": []}


def _kroto_spread() -> list[dict[str, Any]]:
    """Previous letter's tail, Kroto 1987, next letter's byline + head."""
    return [
        _b("text", "1986). " + OTHER_PROSE),
        _b(
            "text",
            "The fact that the solid products represent catalysts. " + OTHER_PROSE,
        ),
        _b(
            "text",
            "- 1. Brown, W. E., Dollimore, D. & Galwey, A. K. in Comprehensive "
            "Chemical Kinetics Vol. 22, 167-174 (Elsevier, Amsterdam, 1980).",
        ),
        _b("section_header", f"**{KROTO}**"),
        _b("text", PROSE),
        _b("text", "These results provide further evidence. " + PROSE),
        _b("text", "C60 is the smallest closed network. " + PROSE),
        _b("text", "I thank David Walton for helpful discussions."),
        _b(
            "section_header",
            "Alan B. McEwen\\*, Wilhelm F. Maier\\*t & Scott M. Baumann:!:",
            page=2,
        ),
        _b("text", "Catalytic C-H bond activation on silicon dioxide overlayers", 2),
        _b("text", OTHER_PROSE, 2),
    ]


def _correspondence_spread() -> list[dict[str, Any]]:
    """Nature 1998 correspondence: title, body, end byline, references."""
    return [
        _b("text", "decreasing growth in the early limb bud. " + OTHER_PROSE),
        _b("section_header", "Clifford J. Tabin"),
        _b("text", "- 1. Lenz, W. Lancet 1, 45 (1962)."),
        _b("section_header", "Encapsulated C<sub>60</sub> in carbon nanotubes"),
        _b("text", "Here we use HRTEM to show closed shells in nanotubes. " + PROSE),
        _b("text", "HRTEM observations were made on purified material. " + PROSE),
        _b("text", "The coalescence may therefore be a reconfiguration. " + PROSE),
        _b("section_header", "scientific correspondence", page=1),
        _b(
            "section_header",
            "Brian W. Smith\\*, Marc Monthioux\\*†, David E. Luzzi\\*",
            1,
        ),
        _b("text", "- 1. Rinzler, A. G. et al. Appl. Phys. A 67, 29-37 (1998).", 1),
        _b("section_header", "How does xenon produce anaesthesia?", 1),
        _b("text", "We found that xenon had virtually no effect. " + OTHER_PROSE, 1),
        _b("section_header", "N. P. Franks, R. Dickinson, W. R. Lieb", 1),
        _b("references", "- Cullen, S. C. & Gross, E. G. Science 113, 580 (1951).", 1),
    ]


def _single_paper() -> list[dict[str, Any]]:
    """One ordinary article: front-matter affiliation, standard headings,
    references, then a post-reference Methods section."""
    return [
        _b("section_header", "Graphene growth on copper foils"),
        _b("text", "Jane Doe, Department of Physics, University of Somewhere"),
        _b("text", PROSE),
        _b("section_header", "Introduction"),
        _b("text", PROSE),
        _b("section_header", "Growth kinetics on polycrystalline copper"),
        _b("text", PROSE),
        _b("section_header", "References"),
        _b("references", "1. Smith, J. Nature 1, 2 (2001)."),
        _b("section_header", "Methods"),
        _b("text", PROSE),
        _b("section_header", "2.1 Sample preparation"),
        _b("text", PROSE),
    ]


class TestTrimToArticle:
    def test_kroto_spread_keeps_only_kroto(self) -> None:
        blocks = _kroto_spread()
        trim = trim_to_article(blocks, title=KROTO)
        assert trim.verdict == "trimmed"
        assert trim.reason == "title"
        assert trim.blocks == blocks[3:8]
        audit = trim.audit()
        assert audit is not None
        assert audit["kept"]["blocks"] == [3, 8]
        assert [c["blocks"] for c in audit["cut"]] == [[0, 3], [8, 11]]
        assert audit["cut"][1]["pages"] == [2, 2]

    def test_correspondence_spread_keeps_target_with_its_byline(self) -> None:
        blocks = _correspondence_spread()
        trim = trim_to_article(
            blocks, title="Encapsulated {C$_{60}$} in carbon nanotubes"
        )
        assert trim.verdict == "trimmed"
        # Title, body, running head, end byline and its references.
        assert trim.blocks == blocks[3:10]
        texts = " ".join(b["text"] for b in trim.blocks)
        assert "xenon" not in texts and "limb bud" not in texts

    def test_doi_in_article_wins_over_title(self) -> None:
        blocks = _kroto_spread()
        blocks[4] = _b("text", "doi:10.1038/329529a0 " + PROSE)
        trim = trim_to_article(blocks, title="", doi="10.1038/329529A0")
        assert trim.verdict == "trimmed"
        assert trim.reason == "doi"
        assert trim.blocks == blocks[3:8]

    def test_target_title_absent_is_ambiguous(self) -> None:
        # pa44468: the thalidomide title is not on the spread, only its tail.
        blocks = _correspondence_spread()
        trim = trim_to_article(
            blocks, title="A developmental model for thalidomide defects"
        )
        assert trim.verdict == "ambiguous"
        assert trim.blocks is blocks
        audit = trim.audit()
        assert audit is not None and len(audit["found"]) == 3

    def test_no_identity_is_ambiguous(self) -> None:
        trim = trim_to_article(_correspondence_spread())
        assert trim.verdict == "ambiguous"
        assert trim.reason == "no title or DOI to match"

    def test_no_identity_one_evidence_is_single(self) -> None:
        # Only the McEwen byline is article evidence: not enough to tag.
        assert trim_to_article(_kroto_spread()).verdict == "single"

    def test_unconfirmed_trailing_piece_stays_as_residual(self) -> None:
        # pa42559: the xenon piece lost its byline, so it is not cut; the
        # confirmed leading cut still happens and the result flags residual.
        blocks = _correspondence_spread()[:12]
        trim = trim_to_article(
            blocks, title="Encapsulated {C$_{60}$} in carbon nanotubes"
        )
        assert trim.verdict == "trimmed"
        assert trim.residual
        assert trim.blocks == blocks[3:12]
        audit = trim.audit()
        assert audit is not None and audit["residual"] is True

    def test_ordinary_paper_is_single(self) -> None:
        blocks = _single_paper()
        trim = trim_to_article(blocks, title="Graphene growth on copper foils")
        assert trim.verdict == "single"
        assert trim.blocks is blocks
        assert trim.audit() is None

    def test_two_titles_matching_is_ambiguous(self) -> None:
        # Both articles' heads match the target title: refuse to guess.
        blocks = _kroto_spread()
        blocks[8] = _b("section_header", KROTO + " revisited")
        blocks[9] = _b("text", PROSE)
        blocks[10] = _b("text", "- 2. Kroto, H. W. Nature 329, 529 (1987).")
        trim = trim_to_article(blocks, title=KROTO)
        assert trim.verdict != "trimmed"
        assert trim.blocks is blocks


def _ordinary(tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    head = [
        _b("section_header", "Graphene growth on copper foils"),
        _b("text", "Jane Doe and John Roe, Department of Physics, University X"),
    ]
    return head + [_b("text", PROSE) for _ in range(4)] + tail


# Ordinary single-article layouts that must never lose content (reviewer
# reproductions of the first draft's false cuts).
ORDINARY_TAILS: dict[str, list[dict[str, Any]]] = {
    "cell_star_methods": [
        _b("section_header", "References"),
        _b("text", "Smith, J. A. Cell 1, 2 (2001)."),
        _b("section_header", "STAR★METHODS"),
        _b("text", PROSE),
        _b("section_header", "Cell culture and transfection"),
        _b("text", PROSE),
    ],
    "funding_declarations": [
        _b("section_header", "Funding"),
        _b("text", "Supported by Stanford University grant 5."),
        _b("section_header", "Declarations"),
        _b("text", PROSE),
    ],
    "ack_then_glyph_references": [
        _b("section_header", "ACKNOWLEDGMENTS"),
        _b("text", "We thank X for discussions."),
        _b("section_header", "■ REFERENCES"),
        _b("references", "(1) Smith, J. A. J. Am. Chem. Soc. 1, 2 (2001)."),
    ],
    "thesis_chapters": [
        _b("section_header", "References"),
        _b("text", "Smith, J. A. Some book (2001)."),
        _b("section_header", "Chapter 2 Methods overview"),
        _b("text", PROSE),
    ],
    "mid_body_affiliation": [
        _b("text", "Data from the Medical Center registry of cases."),
        _b("section_header", "Limitations of the study"),
        _b("text", PROSE),
    ],
    "we_acknowledge": [
        _b("text", "We acknowledge that this is limited."),
        _b("section_header", "Where the model breaks down"),
        _b("text", PROSE),
    ],
    "correspondence_analysis": [
        _b("text", "Correspondence analysis (CA) is used here."),
        _b("section_header", "Ordination of the samples"),
        _b("text", PROSE),
    ],
    "ieee_biographies": [
        _b("section_header", "References"),
        _b("references", "[1] Smith, J. A. IEEE Trans. 1, 2 (2001)."),
        _b("section_header", "John A. Smith"),
        _b(
            "text",
            "John A. Smith received the B.S. degree from Stanford University "
            "and is currently a professor. " + PROSE,
        ),
    ],
    # pa4460 (Chem Soc Rev): Marker clipped the headings; RSC references put
    # initials first and carry other papers' DOIs.
    "rsc_clipped_references": [
        _b("section_header", "cknowledgement"),
        _b("text", "Acknowledgements  Part of this work was inspired by talks."),
        _b("section_header", "eference"),
        _b(
            "text",
            "References  - 1 M. S. Holm, E. Taarning and C. H. Christensen, "
            "Catal. Today, 2011, 168, 3-16, doi:10.1039/c5cs00599j",
        ),
        _b(
            "text",
            "- 6 J. Perez-Ramirez, C. H. Christensen and J. C. Groen, Chem. Soc. "
            "Rev., 2008, 37, 2530, doi:10.1038/ncomms9633",
        ),
    ],
    "supporting_info_titled": [
        _b("section_header", "References"),
        _b("references", "1. Smith, J. A. Nature 1, 2 (2001)."),
        _b(
            "section_header",
            "Supplementary information for graphene growth on copper foils",
        ),
        _b("text", "Graphene growth on copper foils: extra data. " + PROSE),
        _b("text", "Jane Doe and John Roe, Department of Physics, University X"),
    ],
}


class TestOrdinaryPapersUntouched:
    def test_tails_keep_every_block(self) -> None:
        for name, tail in ORDINARY_TAILS.items():
            blocks = _ordinary(tail)
            trim = trim_to_article(blocks, title="Graphene growth on copper foils")
            assert trim.blocks is blocks, name
            assert trim.verdict != "trimmed", name


class TestExtractPaperTrims:
    def test_chunks_carry_only_target_article(self, tmp_path: Path) -> None:
        pdf = tmp_path / "kroto87.pdf"
        pdf.write_bytes(b"%PDF-1.4 spread")
        meta = PdfMetadata(pdf_path=pdf, title=KROTO, year=1987)
        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=meta,
            ),
            patch(
                "precis.ingest.pipeline.extract_blocks_marker",
                return_value=_kroto_spread(),
            ),
        ):
            paper = extract_paper(pdf)
        body = [c.text for c in paper.chunks if c.ord >= 0]
        assert body and all("silicon dioxide" not in t for t in body)
        assert not any("Comprehensive Chemical Kinetics" in t for t in body)
        assert paper.meta["article_spread"]["verdict"] == "trimmed"
        # Page stats describe the whole PDF, so a trim never reads as a preview.
        assert paper.pdf_pages_first == 0 and paper.pdf_pages_last == 2

    def test_target_title_overrides_metadata(self, tmp_path: Path) -> None:
        pdf = tmp_path / "pa1.pdf"
        pdf.write_bytes(b"%PDF-1.4 spread2")
        meta = PdfMetadata(pdf_path=pdf, title="", year=1987)
        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=meta,
            ),
            patch(
                "precis.ingest.pipeline.extract_blocks_marker",
                return_value=_kroto_spread(),
            ),
        ):
            paper = extract_paper(pdf, target_title=KROTO)
        assert paper.meta["article_spread"]["verdict"] == "trimmed"

    def test_single_article_has_no_spread_meta(self, tmp_path: Path) -> None:
        pdf = tmp_path / "single.pdf"
        pdf.write_bytes(b"%PDF-1.4 single")
        meta = PdfMetadata(pdf_path=pdf, title="Graphene growth on copper foils")
        with (
            patch(
                "precis.ingest.pipeline.extract_metadata_from_sources",
                return_value=meta,
            ),
            patch(
                "precis.ingest.pipeline.extract_blocks_marker",
                return_value=_single_paper(),
            ),
        ):
            paper = extract_paper(pdf)
        assert "article_spread" not in paper.meta


def _paper(sha: str, spread: dict[str, Any] | None) -> PaperToWrite:
    meta: dict[str, Any] = {} if spread is None else {"article_spread": spread}
    return PaperToWrite(
        title=KROTO,
        authors=[{"name": "Kroto, H. W."}],
        year=1987,
        kind="paper",
        provider="local",
        set_by="system",
        paper_id="sp" + sha[:6],
        pub_id=None,
        cite_key_prefix="kroto87",
        pdf_sha256=sha,
        content_hash=sha,
        pdf_storage_path="/tmp/fake.pdf",
        pdf_page_count=3,
        pdf_size_bytes=100,
        meta=meta,
        chunks=[
            ChunkToWrite(ord=-1, chunk_kind="card_combined", text=KROTO),
            ChunkToWrite(ord=0, chunk_kind="paragraph", text=PROSE, page_first=0),
        ],
    )


def _events_and_tag(store: Any, ref_id: int) -> tuple[list[Any], bool, Any]:
    with store.pool.connection() as conn:
        events = conn.execute(
            "SELECT event, payload FROM ref_events WHERE ref_id = %s AND source = %s",
            (ref_id, ARTICLE_SPREAD_SOURCE),
        ).fetchall()
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()[0]
    return events, store.has_tag(ref_id, "OPEN", MULTI_ARTICLE_TAG), meta


class TestIngestJournalsSpread:
    def _ingest(
        self, store: Any, tmp_path: Path, name: str, spread: Any
    ) -> IngestResult:
        pdf = tmp_path / name
        pdf.write_bytes(b"%PDF-1.4 " + name.encode())
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        with patch(
            "precis.ingest.pipeline.extract_paper", return_value=_paper(sha, spread)
        ):
            result = precis_add(PdfInput(pdf_path=pdf), store=store)
        assert isinstance(result, IngestResult)
        return result

    def test_trimmed_spread_is_journalled_not_tagged(
        self, store, tmp_path: Path
    ) -> None:
        spread = {
            "verdict": "trimmed",
            "reason": "title",
            "articles": 3,
            "kept": {"blocks": [3, 8], "pages": [0, 0], "head": KROTO, "score": 1.0},
            "cut": [
                {"blocks": [0, 3], "pages": [0, 0], "head": "1986).", "score": 0.0}
            ],
        }
        result = self._ingest(store, tmp_path, "trim.pdf", spread)
        events, tagged, meta = _events_and_tag(store, result.ref_id)
        assert [e[0] for e in events] == ["trimmed"]
        assert events[0][1]["kept"]["blocks"] == [3, 8]
        assert events[0][1]["file"] == "trim.pdf"
        assert not tagged
        assert "article_spread" not in (meta or {})

    def test_ambiguous_spread_tags_multi_article(self, store, tmp_path: Path) -> None:
        spread = {"verdict": "ambiguous", "reason": "no confident match", "articles": 4}
        result = self._ingest(store, tmp_path, "amb.pdf", spread)
        events, tagged, _meta = _events_and_tag(store, result.ref_id)
        assert [e[0] for e in events] == ["ambiguous"]
        assert tagged

    def test_residual_trim_is_tagged(self, store, tmp_path: Path) -> None:
        spread = {"verdict": "trimmed", "reason": "title", "residual": True}
        result = self._ingest(store, tmp_path, "res.pdf", spread)
        events, tagged, _meta = _events_and_tag(store, result.ref_id)
        assert [e[0] for e in events] == ["trimmed"]
        assert tagged

    def test_single_article_writes_nothing(self, store, tmp_path: Path) -> None:
        result = self._ingest(store, tmp_path, "one.pdf", None)
        events, tagged, _meta = _events_and_tag(store, result.ref_id)
        assert events == [] and not tagged

    def test_handle_named_drop_targets_the_stub_identity(
        self, store, tmp_path: Path
    ) -> None:
        stub = store.insert_ref(kind="paper", slug="kroto87", title=KROTO)
        pdf = tmp_path / f"pa{stub.id}.pdf"
        pdf.write_bytes(b"%PDF-1.4 handle spread")
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        with patch(
            "precis.ingest.pipeline.extract_paper", return_value=_paper(sha, None)
        ) as m:
            precis_add(PdfInput(pdf_path=pdf), store=store)
        assert m.call_args.kwargs["target_title"] == KROTO
