"""Reader-side rendering for the ADR 0052 term registry — the rich ``.pa-pop``
hover rows and the ``assign="render"`` numeral substitution. Pure string
rendering (no DB)."""

from __future__ import annotations

from precis.utils import handle_registry
from precis_web.linkify import linkify_refs


def test_part_hover_shows_rich_rows() -> None:
    html = str(
        linkify_refs(
            "The LM358 drives the load.",
            markdown=True,
            compact=True,
            abbrevs={
                "LM358": {
                    "definition": "an operational amplifier",
                    "mpn": "LM358DR",
                    "manufacturer": "Texas Instruments",
                    "url": "https://example.com/lm358.pdf",
                    "registry": "components",
                }
            },
        )
    )
    assert 'class="pa"' in html
    assert "an operational amplifier" in html
    assert "MPN LM358DR" in html
    assert "Texas Instruments" in html
    assert 'href="https://example.com/lm358.pdf"' in html
    assert "datasheet" in html


def test_plain_glossary_hover_has_no_bag_rows() -> None:
    html = str(
        linkify_refs(
            "A MOF is porous.",
            markdown=True,
            compact=True,
            abbrevs={"MOF": {"definition": "metal-organic framework"}},
        )
    )
    assert "metal-organic framework" in html
    assert "pa-def" in html
    assert "MPN" not in html and "datasheet" not in html


def test_bare_string_entry_still_renders() -> None:
    """A legacy ``{short: str}`` map (e.g. a caller that didn't upgrade) still
    renders the definition — the highlighter is tolerant of both shapes."""
    html = str(
        linkify_refs(
            "PEI here.",
            markdown=True,
            compact=True,
            abbrevs={"PEI": "polyethyleneimine"},
        )
    )
    assert "polyethyleneimine" in html


def test_render_numeral_substitution_for_part_handle() -> None:
    norm = handle_registry.normalize("dc41")
    html = str(
        linkify_refs(
            "the widget [[dc41]] is coupled to the frame.",
            markdown=True,
            compact=True,
            callouts={norm: "105"},
        )
    )
    # The numeral is the anchor label, not the ¶ sigil.
    assert ">105<" in html
    assert "¶" not in html


def test_no_callout_keeps_the_sigil() -> None:
    html = str(
        linkify_refs(
            "see [[dc41]] there.",
            markdown=True,
            compact=True,
            callouts={},
        )
    )
    # Falls back to the generic chunk sigil when the handle carries no numeral.
    assert ">¶<" in html
