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

import html
import re
from collections.abc import Iterable

NUMBER_V3 = re.compile(
    r"(?<![\w.])[-+−]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?(?!\d|\.\d)"
)
SCIENTIFIC_NUMBER = re.compile(
    r"(?<![\w.])(?:[-+−]?\d+(?:\.\d+)?\s*(?:\\times|\\cdot|×|·)\s*)?"
    r"10\s*(?:\^\s*\{?\s*[-+−]?\d+\s*\}?|<sup>[-+−]?\d+</sup>|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)"
)
FRACTION_NUMBER = re.compile(r"(?<![\w.])[-+−]?\d+\s*/\s*\d+(?![\w.])")

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzµΩ"


def literal_normalize(value: str) -> str:
    """The matching form of a literal or span: whitespace squashed, the
    Unicode minus/dash variants folded to ASCII ``-``."""
    return re.sub(r"\s+", " ", value.replace("−", "-").replace("–", "-")).strip()


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
    if not lit or lit not in literal_normalize(span_text):
        return False
    expected = {literal_normalize(part) for _, _, part in numeric_regions(literal)}
    available = {
        literal_normalize(part)
        for start, end, part in numeric_regions(source_text)
        if span[0] <= start and end <= span[1]
    }
    if not expected <= available:
        return False
    return not (numeric_required and not expected)


def unit_surface(value: str) -> str:
    """The plain-text reading of a possibly-markup unit: HTML entities and
    tags stripped, LaTeX ``\\mu``/``\\Omega``/``\\%``/``\\times`` folded,
    superscripts flattened (``cm⁻²``/``cm^-2``/``cm^{−2}`` → ``cm-2``)."""
    value = html.unescape(value).replace("μ", "µ").replace("−", "-")
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\\mu", "µ").replace("\\Omega", "Ω").replace("\\%", "%")
    value = re.sub(
        r"\\(?:text|mathrm|textrm|mathbf|operatorname|rm|left|right|tiny)\b", "", value
    )
    value = re.sub(r"\\[,;! ]", " ", value)
    value = value.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+"))
    return re.sub(r"[$^_{}]", "", value)


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
