"""Multi-article page spreads keep only the target article (gr463414).

The block shapes mirror real Marker output from prod: pa478893 (Kroto 1987, a
catalysis letter on each side) and pa42559 / pa44468 (four Nature 1998
correspondence pieces on one spread, bylines at the end).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from precis.ingest.add import (
    ARTICLE_SPREAD_SOURCE,
    MULTI_ARTICLE_TAG,
    IngestResult,
    PdfInput,
    precis_add,
)
from precis.ingest.article_spread import (
    article_start_ord,
    leading_foreign_ord,
    mark_article_start,
    trim_to_article,
)
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.ingest.pdf_metadata import PdfMetadata
from precis.ingest.pipeline import extract_paper
from precis.store.types import ChunkInsert

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


# ---- foreign-prefix marker (gr515439) ----------------------------------
#
# Journal PDFs sharing a page with the previous article ingest its tail as
# leading body chunks. Fixtures mirror the three prod cases: pa39683
# (Rumelhart 1986 — chunks 0-7 are the Engel & Macko 14C tail), pa35223
# (Pfleiderer MnSi tail + refs + acks before chunk 9) and pa35334 (Kipp
# refs + acks before chunk 3).

RUMELHART = "Learning representations by back-propagating errors"
PACOMM = "Programmable autonomous computing machines from coupled oscillators"
AQUA = "Water conduction through the hydrophobic channel of aquaporin-1"
FOREIGN_14C = (
    "The 14C ages of groundwater samples from the confined carbonate "
    "aquifer were interpreted with a geochemical mass-balance model, and "
    "carbon isotope exchange between water and minerals was estimated "
    "from the dolomite and calcite content of the rock."
)
FOREIGN_MNSI = (
    "The helimagnetic transition in MnSi is driven by the Dzyaloshinskii-"
    "Moriya interaction; the measured susceptibility and specific heat "
    "anomalies agree with a weak first-order transition at ambient "
    "pressure near 29.5 K."
)
OWN_PROSE = (
    "We describe a new learning procedure for networks of neuron-like "
    "units and demonstrate that it learns internal representations that "
    "allow the network to solve tasks which simple associative "
    "generalisation cannot capture."
)


def _c(ord_: int, text: str, kind: str = "paragraph") -> Any:
    return SimpleNamespace(ord=ord_, text=text, chunk_kind=kind)


def _rumelhart_prefix_chunks() -> list[Any]:
    """pa39683: chunks 0-7 are the 14C paper's tail; own title at 8."""
    return [
        _c(0, FOREIGN_14C),
        _c(1, "The model reconciles the apparent age differences. " + FOREIGN_14C),
        _c(2, "We thank the referees for constructive comments."),
        _c(3, "Received 19 May; accepted 15 July 1986."),
        _c(
            4, "- 1. Engel, M. & Macko, S. A. Nature 317, 350-352 (1985).", "references"
        ),
        _c(5, "- 2. Mook, W. G. Chem. Geol. 25, 155-176 (1979).", "references"),
        _c(6, "- 3. Pearson, F. J. Water Resour. Res. 1, 394 (1965).", "references"),
        _c(7, "Correspondence and requests for materials should be addressed to M.E."),
        _c(
            8,
            f"{RUMELHART}\nDavid E. Rumelhart, Geoffrey E. Hinton & Ronald J. Williams\n{OWN_PROSE}",
        ),
        _c(
            9, "The aim is to achieve a state in which the output matches. " + OWN_PROSE
        ),
    ]


