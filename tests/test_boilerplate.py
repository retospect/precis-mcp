"""Boilerplate classifier contract tests.

Pins the heuristics — when they fire, when they don't, and what
gets labelled. Lightweight (no model, no network).
"""

from __future__ import annotations

from precis.utils.boilerplate import ChunkClass, classify_chunks

# ── trivial / boundary cases ────────────────────────────────────────


class TestTrivial:
    def test_empty(self) -> None:
        result = classify_chunks([])
        assert result.classes == ()
        assert result.body_indices == ()

    def test_single_chunk_is_body(self) -> None:
        result = classify_chunks(["some content here that's substantive"])
        assert result.classes == (ChunkClass.BODY,)
        assert result.body_indices == (0,)

    def test_two_chunks_both_body(self) -> None:
        result = classify_chunks(["body 1", "body 2"])
        assert result.classes == (ChunkClass.BODY, ChunkClass.BODY)


# ── head detection ──────────────────────────────────────────────────


class TestHead:
    def test_position_0_short_is_head(self) -> None:
        result = classify_chunks(
            [
                "Journal of Awesome Things\nVOLUME 23",  # short tit page
                "Body content begins here in chunk 1 with real prose...",
                "More body in chunk 2 with substantive content here.",
                "Even more body in chunk 3.",
            ]
        )
        assert result.classes[0] == ChunkClass.HEAD
        assert result.classes[1] == ChunkClass.BODY

    def test_abstract_heading_in_chunk_1(self) -> None:
        result = classify_chunks(
            [
                "Title page",
                "## Abstract\nThis paper studies the behaviour of foo under bar...",
                "## 1. Introduction\nWe present a study of...",
                "Body chunk 3.",
            ]
        )
        assert result.classes[1] == ChunkClass.HEAD
        assert result.classes[2] == ChunkClass.BODY

    def test_orcid_dense_chunk_is_head(self) -> None:
        result = classify_chunks(
            [
                "Title",
                "Author A: 0000-0001-2345-6789\nAuthor B: 0000-0002-3456-7890\nAuthor C: 0000-0003-4567-8901",
                "Real body content with substantial prose here.",
                "More body.",
            ]
        )
        assert result.classes[1] == ChunkClass.HEAD

    def test_head_walk_stops_at_first_body(self) -> None:
        result = classify_chunks(
            [
                "Title",  # HEAD
                "## Abstract\n…",  # HEAD
                "## Introduction\nWe study foo. The motivation is...",  # BODY
                "## Methods\nWe used Y to measure X over the entire...",  # BODY (not HEAD even though "method" sounds structural)
            ]
        )
        assert result.classes == (
            ChunkClass.HEAD,
            ChunkClass.HEAD,
            ChunkClass.BODY,
            ChunkClass.BODY,
        )


# ── references detection ────────────────────────────────────────────


class TestReferences:
    def test_references_heading(self) -> None:
        result = classify_chunks(
            [
                "Body 1",
                "Body 2",
                "Body 3",
                "## References\n(1) Smith, J. Science. 2020.",
            ]
        )
        assert result.classes[3] == ChunkClass.REFERENCES

    def test_citation_density(self) -> None:
        chunk = (
            "(1) Wang, J.-L.; Wang, Ch.; Lin, W. Metal-Organic Frameworks. ACS Catal. 2012, 2.\n"
            "(2) Wang, Ch.-Ch.; Du, X.-D.; Li, J. Photocatalytic Cr(VI). Appl. Catal. 2015.\n"
            "(3) Dias, E. M.; Petit, C. Towards the Use of MOFs. J. Mater. Chem. 2015.\n"
            "(4) Bobbitt, N. S.; Mendonca, M. Metal-organic frameworks. Chem. Soc. Rev. 2017."
        )
        result = classify_chunks(["Body 1", "Body 2", "Body 3", chunk])
        assert result.classes[3] == ChunkClass.REFERENCES

    def test_doi_dense_chunk_is_references(self) -> None:
        chunk = (
            "Wang et al. https://doi.org/10.1038/nature10352 and other refs.\n"
            "Smith et al. doi: 10.1126/science.1112286 confirmed.\n"
            "Brown et al. 10.1021/jacs.5b00123 observed similar."
        )
        result = classify_chunks(["Body 1", "Body 2", "Body 3", chunk])
        assert result.classes[3] == ChunkClass.REFERENCES

    def test_low_citation_density_stays_body(self) -> None:
        # A normal paragraph with one citation isn't a reference list.
        chunk = (
            "We followed the protocol from Smith et al. (2020) and obtained "
            "results consistent with prior work. The mechanism is well-understood."
        )
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.BODY


