"""``precis.taproot.review_like.is_review_like`` — the conservative
title/journal review heuristic behind the hub-cite fallback
(``taproot/cite.py::hub_cite_keys``). Pure, no DB."""

from __future__ import annotations

import pytest

from precis.taproot.review_like import (
    PATTERNS_VERSION,
    is_review_like,
    proceedings_venue,
    review_reason,
    venue_vetoed,
)


@pytest.mark.parametrize(
    "journal",
    [
        "Chemical Reviews",
        "Chem. Soc. Rev.",
        "Chemical Society Reviews",
        "Nature Reviews Materials",
        "Nat. Rev. Chem.",
        "Annual Review of Physical Chemistry",
        "Annu. Rev. Mater. Res.",
        "Reviews in Mineralogy and Geochemistry",
        "Applied Physics Reviews",
        "Reviews of Modern Physics",
        "Progress in Materials Science",
        "Reports on Progress in Physics",
        "Physics Reports",
        "Trends in Analytical Chemistry",
        "TrAC Trends in Analytical Chemistry",
        "Materials Science and Engineering: R: Reports",
    ],
)
def test_review_venues_are_review_like(journal: str) -> None:
    assert is_review_like("Some neutral research title", journal) is True


@pytest.mark.parametrize(
    "journal",
    [
        "Physical Review Letters",
        "Physical Review B",
        "Phys. Rev. Applied",
        "Review of Scientific Instruments",
        "Progress in Photovoltaics: Research and Applications",
        "Journal of the American Chemical Society",
        "Nano Letters",
        "Advanced Materials",
        None,
        "",
    ],
)
def test_research_venues_are_not_review_like(journal: str | None) -> None:
    assert is_review_like("Some neutral research title", journal) is False


@pytest.mark.parametrize(
    "title",
    [
        "Carbon nanobuds: a review",
        "A systematic review and meta-analysis of catalyst deactivation",
        "Reviews of nanocarbon hybrids",
        "An overview of fullerene functionalisation",
        "Progress in perovskite photovoltaics",
        "Recent advances in single-walled nanotube sorting",
        "Advances in graphene synthesis",
        "Recent developments in carbon nanomaterials",
        "Perspective: Nanocarbon electronics",
        "Carbon electronics: a perspective",
        "Perspectives on cold atom sensing",
        "A survey of density functional methods",
        "The state of the art in nanotube growth",
        "The 2020 materials roadmap",
        "A tutorial on tight-binding models",
        "Recent advancements in carbon nanotube electrodes",
        "MOFs in food biotechnology: Opportunities, challenges, and future perspectives",
        "Nanomaterials for sensing: challenges and opportunities",
    ],
)
def test_review_phrasing_in_title(title: str) -> None:
    assert is_review_like(title, None) is True


@pytest.mark.parametrize(
    "title",
    [
        # review-ish substrings that are NOT review phrasing
        "Peer-reviewed benchmarks for nanotube thermal conductivity",
        "A peer-review process for computational datasets",
        "Reviewer bias in materials journals",
        "Reviewing the reviewed: notes",
        "From the perspective of the substrate, graphene wets poorly",
        "State-of-the-art nanotube transistors reach 10 GHz",
        "Synthesis of nanobuds by CO disproportionation",
        "Field-effect transistors from nanobud networks",
        "",
    ],
)
def test_non_review_titles(title: str) -> None:
    assert is_review_like(title, "Nano Letters") is False


def test_none_inputs_are_no_signal() -> None:
    assert is_review_like(None, None) is False


def test_primary_venue_does_not_hide_a_review_title() -> None:
    # The venue exception only vetoes the *journal* rule; the title still counts.
    assert is_review_like("A review of Raman spectra", "Physical Review B") is True


def test_review_reason_names_the_pattern() -> None:
    assert review_reason("Anything", "Chemical Reviews") == "journal ~ 'Reviews'"
    assert review_reason("Carbon nanobuds: a review", None) == "title ~ 'review'"
    assert review_reason("Peer-reviewed benchmarks", "Nano Letters") is None
    # venue rule wins over the title rule when both fire
    assert review_reason("A review of X", "Nature Reviews Chemistry") == (
        "journal ~ 'Reviews'"
    )


