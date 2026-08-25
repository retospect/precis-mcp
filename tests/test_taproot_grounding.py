"""Evidence-grounding admissibility of a chunk
(:mod:`precis.taproot.grounding`).

Pure — no DB, no LLM. The fixtures are real shapes lifted from the corpus:
what separates a paper's title/author front matter from its abstract is the
whole job, and both routinely sit at ``ord`` 0-2.
"""

from __future__ import annotations

from precis.taproot.grounding import has_grounding_prose

_FRONT_MATTER = """**Printed Touch Sensors Using Carbon NanoBud Material**

*Anton S. Anisimov, David P. Brown, Bjorn F. Mikladal, Kunjal Parikh,
Erkki Soininen, Martti Sonninen, Dewei Tian, Ilkka Varjos*

> **Canatu Oy, Helsinki, Finland, Intel Corporation, Santa Clara, USA**"""


def test_prose_gate_rejects_title_author_front_matter() -> None:
    assert not has_grounding_prose(_FRONT_MATTER)


def test_prose_gate_rejects_a_title_that_is_a_full_sentence() -> None:
    # The claim-shaped title is the trap: it asserts, and it lexically matches
    # the citing span better than any body passage. A title carries no
    # terminator, so it is not a sentence and never grounds.
    assert not has_grounding_prose(
        "Glymphatic dysfunction evidenced by DTI-ALPS is related to obstructive "
        "sleep apnea intensity in newly diagnosed Parkinson's disease\n\n"
        "Jiri Nepozitek, Stanislav Marecek, Veronika Rottova, Petr Dusek"
    )


def test_prose_gate_rejects_correspondence_footnote_under_front_matter() -> None:
    # The one terminated run in a front-matter block is the correspondence
    # note — too short, and the block above it is a name list.
    assert not has_grounding_prose(
        "A Novel Hybrid Carbon Material\n\n"
        "Albert G. Nasibulin, Peter V. Pikhitsa, Esko I. Kauppinen\n\n"
        "*To whom correspondence should be addressed."
    )


def test_prose_gate_rejects_single_line_front_matter() -> None:
    # No blank line to split the title off the footnote, so the whole block is
    # one "sentence" — the capitalised-share backstop is what rejects it.
    assert not has_grounding_prose(
        "A Novel Hybrid Carbon Material Albert G. Nasibulin, Peter V. Pikhitsa, "
        "Hua Jiang, David P. Brown, Esko I. Kauppinen. "
        "To whom correspondence should be addressed."
    )


def test_prose_gate_accepts_acronym_dense_body_prose() -> None:
    # The backstop must not swallow real prose in a biomedical corpus.
    assert has_grounding_prose(
        "CRISPR-Cas9 targeting of BRCA1 and TP53 in HeLa cells increased "
        "apoptosis significantly."
    )


def test_prose_gate_accepts_an_abstract() -> None:
    assert has_grounding_prose(
        "> **Abstract:** Synthesis, properties, structural peculiarities, and "
        "applications of nanobuds and related nanostructures are discussed."
    )


def test_prose_gate_accepts_front_matter_followed_by_abstract_prose() -> None:
    # The Dunlap-1992 shape: journal header + title + author + affiliation, then
    # the abstract in the SAME chunk. One assertion is enough to ground.
    assert has_grounding_prose(
        "15 JULY 1992-I\n\nConnecting carbon tubules\n\nB. I. Dunlap\n\n"
        "Naval Research Laboratory, Washington, D.C. 20375-5000\n\n"
        "Two possible joints between different types of carbon tubules are "
        "discussed."
    )


def test_prose_gate_accepts_a_numeric_table() -> None:
    # A table asserts through its cells and is legitimate evidence for a
    # numeric claim, so it escapes the sentence test.
    assert has_grounding_prose(
        "| Structure | Binding energy (eV) | Band gap (eV) |\n"
        "|-----------|--------------------|---------------|\n"
        "| I PGNB    | -3.34              | 0.31          |\n"
        "| II PGNB   | -3.78              | 0.12          |"
    )


def test_prose_gate_ignores_abbreviation_and_initial_terminators() -> None:
    # "Vol." / "No." / "B. I." are not sentence ends: a header line that splits
    # into short runs must not add up to an assertion.
    assert not has_grounding_prose("NANO LETTERS 2009 Vol. 9, No. 1 250-256")


def test_prose_gate_rejoins_a_sentence_split_by_an_initial() -> None:
    # "B." and "I." are initials, so the ONE assertion here spans them. Split
    # at each, every run falls under the word floor and the chunk reads as
    # front matter — so this pins the re-join, not just the skip.
    assert has_grounding_prose(
        "Reported first by B. I. Dunlap, joints introduce pentagon-heptagon pairs here."
    )


def test_prose_gate_rejects_a_sentence_too_short_to_assert() -> None:
    assert not has_grounding_prose("Results are shown.")
