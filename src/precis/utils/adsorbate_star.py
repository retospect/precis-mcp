r"""Adsorbate star notation — the literal ``*`` in ``*NO``, ``H*``, ``O*``.

Surface chemistry writes an adsorbed species as a formula starred on the
side that binds the surface — leading form ``*NO``, ``*H``, ``*OH``,
``*NO₃⁻``; trailing form ``H*``, ``O*``, ``Pd*``. Claim hubs carry the
notation verbatim (gr674905), so the single-``*`` emphasis rules shared by
the LaTeX/docx exporters, the web reader and the house-style lint must not
eat it: ``*NO₂⁻ + O*`` used to render "NO₂⁻ + O" in italics with both stars
dropped, and the lint's ``italic_star`` flag predicted the mis-render
correctly.

The shared rule: a ``*`` is literal when it *touches a chemical species* —
immediately followed by an element symbol (``*NO``) or immediately preceded
by one (``H*``). A star escaped as ``\*`` is literal everywhere else; every
consumer of these fragments ALSO keeps ``\*`` out of the delimiter classes
(``\\`` in the adjacent-char lookbehind), so an escaped star is never an
emphasis delimiter either.

Two fragments cover the two adjacency directions. Splice them into the
single-``*`` italic pattern at the delimiter ``*``:

* :data:`STAR_NOT_FOLLOWED_BY_ELEMENT` — a lookahead, placed right AFTER a
  candidate delimiter ``*``: no element symbol starts there.
* :data:`STAR_NOT_PRECEDED_BY_ELEMENT` — a lookbehind pair, placed right
  BEFORE a candidate delimiter ``*``: no element symbol ends there.
  ``re`` lookbehinds must be fixed-width, so the 1- and 2-letter symbol
  classes split into two lookbehinds.

Both are *negative* assertions: an element neighbour vetoes the delimiter
reading and the star falls through as literal text. A leading element
that a lowercase letter continues (``*Candida*``, ``*Nature*``) is a
word, not a species — the ``(?![a-z])`` boundary keeps it italic. An
element at a token boundary is genuinely ambiguous with a capital-initial
italic (``*In situ*`` vs adsorbed ``*In``); the species reading wins —
house style forbids italics anyway, and ``\*`` covers a star the author
insists on.
"""

from __future__ import annotations

import re
from typing import Final

#: IUPAC element symbols — the full 118, same concatenated-order trick
#: ``ingest/metadata_resolve``'s ``_ELEMENTS`` uses, extended to Og. The
#: full table, not an adsorbate subset: ``*Au``/``Pt*`` metal adatoms are
#: notation too.
_ELEMENT_SYMBOLS: Final[frozenset[str]] = frozenset(
    re.findall(
        r"[A-Z][a-z]?",
        "HHeLiBeBCNOFNeNaMgAlSiPSClArKCaScTiVCrMnFeCoNiCuZnGaGeAsSeBr"
        "KrRbSrYZrNbMoTcRuRhPdAgCdInSnSbTeIXeCsBaLaCePrNdPmSmEuGdTbDy"
        "HoErTmYbLuHfTaWReOsIrPtAuHgTlPbBiPoAtRnFrRaAcThPaUNpPuAmCm"
        "BkCfEsFmMdNoLrRfDbSgBhHsMtDsRgCnNhFlMcLvTsOg",
    )
)
assert len(_ELEMENT_SYMBOLS) == 118

_ELT1_CHARS: Final[str] = "".join(sorted(s for s in _ELEMENT_SYMBOLS if len(s) == 1))
_ELT2_ALT: Final[str] = "|".join(sorted(s for s in _ELEMENT_SYMBOLS if len(s) == 2))

#: Negative lookahead — place right AFTER a delimiter ``*`` candidate: the
#: star is not followed by an element symbol that a lowercase letter does
#: not continue, so it is not the leading adsorbate star of ``*NO`` /
#: ``*H`` / ``*OH`` / ``*NO₃⁻`` / ``*Au``. The ``(?![a-z])`` boundary keeps
#: ``*Candida*``/``*Nature*``-style words (element start continued by
#: lowercase) italic — a species never carries a trailing lowercase letter
#: that isn't part of a second element symbol.
STAR_NOT_FOLLOWED_BY_ELEMENT: Final[str] = (
    rf"(?!(?:{_ELT2_ALT}|[{_ELT1_CHARS}])(?![a-z]))"
)

#: Negative lookbehind pair — place right BEFORE a delimiter ``*``
#: candidate: the star is not preceded by an element symbol, so it is not
#: the trailing adsorbate star of ``H*`` / ``O*`` / ``Pd*`` / ``Au*``. The
#: 1- and 2-letter classes split because ``re`` lookbehinds must be
#: fixed-width.
STAR_NOT_PRECEDED_BY_ELEMENT: Final[str] = rf"(?<![{_ELT1_CHARS}])(?<!(?:{_ELT2_ALT}))"
