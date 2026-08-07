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

    def test_inline_bracket_citation_in_prose_stays_body(self) -> None:
        # A single-line body chunk with a mid-sentence bracketed citation
        # (not at line start) must not flip — regression guard for the
        # gr196447 fix that lowered the per-chunk match floor.
        chunk = (
            "This approach improves yield significantly, consistent with "
            "prior work [3] and later confirmed independently."
        )
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.BODY

    def test_single_marker_line_entry_is_references(self) -> None:
        # gr196447 bug 1: Marker's block splitter commonly emits ONE
        # bibliography entry per chunk (1-2 lines). The old absolute
        # floor (matches >= 3) made a real single-entry chunk like this
        # unreachable — matches=1, ratio=1.0, but 1 < 3.
        chunk = "- [15] Smith, J. A.; Doe, K. B. Journal of Materials 2015, 25, 115."
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.REFERENCES

    def test_two_line_entry_with_continuation_is_references(self) -> None:
        chunk = (
            "- [7] Johnson, A. & Lee, B. Cu-MOF synthesis and\n"
            "characterization for CO2 capture applications."
        )
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.REFERENCES

    def test_marker_shaped_bullet_list_in_body_not_over_matched(self) -> None:
        # An ordinary bracketed-footnote body bullet has the same "- [N]"
        # structural shape as a bibliography entry but no citation
        # content (no author/comma/initial). It must not flip — the
        # reason boilerplate's marker-citation pattern requires the
        # author-shaped content on top of the bare marker shape that
        # bib_parse's (looser, majority-vote-protected) detector uses.
        chunk = "- [1] See supporting information for experimental details."
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.BODY


class TestReferencesRealWorldShape:
    """The actual Marker/marker-splitter shape: a long body, a
    ``# References`` heading, then one bibliography entry PER CHUNK.
    The old single-blob fixture in this file (and in
    ``tests/ingest/test_pipeline.py``) masked both gr196447 bugs —
    these pin the real failure shape.
    """

    @staticmethod
    def _entry(n: int) -> str:
        # Surname must not have a digit glued onto it (Smith%d) — the
        # citation-shape regex only accepts letters/apostrophe/hyphen
        # in the surname, matching real bibliography formatting where
        # the entry number lives inside the leading "[N]" marker, not
        # appended to the author's name.
        return f"- [{n}] Smith, J. {n}.; Doe, K. Journal of Materials {2000 + n}, {n}, {n * 10}."

    def test_15_entry_bibliography_fully_retagged(self) -> None:
        body = [
            f"Paragraph {i} discusses synthesis, characterization, and "
            f"electrochemical performance of the candidate material in detail."
            for i in range(85)
        ]
        heading = ["# References"]
        entries = [self._entry(n) for n in range(1, 16)]
        chunks_text = body + heading + entries

        result = classify_chunks(chunks_text)

        heading_idx = len(body)
        assert result.classes[heading_idx] == ChunkClass.REFERENCES
        entry_classes = result.classes[heading_idx + 1 :]
        assert len(entry_classes) == 15
        assert all(c == ChunkClass.REFERENCES for c in entry_classes), (
            f"expected all 15 entries retagged, got {list(entry_classes)}"
        )
        # Body chunks (excluding the position-0 head liberal-accept)
        # must be untouched.
        assert all(c == ChunkClass.BODY for c in result.classes[1:85])

    def test_100_entry_bibliography_fully_retagged(self) -> None:
        # Pins the tail_cap fix independently of the per-chunk-line-count
        # fix: with the old tail_cap=8, only the last 8 of 100 entries
        # (plus the heading, unreached) would retag.
        body = [
            f"Paragraph {i} discusses synthesis, characterization, and "
            f"electrochemical performance of the candidate material in detail."
            for i in range(85)
        ]
        heading = ["# References"]
        entries = [self._entry(n) for n in range(1, 101)]
        chunks_text = body + heading + entries

        result = classify_chunks(chunks_text)

        heading_idx = len(body)
        assert result.classes[heading_idx] == ChunkClass.REFERENCES
        entry_classes = result.classes[heading_idx + 1 :]
        assert len(entry_classes) == 100
        assert all(c == ChunkClass.REFERENCES for c in entry_classes), (
            "expected all 100 entries retagged; "
            f"got {sum(1 for c in entry_classes if c == ChunkClass.REFERENCES)}/100"
        )
        assert all(c == ChunkClass.BODY for c in result.classes[1:85])


