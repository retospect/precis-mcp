"""utils/adsorbate_star — the shared ``*``-is-literal rule (gr674905).

The fragments are spliced into every copy of the single-``*`` italic
shape (``export/latex._MD_ITALIC``, ``export/docx._SPAN``, web linkify's
``_MD_ITALIC``, ``house_style._ITALIC_STAR``); this file pins the shared
part: a ``*`` touching an element symbol is literal, and ``\*`` is never
a delimiter.
"""

from __future__ import annotations

import re

from precis.utils.adsorbate_star import (
    _ELEMENT_SYMBOLS,
    STAR_NOT_FOLLOWED_BY_ELEMENT,
    STAR_NOT_PRECEDED_BY_ELEMENT,
)

#: The shape every consumer builds from the fragments.
_ITALIC = re.compile(
    rf"(?<![*\w\\])\*(?!\s){STAR_NOT_FOLLOWED_BY_ELEMENT}"
    rf"([^*]+?)(?<![\s\\]){STAR_NOT_PRECEDED_BY_ELEMENT}\*(?!\w)"
)


def test_element_table_is_complete() -> None:
    assert len(_ELEMENT_SYMBOLS) == 118
    assert {"H", "N", "O", "Pd", "Au", "Og"} <= _ELEMENT_SYMBOLS


def test_adsorbate_stars_stay_literal() -> None:
    # The norr-her-survey hub text that fired five italic_star flags.
    text = "*NO₃⁻ dissociation to *NO₂⁻ + O* is more favorable"
    assert _ITALIC.findall(text) == []
    for species in (
        "*NO",
        "*H",
        "*OH",
        "*NO2",
        "*H2O",
        "*CH4",
        "*Au",
        "H*",
        "O*",
        "Pd*",
        "Au*",
    ):
        assert _ITALIC.findall(species) == [], species


def test_emphasis_still_matches_and_escape_stays_literal() -> None:
    assert _ITALIC.findall("a *word* pair") == ["word"]
    assert _ITALIC.findall(r"a \*word\* pair") == []  # \* is literal
    # A trailing adsorbate star is neither an opener nor the closer of a
    # real italic span.
    assert _ITALIC.findall("*word* tail O*") == ["word"]
    # Bold, a*b and spaced multiplication are untouched by the new guards.
    assert _ITALIC.findall("**b** a*b 2 * 3") == []


def test_capital_word_italics_survive() -> None:
    # An element start continued by a lowercase letter is a word, not a
    # species — publisher italics like *Nature* / *Candida* / *Helicobacter*
    # still render (the (?![a-z]) boundary). *In is the accepted ambiguity:
    # adsorbed indium and capital-Initial italic can't be told apart, and
    # the species reading wins.
    assert _ITALIC.findall("*Candida* *Nature* *Helicobacter*") == [
        "Candida",
        "Nature",
        "Helicobacter",
    ]
    assert _ITALIC.findall("*In situ*") == []


def test_speakable_keeps_adsorbate_stars() -> None:
    # draft/narrate._ITALIC is the same shape — speakable() used to drop
    # both stars and pass "NO₂⁻ + O" to the voice.
    from precis.draft.narrate import speakable

    assert speakable("*NO dissociation to O*") == "*NO dissociation to O*"
    assert speakable("a *word* pair") == "a word pair"