# ── acknowledgements detection ──────────────────────────────────────


class TestAcknowledgements:
    def test_acknowledgements_heading(self) -> None:
        result = classify_chunks(
            [
                "Body 1",
                "Body 2",
                "Body 3",
                "## Acknowledgements\nThe authors thank XYZ for support.",
            ]
        )
        assert result.classes[3] == ChunkClass.ACKNOWLEDGEMENTS

    def test_funding_heading(self) -> None:
        result = classify_chunks(
            [
                "Body 1",
                "Body 2",
                "Body 3",
                "## Funding\nThis work was supported by NSF grant 12345.",
            ]
        )
        assert result.classes[3] == ChunkClass.ACKNOWLEDGEMENTS

    def test_author_contributions_heading(self) -> None:
        result = classify_chunks(
            [
                "Body 1",
                "Body 2",
                "Body 3",
                "Author contributions: ABC designed the study, DEF analyzed.",
            ]
        )
        assert result.classes[3] == ChunkClass.ACKNOWLEDGEMENTS


# ── contact detection ───────────────────────────────────────────────


class TestContact:
    def test_corresponding_author_heading(self) -> None:
        result = classify_chunks(
            [
                "Body 1",
                "Body 2",
                "Body 3",
                "Corresponding author: Jane Doe (jane.doe@university.edu)",
            ]
        )
        assert result.classes[3] == ChunkClass.CONTACT

    def test_short_tail_with_email_is_contact(self) -> None:
        result = classify_chunks(
            [
                "Body 1",
                "Body 2",
                "Body 3",
                "J. Doe, j.doe@uni.edu",
            ]
        )
        assert result.classes[3] == ChunkClass.CONTACT


# ── body indices invariant ──────────────────────────────────────────


def test_body_indices_matches_body_classes() -> None:
    result = classify_chunks(
        [
            "Title page",
            "## Abstract\n…",
            "## Intro\nBody text starts here with real prose content.",
            "## Methods\nMore body with substantial discussion.",
            "## Results\nMore body content.",
            "## References\n(1) Foo (2020). (2) Bar (2021). (3) Baz (2022).",
        ]
    )
    expected_body = tuple(
        i for i, c in enumerate(result.classes) if c == ChunkClass.BODY
    )
    assert result.body_indices == expected_body
    assert len(expected_body) == 3  # chunks 2, 3, 4


# ── tail walk respects head guard ───────────────────────────────────


def test_tail_walk_doesnt_cross_head(self_unused=None) -> None:
    # Tiny paper where the head walk consumed everything: tail walk
    # must not relabel HEAD chunks.
    result = classify_chunks(
        [
            "Title\nVOL 23",
            "## Abstract\n…",
            "Author contributions: tiny chunk",  # would match ACK but is adjacent to HEAD
        ]
    )
    # Chunk 0 and 1 are HEAD; chunk 2 might be ACK or BODY but must
    # not break the structural invariant.
    assert result.classes[0] == ChunkClass.HEAD
    assert result.classes[1] == ChunkClass.HEAD
    assert result.classes[2] in (ChunkClass.BODY, ChunkClass.ACKNOWLEDGEMENTS)
