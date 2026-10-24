"""Deterministic checks for proposed measure runs — port of the pilot's
``pilot.py`` literals/units machinery (docs/backlog/
corpus-quantitative-extraction.md §10-12, Reto knowledge-mesh-10).

Every check is a *flag, not a verdict*: they prove mechanical claims
(the literal is verbatim in its span, the unit is spelled in the text,
the anchor doesn't crop a number). Semantic validity stays with the
sampling review (:mod:`precis.extract.accept`) — this file deliberately
establishes none.

Carries the two ported defect fixes:

* :func:`unit_has_text_support` applies its letter-edge lookarounds only
  where the unit's own edge is a letter — a bare ``%`` in "95%NO3" is
  the unit spelled exactly.
* Categorical rows: :func:`literal_supported` takes ``numeric_required``;
  a ``value_form='categorical'`` literal ("NH3", "Cordierite") is
  verbatim-checked but need not contain a number.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from precis.utils.text import markup_surface

NUMBER_V3 = re.compile(
    r"(?<![\w.])(?:[-+−](?:\s*\|\s*)?)?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?(?!\d|\.\d)"
)
SCIENTIFIC_NUMBER = re.compile(
    r"(?<![\w.])(?:[-+−]?\d+(?:\.\d+)?\s*(?:\\times|\\cdot|×|·)\s*)?"
    r"10\s*(?:\^\s*\{?\s*[-+−]?\d+\s*\}?|<sup>[-+−]?\d+</sup>|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)"
)
FRACTION_NUMBER = re.compile(r"(?<![\w.])[-+−]?\d+\s*/\s*\d+(?![\w.])")

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzµΩ"


def literal_normalize(value: str) -> str:
    """The matching form of a literal or span: markup folded to the
    printed reading (:func:`precis.utils.text.markup_surface` — ``\\pm``
    -> ``±``, ``^{−2}`` -> ``-2``, ``<sup>``/``<sub>`` unwrapped, minus
    and dash glyphs folded to ASCII ``-``, ``|`` cell separators and all
    whitespace collapsed, a sign-only table cell merged onto the next
    number (gr511458, gr511461))."""
    return markup_surface(value)


def _ws_free(value: str) -> str:
    """``literal_normalize`` with every space removed — a quantity split
    across table cells or lines still contains itself."""
    return re.sub(r"\s+", "", value)


#: The phrasings a sentence uses where a ``<``/``>`` would sit: the bound
#: cue must end the text right before the literal it caps. ``not exceed``
#: is an UPPER bound on the value — it is listed before the plain
#: ``exceed`` family so the longer phrase wins.
_BOUND_UPPER_CUES = re.compile(
    r"(?:at most|no more than|not exceed\w*|never exceed\w*|up to|"
    r"no greater than|less than|fewer than|under|below|within|"
    r"maximum of|at maximum)\s*$",
    re.IGNORECASE,
)
_BOUND_LOWER_CUES = re.compile(
    r"(?:at least|no less than|no fewer than|more than|greater than|"
    r"above|over|exceed\w*|surpass\w*|minimum of|at minimum|"
    r"longer than|older than)\s*$",
    re.IGNORECASE,
)


def bound_hint(prefix_text: str) -> str | None:
    """``'upper'`` / ``'lower'`` when ``prefix_text`` (the normalised span
    up to the literal's position) ends in a bound cue — ``'at most '``
    caps the next number, ``'exceeding '`` floors it. ``None`` when no
    cue precedes. The upper list is checked first so a ``not exceed``
    phrase never reads as a lower bound."""
    tail = literal_normalize(prefix_text)
    if _BOUND_UPPER_CUES.search(tail):
        return "upper"
    if _BOUND_LOWER_CUES.search(tail):
        return "lower"
    return None


def literal_positions(literal: str, span_text: str) -> list[int]:
    """Start index of every occurrence of the normalised literal in the
    normalised span — the positions a bound/sign cue check looks
    behind."""
    norm = literal_normalize(span_text)
    lit = literal_normalize(literal)
    if not lit:
        return []
    out = []
    at = norm.find(lit)
    while at >= 0:
        out.append(at)
        at = norm.find(lit, at + 1)
    return out


#: The phrasings that make an unsigned printed literal a NEGATIVE
#: change: 'cools the surface by 0.9' stores -0.9. The cue ends the text
#: right before the literal, like a bound cue (gr511461).
_DECREASE_CUES = re.compile(
    r"\b(?:cool\w*|cold\w*|reduc\w*|decreas\w*|declin\w*|drop\w*|"
    r"falls?|fell|lower\w*|diminish\w*|shrink\w*|drawdown|"
    r"loss(?:es)?|below|offset\w*|removal|suppress\w*|"
    r"attenuat\w*|depress\w*)\b",
    re.IGNORECASE,
)
#: The same cue behind the number instead — '15% below the baseline'
#: prints the sign's reason after the literal.
_DECREASE_AFTER = re.compile(
    r"^\s*%?\s*(?:[a-z]+\s+){0,3}?(?:below|beneath|under|lower than)\b",
    re.IGNORECASE,
)


def sign_hint(prefix_text: str) -> int | None:
    """``-1`` when the last 64 normalised chars of ``prefix_text`` (the
    span up to the literal's position) carry a decrease cue — 'cools the
    surface by 0.9' stores -0.9. ``None`` otherwise."""
    if _DECREASE_CUES.search(literal_normalize(prefix_text)[-64:]):
        return -1
    return None


def sign_hint_after(suffix_text: str) -> int | None:
    """``-1`` when the text just after the literal opens with a decrease
    cue — 'below the baseline', 'under the 1990 level'."""
    if _DECREASE_AFTER.match(literal_normalize(suffix_text)):
        return -1
    return None


_SPREAD_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_SPREAD_RES = (
    re.compile(rf"^({_SPREAD_NUM})\s+to\s+({_SPREAD_NUM})$", re.IGNORECASE),
    re.compile(rf"^({_SPREAD_NUM})\s*[,;]\s*({_SPREAD_NUM})$"),
    re.compile(rf"^({_SPREAD_NUM})\s*[–—~]\s*({_SPREAD_NUM})$"),
    # a '-' between two digits is a range dash, not a sign
    re.compile(rf"^({_SPREAD_NUM})(?<=\d)\s*-\s*(?=\d)({_SPREAD_NUM})$"),
)


def parse_spread(text: str) -> tuple[float, float] | None:
    """``(low, high)`` for a bracketed spread — ``'[-0.2 to -1.3]'``,
    ``'(0.5, 2)'``, ``'[1-9]'`` — or ``None`` when the text does not hold
    exactly two numbers. Brackets and the separator are syntax; the
    values come out sorted like an interval literal's."""
    inner = literal_normalize(text).strip("[](){} ")
    for pattern in _SPREAD_RES:
        match = pattern.match(inner)
        if match is not None:
            a, b = float(match.group(1)), float(match.group(2))
            return (min(a, b), max(a, b))
    return None


def numeric_regions(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, part)`` for every numeric expression in ``text`` —
    plain numbers (incl. thousands separators and exponents), scientific
    ``×10^`` forms and fractions — ordered, non-overlapping."""
    scan = text.replace("−", "-").replace("–", "-")
    compound = sorted(
        (match.start(), match.end(), text[match.start() : match.end()])
        for regex in (SCIENTIFIC_NUMBER, FRACTION_NUMBER)
        for match in regex.finditer(scan)
    )
    atoms = [
        (match.start(), match.end(), text[match.start() : match.end()])
        for match in NUMBER_V3.finditer(scan)
        if not any(start <= match.start() < end for start, end, _ in compound)
    ]
    return sorted(compound + atoms)


def literal_supported(
    literal: str,
    span_text: str,
    source_text: str,
    span: tuple[int, int],
    *,
    numeric_required: bool = True,
) -> bool:
    """The literal is verbatim in the span AND every numeric region it
    carries is a complete source expression inside that span.

    The second clause is the boundary check that refuses "505.9" cited
    from "1,505.9 mg" or "2" cited from "2 \\times 10^{-3}" — the
    literal's number must appear as a *complete* region, not a prefix of
    a longer one. ``numeric_required=False`` is the categorical form: a
    literal like "NH3" is verbatim-checked only.
    """
    lit = literal_normalize(literal)
    if not lit or _ws_free(lit) not in _ws_free(literal_normalize(span_text)):
        return False
    expected = {
        _ws_free(literal_normalize(part)) for _, _, part in numeric_regions(literal)
    }
    crossing: set[str] = set()
    available: set[str] = set()
    for start, end, part in numeric_regions(source_text):
        if span[0] <= start and end <= span[1]:
            available.add(_ws_free(literal_normalize(part)))
        elif start < span[0] < end or start < span[1] < end:
            crossing.add(_ws_free(literal_normalize(part)))
    # Markup can move a sign or an exponent into tags the raw scan splits
    # out ("<sup>−</sup>1.9" reads "-1.9", "m}^{-2}" reads "m-2"), so the
    # folded span's own regions count too — except a folded part that is
    # a fragment of a raw region the span boundary cuts (the anti-crop
    # guard: "505.9" cropped out of "1,505.9" still refuses).
    for _, _, part in numeric_regions(literal_normalize(span_text)):
        folded = _ws_free(literal_normalize(part))
        if not any(folded in cut for cut in crossing):
            available.add(folded)
    if not expected <= available:
        return False
    return not (numeric_required and not expected)


def unit_surface(value: str) -> str:
    """The plain-text reading of a possibly-markup unit: HTML entities and
    tags stripped, LaTeX commands folded, superscripts flattened
    (``cm⁻²``/``cm^-2``/``cm^{−2}``/``cm<sup>−</sup><sup>2</sup>`` →
    ``cm-2``) — :func:`precis.utils.text.markup_surface`."""
    return markup_surface(value)


def unit_has_text_support(unit: object, text: str) -> bool:
    """The unit appears as written in ``text`` — not just in a parser's
    surface map. ``None`` (no unit claimed) is supported by definition;
    an unstringable or blank unit is not."""
    if unit is None:
        return True
    if not isinstance(unit, str) or not unit.strip():
        return False
    literal = re.sub(r"\s+", "", unit_surface(unit))
    pattern = r"\s*".join(re.escape(char) for char in literal)
    #  Letter guards only where the literal's own edge is a letter: they
    #  stop "eV" matching inside "meV", but a bare "%" carries no letter
    #  edge — "95%NO3" or "2%w/v" still spells the unit exactly.
    if literal[0] in _LETTERS:
        pattern = r"(?<![A-Za-zµΩ])" + pattern
    if literal[-1] in _LETTERS:
        pattern += r"(?![A-Za-zµΩ])"
    return bool(re.search(pattern, unit_surface(text)))


def numeric_crop_errors(
    source_text: str, spans: Iterable[tuple[int, int]]
) -> list[str]:
    """Each span boundary that cuts a numeric region (a literal cited as
    "…505.9" of "1,505.9 mg"). Punctuation-only crossings are allowed:
    ``functional.69,70`` is a citation marker, not a cropped number."""
    errors = []
    for a, b in spans:
        for start, end, part in numeric_regions(source_text):
            if start < a < end or start < b < end:
                left = source_text[start:a].rstrip()
                right = source_text[b:end].lstrip()
                if left and not left[-1].isalnum():
                    continue
                if right and not right[0].isalnum():
                    continue
                errors.append(
                    f"span boundary cuts the numeric region {part!r} at {a}-{b}"
                )
    return errors
