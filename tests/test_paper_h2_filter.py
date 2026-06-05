"""Journal-template H2 filter for the paper handler's TOC adapter.

The markdown ingester sometimes promotes journal page chrome
(``PAPER``, ``View Article Online``, ``Broader context``) to H1
or H2. These confuse the TOC's H2-mode policy because they're not
real sections — they're publisher chrome around the real body.
This filter drops them before they reach the renderer.
"""

from __future__ import annotations

import pytest

from precis.handlers.paper import _is_journal_template_heading

# ── exact-match templates ───────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        # Article-type labels
        "PAPER",
        "paper",
        "Paper",
        "Article",
        "Research Article",
        "Review",
        "Editorial",
        "Communication",
        # Journal nav / chrome
        "View Article Online",
        "view article online",
        "Article info",
        "Article Information",
        "Article history",
        "Cite this article",
        "Open Access",
        # Publisher sidebars
        "Broader context",
        "Graphical Abstract",
        "Highlights",
        "Key Points",
        # Date stamps
        "Received",
        "Accepted",
        # Footer chrome
        "Supporting Information",
        "Supplementary Material",
    ],
)
def test_known_template_strings_are_filtered(title: str) -> None:
    assert _is_journal_template_heading(title), (
        f"expected {title!r} to be classified as template chrome"
    )


# ── all-caps short-word rule ────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    ["PAPER", "NEWS", "BRIEF", "NOTES", "REVIEW", "LETTER"],
)
def test_short_allcaps_word_is_filtered(title: str) -> None:
    assert _is_journal_template_heading(title)


# ── real sections must pass through ─────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "Introduction",
        "Methods",
        "Results and Discussion",
        "Conclusions",
        "Experimental Section",
        "Materials and Methods",
        "Synthesis of MOF-5",
        "Background",
        "Discussion",
        "Results",
        "1. Introduction",
        "2.3 Synthesis",
        # Capitalised but multi-word — real section
        "Surface Characterization",
        "Density Functional Theory",
        # All-caps but longer than 8 chars → real heading
        "INTRODUCTION",
        # NB: "REFERENCES" is 10 chars, so by the rule we keep it; ack
        # is filtered elsewhere (boilerplate classifier).
        "REFERENCES",
    ],
)
def test_real_sections_are_kept(title: str) -> None:
    assert not _is_journal_template_heading(title), (
        f"expected {title!r} to be kept as a real section"
    )


# ── empty / whitespace ──────────────────────────────────────────────


def test_empty_title_filtered() -> None:
    assert _is_journal_template_heading("")
    assert _is_journal_template_heading("   ")
    assert _is_journal_template_heading("\n\t")


# ── edge cases ──────────────────────────────────────────────────────


def test_case_insensitive_match() -> None:
    assert _is_journal_template_heading("PAPER")
    assert _is_journal_template_heading("paper")
    assert _is_journal_template_heading("Paper")
    assert _is_journal_template_heading("View Article Online")
    assert _is_journal_template_heading("view article online")
    assert _is_journal_template_heading("VIEW ARTICLE ONLINE")


def test_unrelated_short_uppercase_word_filtered() -> None:
    # The all-caps rule is conservative: any single short ALL-CAPS
    # word looks like a chrome label, even if it isn't one. This
    # might drop the rare real section called "FUNDS" or "DATA"
    # but those are vanishingly rare as real H2 titles.
    assert _is_journal_template_heading("DATA")
    assert _is_journal_template_heading("CODE")


def test_punctuation_in_short_allcaps_is_kept() -> None:
    # Numbered headings like "1." or with periods aren't pure
    # alpha — kept.
    assert not _is_journal_template_heading("1.")
    assert not _is_journal_template_heading("2A")  # mixed digit/letter
