"""``precis.taproot.review_like.is_review_like`` — the conservative
title/journal review heuristic behind the hub-cite fallback
(``taproot/cite.py::hub_cite_keys``). Pure, no DB."""

from __future__ import annotations

import pytest

from precis.taproot.review_like import is_review_like, review_reason


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
