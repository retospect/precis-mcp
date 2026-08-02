"""Unit tests for the non-concept term filter (gripe 186183).

The gallery below is grounded in the real junk card_forge minted before the
gate: topic-labels ("new trends"), stock academic phrases ("extensively
examined"), and front-matter ("Dedication"). The kept set guards against the
filter swallowing genuine domain terms whose head happens to be generic.
"""

from __future__ import annotations

import pytest

from precis.reading.term_quality import non_concept_reason

# Terms that must be refused (the documented failure gallery + close kin).
_REFUSED = [
    "new trends",
    "new compounds",
    "recent advances",
    "novel compounds",
    "future directions",
    "improved properties",
    "various applications",
    "many advantages",
    "several methods",
    "extensively examined",
    "widely studied",
    "critically evaluated",
    "Dedication",
    "Acknowledgements",
    "Introduction",
    "References",
    "Conclusion",
    "results",  # section-word singleton (front-matter set)
]

# Real domain terms that must survive. The filter is deliberately conservative:
# it never fires on an ambiguous adjective (critical/major/advanced/…) or a bare
# generic head, because a wrongly-dropped concept is worse than a junk one
# reaching the card-side gate. Several of these are the reviewer's concrete
# false-positive scenarios — regression guards against re-broadening the sets.
_KEPT = [
    "diarylethene",
    "fatigue resistance",
    "thermal irreversibility",
    "quantum yield",
    "photochromism",
    "expert systems",  # 'expert' is not a vacuous modifier
    "control theory",
    "organic compounds",  # 'organic' is not a vacuous modifier
    "band gap",
    "bacteriorhodopsin",
    "activation energy",
    "reaction mechanism",
    # reviewer's false-positive gallery — these MUST stay kept:
    "advanced materials",
    "critical system",
    "critical systems",
    "critical case",
    "major system",
    "common factor",
    "general system",
    "well ordered",  # 'well' is not a rhetorical adverb here
    "highly correlated",
    "index",  # Fredholm/refractive index — overloaded, kept
    "background",  # background field/subtraction — overloaded, kept
    "field",  # bare generic-looking word, but a core concept
    "work",
]


@pytest.mark.parametrize("term", _REFUSED)
def test_non_concept_terms_are_refused(term: str) -> None:
    assert non_concept_reason(term) is not None, f"{term!r} should be refused"


@pytest.mark.parametrize("term", _KEPT)
def test_real_terms_are_kept(term: str) -> None:
    assert non_concept_reason(term) is None, f"{term!r} should be kept"


def test_empty_term_is_refused() -> None:
    assert non_concept_reason("") is not None
    assert non_concept_reason("   ") is not None