class TestLoosePatternsStayGatedByFloor:
    """Regression guard: the gr196447 fix that let short chunks qualify
    via density alone must NOT extend to the *loose* citation patterns
    (only ``_MARKER_CITATION_LINE_RE`` — marker shape AND author-comma-
    initial content — is safe on a short chunk). A reviewer caught that
    the first pass over-applied the relaxed floor: these loose patterns
    (``"[N] Capitalized..."``, ``"N. Capitalized, Capitalized..."``)
    fire on ordinary short structural lines that have nothing to do
    with citations — a heading, a cross-reference, a figure caption.
    Each of these must FAIL against a floorless implementation and
    PASS once loose patterns are re-gated behind ``matches >= 3``.
    """

    def test_bracket_heading_not_references(self) -> None:
        # "[1] Introduction" — a numbered section heading, not a citation.
        result = classify_chunks(["b1", "b2", "b3", "[1] Introduction"])
        assert result.classes[3] == ChunkClass.BODY

    def test_bracket_cross_reference_not_references(self) -> None:
        chunk = "[3] See Section 2 for details."
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.BODY

    def test_numbered_methods_line_not_references(self) -> None:
        chunk = "1. Materials, Synthesis conditions were as follows."
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.BODY

    def test_bracket_figure_caption_not_references(self) -> None:
        chunk = (
            "[12] Figure showing the XRD pattern of the sample after "
            "annealing at high temperature."
        )
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.BODY

    def test_trailing_numbered_notes_list_not_references(self) -> None:
        # The reviewer's real-shape scenario: a body of many paragraphs,
        # a "## Notes" heading, then a short numbered list that is NOT
        # a bibliography (methods/notes-style, "N. Word, Word ...").
        # None of the trailing lines — including the heading — must
        # flip to REFERENCES.
        body = [
            f"Paragraph {i} discussing synthesis and characterization "
            f"results in detail here."
            for i in range(20)
        ]
        notes_heading = ["## Notes"]
        trailing_notes = [
            "1. Materials, Synthesis conditions were as follows.",
            "2. Methods, Sample preparation followed standard procedures.",
            "3. Discussion, Results were consistent across trials.",
        ]
        chunks_text = body + notes_heading + trailing_notes

        result = classify_chunks(chunks_text)

        tail = result.classes[len(body) :]
        assert all(c == ChunkClass.BODY for c in tail), (
            f"trailing notes list must not flip to references, got {list(tail)}"
        )

    def test_strict_marker_pattern_still_qualifies_short_chunk(self) -> None:
        # Sanity check that the scoped fix didn't overcorrect: a real
        # single-entry Marker-shaped bibliography chunk (marker shape
        # AND author-comma-initial content) still qualifies.
        chunk = "- [15] Smith, J. A.; Doe, K. B. Journal of Materials 2015, 25, 115."
        result = classify_chunks(["b1", "b2", "b3", chunk])
        assert result.classes[3] == ChunkClass.REFERENCES

    def test_mixed_tail_only_true_reference_run_flips(self) -> None:
        # A numbered (non-citation) body list immediately precedes the
        # real, heading-marked bibliography. tail_cap=500 makes the walk
        # effectively unbounded, so this pins that only the contiguous
        # reference run at the very end flips — the walk correctly stops
        # at the numbered list once it no longer sees references/ack/
        # contact-shaped content.
        body = [
            f"Paragraph {i} discussing synthesis and characterization "
            f"results in detail here."
            for i in range(20)
        ]
        numbered_list = [
            "1. First step of the synthesis procedure.",
            "2. Second step, heating to 150 degrees.",
            "3. Third step, cooling and filtration.",
        ]
        heading = ["# References"]

        def entry(n: int) -> str:
            return f"- [{n}] Smith, J. {n}.; Doe, K. Journal of Materials {2000 + n}, {n}, {n * 10}."

        refs = [entry(n) for n in range(1, 11)]
        chunks_text = body + numbered_list + heading + refs

        result = classify_chunks(chunks_text)

        list_start = len(body)
        assert all(
            c == ChunkClass.BODY for c in result.classes[list_start : list_start + 3]
        ), "numbered pre-bibliography body list must not flip"
        heading_idx = list_start + 3
        assert result.classes[heading_idx] == ChunkClass.REFERENCES
        assert all(
            c == ChunkClass.REFERENCES for c in result.classes[heading_idx + 1 :]
        )