def _mnsi_prefix_chunks() -> list[Any]:
    """pa35223: chunks 0-8 are the MnSi paper's tail; own title at 9."""
    return [
        _c(0, FOREIGN_MNSI),
        _c(1, "Below the transition the helix reorients. " + FOREIGN_MNSI),
        _c(2, "Acknowledgements. We thank P. Boni for the neutron data."),
        _c(3, "- 1. Pfleiderer, C. et al. Nature 427, 227-231 (2004).", "references"),
        _c(4, "- 2. Bak, P. & Jensen, M. H. J. Phys. C 13, 881 (1980).", "references"),
        _c(
            5,
            "- 3. Nakanishi, O. et al. Solid State Commun. 35, 995 (1980).",
            "references",
        ),
        _c(
            6,
            "- 4. Ishikawa, Y. et al. Solid State Commun. 19, 525 (1976).",
            "references",
        ),
        _c(7, "- 5. Landau, L. D. Phys. Z. Sowjetunion 11, 26 (1937).", "references"),
        _c(8, "Correspondence should be addressed to C.P."),
        _c(
            9,
            f"{PACOMM}\nWe present a computing machine built from coupled "
            f"nonlinear oscillators. {OWN_PROSE}",
        ),
        _c(10, "The phase dynamics encode the solution. " + OWN_PROSE),
    ]


def _kipp_prefix_chunks() -> list[Any]:
    """pa35334: chunks 0-2 are the Kipp paper's refs + acks; own title at 3."""
    return [
        _c(
            0, "- 12. Kipp, T. et al. Phys. Rev. Lett. 96, 077403 (2006).", "references"
        ),
        _c(1, "- 13. Walck, S. N. et al. Phys. Rev. B 56, 9721 (1997).", "references"),
        _c(2, "We are grateful to the EPSRC for support."),
        _c(3, f"{AQUA}\nSingle-file water transport through narrow pores. {OWN_PROSE}"),
        _c(4, "Molecular dynamics simulations predict high rates. " + OWN_PROSE),
    ]


class TestLeadingForeignOrd:
    def test_rumelhart_prefix_marks_ord_8(self) -> None:
        chunks = _rumelhart_prefix_chunks()
        assert leading_foreign_ord(chunks, title=RUMELHART) == 8

    def test_mnsi_prefix_marks_ord_9(self) -> None:
        chunks = _mnsi_prefix_chunks()
        assert leading_foreign_ord(chunks, title=PACOMM) == 9

    def test_kipp_prefix_marks_ord_3(self) -> None:
        chunks = _kipp_prefix_chunks()
        assert leading_foreign_ord(chunks, title=AQUA) == 3

    def test_doi_candidate_without_title(self) -> None:
        """A title-less ref can still mark from a front-matter DOI line."""
        chunks = [
            _c(0, FOREIGN_14C),
            _c(1, "- 1. Engel, M. Nature 317, 350 (1985).", "references"),
            _c(4, "doi:10.1038/323533a0\n" + OWN_PROSE),
        ]
        assert leading_foreign_ord(chunks, doi="10.1038/323533a0") == 4

    def test_normal_paper_marks_nothing(self) -> None:
        """Negative: chunk 0 is the paper's own title — no mark ever."""
        chunks = [
            _c(0, f"{KROTO}\nKroto, H. W.\n{PROSE}"),
            _c(1, "The fullerene cage is stable. " + PROSE),
            _c(2, "References"),
            _c(3, "- 1. Smith, J. Nature 1, 2 (1985).", "references"),
        ]
        assert leading_foreign_ord(chunks, title=KROTO) is None

    def test_candidate_without_hard_closer_marks_nothing(self) -> None:
        """A title-matching chunk with no closer before it is not a
        boundary — the prefix must carry the previous article's tail."""
        chunks = [
            _c(0, "Some unrelated front matter. " + FOREIGN_14C),
            _c(1, "More front matter without a closer. " + FOREIGN_14C),
            _c(2, f"{RUMELHART}\n{OWN_PROSE}"),
        ]
        assert leading_foreign_ord(chunks, title=RUMELHART) is None

    def test_own_article_then_refs_marks_nothing(self) -> None:
        """A post-references appendix re-mentioning the title is not a
        boundary: the prefix scores on the title (it's the own body)."""
        chunks = [
            _c(0, f"{RUMELHART}\n{OWN_PROSE}"),
            _c(1, "The delta rule generalises. " + OWN_PROSE),
            _c(2, "- 1. Rosenblatt, F. Psychol. Rev. 65, 386 (1958).", "references"),
            _c(3, f"Appendix: proof of convergence for {RUMELHART}. " + OWN_PROSE),
        ]
        assert leading_foreign_ord(chunks, title=RUMELHART) is None

    def test_no_identity_no_mark(self) -> None:
        chunks = _rumelhart_prefix_chunks()
        assert leading_foreign_ord(chunks) is None
        assert leading_foreign_ord(chunks, title="untitled") is None

    def test_doi_candidate_with_soft_closer_marks_nothing(self) -> None:
        """A DOI-only candidate needs a *references* chunk before it — a
        Received/acknowledgement/Correspondence line alone can be the
        paper's own front matter ahead of its DOI line, so it doesn't
        prove a foreign tail."""
        chunks = [
            _c(0, "Received 19 May; accepted 15 July 1986. " + FOREIGN_14C),
            _c(
                1,
                "doi:10.1038/323533a0\n"
                "A somewhat related but distinct title\n" + OWN_PROSE,
            ),
        ]
        assert (
            leading_foreign_ord(
                chunks, title="A totally different paper", doi="10.1038/323533a0"
            )
            is None
        )

    def test_doi_candidate_with_references_closer_still_marks(self) -> None:
        """The DOI-only rule tightens the closer, it doesn't remove it:
        a real references chunk ahead of the DOI line still marks."""
        chunks = [
            _c(0, "Received 19 May; accepted 15 July 1986. " + FOREIGN_14C),
            _c(1, "- 1. Engel, M. Nature 317, 350 (1985).", "references"),
            _c(4, "doi:10.1038/323533a0\n" + OWN_PROSE),
        ]
        assert leading_foreign_ord(chunks, doi="10.1038/323533a0") == 4