# ── proceedings_venue: the shared venue test (cite fallback + report gate) ──

_NOT_VENUES = [
    "Proceedings of the National Academy of Sciences",
    "Proceedings of the National Academy of Sciences of the United States of America",
    "Proceedings of the Royal Society A: Mathematical, Physical and Engineering Sciences",
    "Proceedings of the Royal Society of London. Series A",
    "Proceedings of the IEEE",
    "Proceedings of the IRE",
    "Proceedings of the Institute of Radio Engineers",
    "Proceedings of the ACM on Programming Languages",
    "Proceedings of the Institution of Mechanical Engineers, Part C",
    "Proceedings of the Indian National Science Academy",
    "Proceedings of the Japan Academy, Series B",
    "Proceedings of the Bulgarian Academy of Sciences",
    "Proceedings of the Physical Society",
    "Proceedings of the Nutrition Society",
    "Proceedings of the American Mathematical Society",
    "Proceedings of the Combustion Institute",
    "Proceedings of the Design Society",
    "Proceedings of the Association for Information Science and Technology",
    "Proceedings of Universities. Electromechanics",
    "Proceedings of Engineering and Technology Innovation",
    "Proceedings of the Southwest State University",
    "Proceedings of Civil Engineering in the Ocean",
    "Proceedings on Privacy Enhancing Technologies",
    "Mathematical Proceedings of the Cambridge Philosophical Society",
    "Digest Journal of Nanomaterials and Biostructures",
    "Current Digest of the Russian Press",
    # word-bounded: plural is not the venue word
    "EPJ Web of Conferences",
]

_VENUES = [
    ("Journal of Physics: Conference Series", "Conference"),
    ("IOP Conference Series: Materials Science and Engineering", "Conference"),
    ("Materials Today: Proceedings", "Proceedings"),
    ("ECS Meeting Abstracts", "Meeting Abstracts"),
    ("SID Symposium Digest of Technical Papers", "Symposium"),
    ("Proceedings of the AAAI Conference on Artificial Intelligence", "Proceedings"),
    ("Progress In Electromagnetics Research Symposium", "Symposium"),
    ("some workshop on things", "Workshop"),
    ("ECS  meeting   abstracts", "Meeting Abstracts"),
]


@pytest.mark.parametrize("journal", _NOT_VENUES)
def test_journals_carrying_a_venue_word_are_not_venues(journal: str) -> None:
    assert proceedings_venue(journal) is None


@pytest.mark.parametrize(("journal", "word"), _VENUES)
def test_proceedings_venues_return_the_canonical_word(journal: str, word: str) -> None:
    assert proceedings_venue(journal) == word


@pytest.mark.parametrize("journal", [None, "", "Nature Communications"])
def test_no_venue_signal(journal: str | None) -> None:
    assert proceedings_venue(journal) is None


def test_the_veto_is_checked_first_and_only_vetoed_names_are_flagged() -> None:
    assert venue_vetoed("Proceedings of the IEEE")
    assert not venue_vetoed("Proceedings of the IEEE Conference on Foo")
    assert proceedings_venue("Proceedings of the IEEE Conference on Foo") == (
        "Proceedings"
    )
    assert not venue_vetoed(None)


def test_a_proceedings_venue_is_not_a_review_journal() -> None:
    # "Progress in" would fire the review-venue rule; the venue wins.
    assert not is_review_like(None, "Progress In Electromagnetics Research Symposium")
    assert (
        review_reason(None, "Progress In Electromagnetics Research Symposium") is None
    )
    # the title arm still applies
    assert is_review_like(
        "A review of antennas", "Progress In Electromagnetics Research Symposium"
    )
    # and a plain review journal is unchanged
    assert is_review_like(None, "Progress in Materials Science")


def test_patterns_version_was_bumped() -> None:
    assert PATTERNS_VERSION == "2026-10-03.1"