class TestReferencesFollowedByAppendix:
    """gr196690: body → references → appendix/SI layouts. The tail
    walk starts at the last chunk and stops at the first non-matching
    one, so a non-citation-shaped appendix/SI chunk after the
    bibliography blocks it from ever reaching the real references
    section above. The heading-anchored forward pass handles this
    independently of the tail walk.
    """

    def test_references_before_appendix_are_retagged(self) -> None:
        # The appendix chunk deliberately has no citation shape and no
        # References/Bibliography heading, so it must stay BODY while
        # the heading + the two real entries above it flip.
        appendix = (
            "## Supporting Information\nAdditional experimental details, "
            "extended figures, and raw data tables are provided below for "
            "completeness and reproducibility."
        )
        entry1 = "- [1] Smith, J. A.; Doe, K. B. Journal of Materials 2015, 25, 115."
        entry2 = "- [2] Johnson, A.; Lee, B. Chem. Soc. Rev. 2016, 12, 200."
        # Position 0 is liberally accepted as HEAD by _is_head_chunk
        # (short title-page heuristic); use a real title-page chunk
        # there and put substantive body content in positions 1-2 so
        # the BODY assertions below are meaningful.
        chunks_text = [
            "Title page",
            "Body 1 with substantive prose discussing the study in detail.",
            "Body 2 continuing the discussion with more content here.",
            "# References",
            entry1,
            entry2,
            appendix,
        ]

        result = classify_chunks(chunks_text)

        assert result.classes[0] == ChunkClass.HEAD
        assert result.classes[1] == ChunkClass.BODY
        assert result.classes[2] == ChunkClass.BODY
        assert result.classes[3] == ChunkClass.REFERENCES  # heading
        assert result.classes[4] == ChunkClass.REFERENCES  # entry 1
        assert result.classes[5] == ChunkClass.REFERENCES  # entry 2
        assert result.classes[6] == ChunkClass.BODY  # appendix, untouched

    def test_references_at_tail_still_classified_no_appendix(self) -> None:
        # Idempotence: when references genuinely are the document tail
        # (no appendix after them), the pre-existing tail walk already
        # tags them REFERENCES, so the new anchor pass must be a no-op
        # here — not double-apply or otherwise change the result.
        entry1 = "- [1] Smith, J. A.; Doe, K. B. Journal of Materials 2015, 25, 115."
        entry2 = "- [2] Johnson, A.; Lee, B. Chem. Soc. Rev. 2016, 12, 200."
        chunks_text = [
            "Title page",
            "Body 1 with substantive prose discussing the study in detail.",
            "Body 2 continuing the discussion with more content here.",
            "# References",
            entry1,
            entry2,
        ]

        result = classify_chunks(chunks_text)

        assert result.classes[0] == ChunkClass.HEAD
        assert result.classes[1] == ChunkClass.BODY
        assert result.classes[2] == ChunkClass.BODY
        assert result.classes[3] == ChunkClass.REFERENCES
        assert result.classes[4] == ChunkClass.REFERENCES
        assert result.classes[5] == ChunkClass.REFERENCES

    def test_toc_heading_before_real_bibliography_does_not_block_it(self) -> None:
        # A review/thesis table-of-contents line ("References ..... 45")
        # matches _REFERENCES_HEADING_RE just like the real heading, but
        # has no citation-shaped entries after it. The anchor pass must
        # NOT commit on that false match and stop — it must skip it and
        # keep scanning to the true bibliography further down, else the
        # gr196690 fix is defeated for exactly this class of paper.
        entry1 = "- [1] Smith, J. A.; Doe, K. B. Journal of Materials 2015, 25, 115."
        entry2 = "- [2] Johnson, A.; Lee, B. Chem. Soc. Rev. 2016, 12, 200."
        appendix = (
            "## Supporting Information\nAdditional experimental details and "
            "extended figures are provided below for completeness."
        )
        chunks_text = [
            "Title page",
            "References ..... 45",  # ToC/outline line — a FALSE anchor
            "Body prose discussing the study in substantive detail here.",
            "More body content continuing the discussion at length here.",
            "# References",  # the REAL bibliography heading
            entry1,
            entry2,
            appendix,
        ]

        result = classify_chunks(chunks_text)

        # The ToC line stays BODY; the real heading + entries flip; the
        # appendix stays BODY.
        assert result.classes[1] == ChunkClass.BODY  # ToC line, not tagged
        assert result.classes[2] == ChunkClass.BODY
        assert result.classes[3] == ChunkClass.BODY
        assert result.classes[4] == ChunkClass.REFERENCES  # real heading
        assert result.classes[5] == ChunkClass.REFERENCES  # entry 1
        assert result.classes[6] == ChunkClass.REFERENCES  # entry 2
        assert result.classes[7] == ChunkClass.BODY  # appendix

    def test_numbered_notes_list_without_heading_not_mistaken_for_references(
        self,
    ) -> None:
        # Guard: a numbered Methods/Notes list with NO References/
        # Bibliography heading anywhere must not be picked up by the
        # new anchor pass (which requires the heading, not just
        # citation density) even when it's followed by other content.
        body = [
            f"Paragraph {i} discussing synthesis and characterization "
            f"results in detail here."
            for i in range(10)
        ]
        notes = [
            "1. Materials, Synthesis conditions were as follows.",
            "2. Methods, Sample preparation followed standard procedures.",
            "3. Discussion, Results were consistent across trials.",
        ]
        trailing_body = [
            "Further discussion continues here with additional prose content."
        ]
        chunks_text = body + notes + trailing_body

        result = classify_chunks(chunks_text)

        assert all(c == ChunkClass.BODY for c in result.classes[1:])


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