class TestMarkArticleStart:
    def test_marks_meta_and_journals(self, store) -> None:
        ref = store.insert_ref(kind="paper", slug="pa39683", title=RUMELHART)
        for c in _rumelhart_prefix_chunks():
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(
                        ord=c.ord, text=c.text, meta={"chunk_kind": c.chunk_kind}
                    )
                ],
            )

        assert mark_article_start(store, ref.id) == 8
        assert article_start_ord(store, ref.id) == 8
        meta = store.fetch_refs_by_ids([ref.id])[ref.id].meta
        assert meta["article_start_ord"] == 8

        # The readers' window shifts: chunk-0 text is the paper's own.
        from precis.ingest.metadata_resolve import _chunk0_text

        assert _chunk0_text(store, ref.id).startswith(RUMELHART)

        with store.pool.connection() as conn:
            events = conn.execute(
                "SELECT event, payload FROM ref_events "
                "WHERE ref_id = %s AND source = 'ingest:spread' "
                "AND event = 'foreign_prefix'",
                (ref.id,),
            ).fetchall()
        assert len(events) == 1
        assert events[0][1]["article_start_ord"] == 8
        assert events[0][1]["foreign_ords"] == list(range(8))

    def test_normal_paper_unmarked(self, store) -> None:
        ref = store.insert_ref(kind="paper", slug="norm", title=KROTO)
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text=f"{KROTO}\n{PROSE}")]
        )
        assert mark_article_start(store, ref.id) is None
        assert article_start_ord(store, ref.id) == 0
        meta = store.fetch_refs_by_ids([ref.id])[ref.id].meta
        assert "article_start_ord" not in meta

    def _seed_prefix(self, store: Any, conn: Any, slug: str) -> int:
        """Insert a ref + the rumelhart prefix chunks on ``conn`` so the
        whole fixture is one (possibly uncommitted) transaction."""
        rid = int(
            store.insert_ref(kind="paper", slug=slug, title=RUMELHART, conn=conn).id
        )
        store.chunks.insert_chunks(
            rid,
            [
                ChunkInsert(ord=c.ord, text=c.text, meta={"chunk_kind": c.chunk_kind})
                for c in _rumelhart_prefix_chunks()
            ],
            conn=conn,
        )
        return rid

    def test_mark_inside_uncommitted_tx(self, store) -> None:
        """At ingest the ref (and its identifiers) are uncommitted on the
        ingest conn — reads must run on that conn or the mark silently
        skips a fresh-ingest ref."""
        with store.tx() as conn:
            rid = self._seed_prefix(store, conn, "uncom")
            assert mark_article_start(store, rid, conn=conn) == 8
        assert article_start_ord(store, rid) == 8

    def test_same_ord_mark_writes_nothing(self, store) -> None:
        """Re-deriving the same boundary is a no-op — no rewrite and no
        duplicate foreign_prefix event on every repair pass."""
        ref = store.insert_ref(kind="paper", slug="pa39683b", title=RUMELHART)
        for c in _rumelhart_prefix_chunks():
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(
                        ord=c.ord, text=c.text, meta={"chunk_kind": c.chunk_kind}
                    )
                ],
            )

        assert mark_article_start(store, ref.id) == 8
        assert mark_article_start(store, ref.id) == 8
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM ref_events WHERE ref_id = %s "
                "AND event = 'foreign_prefix'",
                (ref.id,),
            ).fetchone()[0]
        assert n == 1

    def test_moved_boundary_overwrites(self, store) -> None:
        """A different computed ord replaces the marker and journals the
        new boundary (``was`` records the superseded one)."""
        ref = store.insert_ref(kind="paper", slug="pa39683c", title=RUMELHART)
        for c in _rumelhart_prefix_chunks():
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(
                        ord=c.ord, text=c.text, meta={"chunk_kind": c.chunk_kind}
                    )
                ],
            )
        assert mark_article_start(store, ref.id) == 8

        # Re-ingest dropped the leading tail; the boundary now sits at 4.
        shifted = [
            _c(0, FOREIGN_14C),
            _c(1, "We thank the referees for constructive comments."),
            _c(2, "- 1. Engel, M. Nature 317, 350 (1985).", "references"),
            _c(4, f"{RUMELHART}\n{OWN_PROSE}"),
            _c(5, "The aim is to achieve a state. " + OWN_PROSE),
        ]
        with store.tx() as conn:
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(
                        ord=c.ord,
                        text=c.text,
                        meta={"chunk_kind": c.chunk_kind},
                    )
                    for c in shifted
                ],
                replace=True,
                conn=conn,
            )
            assert mark_article_start(store, ref.id, conn=conn) == 4
        assert article_start_ord(store, ref.id) == 4
        with store.pool.connection() as conn:
            payloads = conn.execute(
                "SELECT payload FROM ref_events WHERE ref_id = %s "
                "AND event = 'foreign_prefix' ORDER BY event_id",
                (ref.id,),
            ).fetchall()
        assert len(payloads) == 2
        assert payloads[1][0]["was"] == 8

    def test_stale_marker_is_dropped(self, store) -> None:
        """The boundary that justified the mark is gone (re-ingested
        front matter) — the marker must drop, else readers keep
        skipping ords that are now the paper's own."""
        ref = store.insert_ref(kind="paper", slug="pa39683d", title=RUMELHART)
        for c in _rumelhart_prefix_chunks():
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(
                        ord=c.ord, text=c.text, meta={"chunk_kind": c.chunk_kind}
                    )
                ],
            )
        assert mark_article_start(store, ref.id) == 8

        with store.tx() as conn:
            store.chunks.insert_chunks(
                ref.id,
                [
                    ChunkInsert(ord=0, text=f"{RUMELHART}\n{OWN_PROSE}"),
                    ChunkInsert(ord=1, text="More of the paper. " + OWN_PROSE),
                ],
                replace=True,
                conn=conn,
            )
            assert mark_article_start(store, ref.id, conn=conn) is None
        assert article_start_ord(store, ref.id) == 0
        meta = store.fetch_refs_by_ids([ref.id])[ref.id].meta
        assert "article_start_ord" not in meta
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM ref_events WHERE ref_id = %s "
                "AND event = 'foreign_prefix_cleared'",
                (ref.id,),
            ).fetchone()
        assert row is not None and row[0]["was"] == 8
