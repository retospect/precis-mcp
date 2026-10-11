"""Numeral cross-check for claim sentences (gr250034) -- advisory only.

A claim sentence's numerals should appear somewhere in the grounding
passages it was written from. :func:`absent_numerals` returns the claim
numerals (as written) that match *no* numeral of the source text. It is the
one deterministic slice of content-checking: a cheap floor for the
fabricated-value shape ("quantum efficiencies of 1-5%" where the source
reports none), blind to the dominant shape (a real source number attached
to the wrong thing -- that numeral *is* in the source).

Surfaces as the ``numeral-absent`` code in
:func:`~precis.taproot.sentence_lint.lint_claim_sentence`, silent without
``source_text``, never in ``gates._BLOCKING_LINT_CODES``.

Matching is on a canonical decimal value, ignoring units, so a stray unit
mismatch never hides a value (looser = fewer false positives):

- thousands separators (``1,000``, thin/no-break space), unicode minus and
  dashes (magnitudes are compared, sign is dropped), ``1.0`` == ``1``;
- ``x 10^n`` / ``x10⁻³`` / ``1.2e-3`` / TeX ``\\times 10^{-3}`` / bare ``10^5``;
- ranges (``1-5%``: both ends, the unit of the last end is shared);
- percent: ``16%`` matches ``0.16`` and ``16``;
- SI prefixes on a recognised unit: ``1.9 nN`` matches ``1900 pN``;
- spelled-out ``one``..``ten`` on the *source* side match digits.

Superscript/subscript runs are given a boundary before translation
(td244964: ``10⁵`` must not read as ``105``). Accepted false positives,
by design: derived values (ratios, range midpoints), dimension
conversions beyond SI prefixes (``Å`` is the only non-prefix unit
folded), spelled-out claim numerals are not checked at all, values that
live only in a figure or table image.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-")
_SUP_RUN_RE = re.compile("[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+")
_SUB_RUN_RE = re.compile("[₀₁₂₃₄₅₆₇₈₉₊₋]+")
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d"
#: Notation that carries digits but is not a quantity: nanotube index pairs
#: ``(10,0)``, Miller indices after a symbol ``Au(111)``, charges ``(2-)``,
#: and IUPAC locants ``1,8-diamino``.
_NON_QUANTITY_RE = re.compile(
    r"\(\s*\d+\s*,\s*-?\d+\s*\)"
    r"|(?<=[A-Za-z)])\(\s*\d+\s*\)"
    r"|\(\d[+-]\)"
    r"|\b\d+(?:,\d+){1,8}-(?=[A-Za-z])"
)

_NUM_RE = re.compile(
    r"""
    (?<![\w.^/)])(?<!\^-)(?<!\^\+)(?<![A-Za-z]-)
    (?:
        10\^(?P<pexp>[+-]?\d+)
      |
        (?P<mant>\d{1,3}(?:[,\u202f\u2009\u00a0]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
        (?:
            \s*[×x*·]\s*10\s*\^?\s*(?P<exp>[+-]?\d+)
          | [eE](?P<e>[+-]?\d+)(?!\w)
        )?
    )
    """,
    re.VERBOSE,
)
_RANGE_GAP_RE = re.compile(r"\s*(?:-|to)\s*")
_PCT_TAIL_RE = re.compile(r"\s?(?:(?:wt|vol|at|mol)\s?)?%")
_GLUED_TAIL_RE = re.compile(r"°?([A-Za-zµμΩÅ]+)")
_SPACED_TAIL_RE = re.compile(r"\s°?([A-Za-zµμΩÅ]+)")

_PREFIX_EXP = {
    "y": -24, "z": -21, "a": -18, "f": -15, "p": -12, "n": -9, "µ": -6,
    "μ": -6, "u": -6, "m": -3, "c": -2, "d": -1, "k": 3, "M": 6, "G": 9,
    "T": 12, "P": 15,
}  # fmt: skip
#: Units that take SI prefixes, mapped to their own power-of-ten scale.
_PREFIXABLE = {
    "m": 0, "g": 0, "s": 0, "A": 0, "V": 0, "W": 0, "J": 0, "N": 0, "Pa": 0,
    "Hz": 0, "eV": 0, "K": 0, "mol": 0, "L": 0, "F": 0, "H": 0, "T": 0,
    "S": 0, "Ω": 0, "bar": 0, "M": 0, "C": 0, "B": 0, "Å": -10,
}  # fmt: skip
#: Recognised units that take no prefix (keeps a glued ``5ppm`` a numeral).
_PLAIN_UNITS = frozenset(
    {"ppm", "ppb", "rpm", "dB", "h", "min", "d", "atm", "Torr", "sccm", "cal"}
)
_SPELLED = {
    w: Decimal(i + 1)
    for i, w in enumerate(
        ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    )
}
_SPACE_GROUPED_RE = re.compile(r"(?<![\w.])\d{1,3}(?: \d{3})+(?!\d)")
_SPELLED_RE = re.compile(r"\b(" + "|".join(_SPELLED) + r")\b", re.IGNORECASE)


def _prepare(text: str) -> str:
    """Boundary-safe notation fold: sub/superscript runs become ``_n`` /
    ``^n`` *before* NFKC could glue them onto adjacent digits."""
    text = _SUP_RUN_RE.sub(lambda m: "^" + m.group(0).translate(_SUPERSCRIPTS), text)
    text = _SUB_RUN_RE.sub(lambda m: "_" + m.group(0).translate(_SUBSCRIPTS), text)
    text = unicodedata.normalize("NFKC", text)
    for d in _DASHES:
        text = text.replace(d, "-")
    text = re.sub(r"\\(?:times|cdot)\b", "×", text)
    text = re.sub(r"\^\s*\{\s*([+-]?\d+)\s*\}", r"^\1", text)
    text = re.sub(r"[${}]|\\[,;]|\\mathrm", "", text)
    return text


def _unit_exp(word: str) -> int | None:
    """Power-of-ten scale of a recognised unit word, ``None`` when unknown."""
    if word in _PREFIXABLE:
        return _PREFIXABLE[word]
    if word in _PLAIN_UNITS:
        return 0
    if len(word) > 1 and word[0] in _PREFIX_EXP and word[1:] in _PREFIXABLE:
        return _PREFIX_EXP[word[0]] + _PREFIXABLE[word[1:]]
    return None


def _exp(s: str) -> int | None:
    """Exponent of a numeral, ``None`` when absurd (|exp| > 400): such a
    value is extraction garbage, and ``Decimal`` would overflow on it."""
    digits = s.lstrip("+-")
    if len(digits) > 3 or not digits.isdigit():
        return None
    n = int(s)
    return n if abs(n) <= 400 else None


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[,\u202f\u2009\u00a0]", "", s))
    except InvalidOperation:
        return None


def _numerals(text: str, *, strict: bool) -> list[tuple[str, frozenset[Decimal]]]:
    """``(as written, candidate canonical values)`` per numeral of ``text``.
    ``strict`` (claim side) drops year-like integers and digits glued to an
    unrecognised letter suffix (``3D``, ``5th``); the source side keeps
    them so the claim side can only lose a match, never gain a false hit."""
    text = _NON_QUANTITY_RE.sub(" ", _prepare(text))
    found = list(_NUM_RE.finditer(text))
    out: list[tuple[str, frozenset[Decimal]]] = []
    extra: set[Decimal] = set()
    for i, m in enumerate(found):
        if m.group("pexp") is not None:
            pexp = _exp(m.group("pexp"))
            if pexp is None:
                continue
            value = Decimal(10) ** pexp
            written, tail_at = m.group(0), m.end()
            if pexp > 0:
                # PDF extraction flattens ``10⁶`` to ``106``.
                flat = _dec("10" + m.group("pexp").lstrip("+"))
                extra = {flat} if flat is not None else set()
        else:
            mant = _dec(m.group("mant"))
            if mant is None:
                continue
            value = mant
            exp = m.group("exp") or m.group("e")
            if exp is not None:
                k = _exp(exp)
                if k is None:
                    continue
                try:
                    value = value * Decimal(10) ** k
                except ArithmeticError:
                    continue
            written, tail_at = m.group(0), m.end()
        forms = {value, *extra}
        extra = set()
        tail = text[tail_at : tail_at + 16]
        unit_text = ""
        pct = _PCT_TAIL_RE.match(tail)
        glued = _GLUED_TAIL_RE.match(tail)
        spaced = _SPACED_TAIL_RE.match(tail)
        if pct:
            forms.add(value / 100)
            unit_text = pct.group(0).strip()
        elif (hit := glued or spaced) is not None:
            k = _unit_exp(hit.group(1))
            if k is not None:
                forms.add(value * Decimal(10) ** k)
                unit_text = hit.group(0).strip()
            elif glued and strict:
                continue
        elif i + 1 < len(found) and _RANGE_GAP_RE.fullmatch(
            text[tail_at : found[i + 1].start()]
        ):
            # Range start with no unit of its own: share the end's unit.
            nxt = found[i + 1]
            ntail = text[nxt.end() : nxt.end() + 16]
            if _PCT_TAIL_RE.match(ntail):
                forms.add(value / 100)
        if strict and not unit_text and value == value.to_integral_value():
            if 1900 <= value <= 2100 and "." not in written:
                continue
        out.append((f"{written} {unit_text}".strip(), frozenset(forms)))
    return out


def absent_numerals(sentence: str, source_text: str) -> list[str]:
    """Numerals of ``sentence`` (as written, with unit when one follows),
    in order, deduplicated, that match no numeral of ``source_text``.
    ``[]`` for a sentence with no numbers or an empty source."""
    if not sentence or not source_text:
        return []
    have: set[Decimal] = set()
    for _, forms in _numerals(source_text, strict=False):
        have |= forms
    for m in _SPACE_GROUPED_RE.finditer(source_text):
        # ``1 786`` -- plain-space thousands grouping is ambiguous in
        # general, so it only ever widens the source side.
        if (grouped := _dec(m.group(0).replace(" ", ""))) is not None:
            have.add(grouped)
    for m in _SPELLED_RE.finditer(source_text):
        have.add(_SPELLED[m.group(1).lower()])
    seen: set[str] = set()
    missing: list[str] = []
    for written, forms in _numerals(sentence, strict=True):
        if written in seen or forms & have:
            continue
        seen.add(written)
        missing.append(written)
    return missing
