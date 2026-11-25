"""Numeric-token extraction for the lexical search index.

Walks a chunk's text and pulls out every recognizable
``<number><unit>`` token, returning normalized strings the agent
can grep over. Stored alongside chunks in a GIN-indexed TEXT[]
column so search can do exact-value lookups (``"1.523 eV"``) and
the agent can iterate values manually for range-ish queries.

Deliberately **not** structured — no parsed `value: 1.523`, no unit
normalization (eV ↔ meV ↔ J), no uncertainty handling. That's path 3
(:mod:`paper_facts`, deferred as task #63). This module is the cheap
precursor: lexical-only, ingest-time, no LLM, no schema commitment
beyond a single TEXT[] column.

Returned tokens are *normalized for matching*, not for display:

* Numbers stay as written (``"1.523"`` not ``"1523e-3"``)
* Unit casing stays as written (``"eV"`` vs ``"ev"`` differ; we
  preserve original) so the agent's query matches what the paper
  actually says.
* Whitespace between number and unit collapses to a single space
  (``"1.523eV"`` → ``"1.523 eV"`` for index uniformity).

Wide readings remove invisible PDF glue before parsing shared-unit ranges,
so both endpoints can ground a citation. Raw span offsets remain unchanged.
"""

from __future__ import annotations

import re

# Units we recognize as a closed set, ordered longest-first so the
# regex consumes the longest match (avoids partial matches like
# ``"keV"`` matching as ``"k"`` + value).
#
# Sources: typical scientific-paper unit vocabulary. Add new units
# when corpus evidence demands them — keeping this closed prevents
# matching arbitrary trailing words as units.
_UNITS: tuple[str, ...] = (
    # vibrational / spectroscopy
    "cm-1",
    "cm−1",
    # energy
    "keV",
    "meV",
    "eV",
    "kJ",
    "J",
    # voltage
    "kV",
    "mV",
    "V",
    # current
    "mA",
    "µA",
    "uA",
    "nA",
    "A",
    # frequency
    "GHz",
    "MHz",
    "kHz",
    "Hz",
    # percent
    "%",
    # temperature
    "°C",
    "°F",
    "K",
    # pressure
    "GPa",
    "MPa",
    "kPa",
    "mbar",
    "atm",
    "bar",
    "Pa",
    "torr",
    # concentration
    "mM",
    "µM",
    "uM",
    "nM",
    "ppm",
    "ppb",
    "M",
    # length
    "nm",
    "µm",
    "um",
    "mm",
    "cm",
    "Å",
    "Angstrom",
    # mass
    "kg",
    "mg",
    "µg",
    "ug",
    "ng",
    # time / count
    "ms",
    "µs",
    "us",
    "ns",
    "ps",
    "min",
    "hr",
    "h",
    "days",
    "cycles",
    "s",
    # current-density-ish
    "mAh/g",
    "Ah/g",
    "mAh",
    "Ah",
)


# Compile once at module load. Longest-first ordering is preserved
# because Python's ``re`` engine tries alternatives left-to-right and
# the union is sorted by length descending.
_NUM_RE_PART = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_UNITS_SORTED = sorted(_UNITS, key=len, reverse=True)
_UNITS_RE_PART = "|".join(re.escape(u) for u in _UNITS_SORTED)
_NUMERIC_RE = re.compile(
    rf"(?<![\w.])({_NUM_RE_PART})\s*({_UNITS_RE_PART})(?!\w)",
    re.UNICODE,
)


#: A list sharing one trailing unit, read backwards from the unit-bearing
#: member: "0.44 and 0.26 eV", "1, 2 and 3 nm", "0.1, 0.2 mm".
_LIST_HEAD_RE = re.compile(
    r"(?<![\w.])(-?\d+(?:\.\d+)?)(?:\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+)$"
)
_LIST_HEAD_CHARS = 40

#: Evidence spellings the tokenizer misses: "2,000 p.p.m." is "2000 ppm".
#: Both forms are read (the ``numerics`` column holds only the raw one).
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
_PPM_RE = re.compile(r"\bp\.p\.m\.?")
#: PDF math: ``$1.78\pm0.11$ Å`` — unwrap the math and read the ``±`` as a
#: two-member list so 1.78 takes the unit too. Inline math is ``$…$`` or
#: ``\(…\)``; a space-padded ``$`` pair counts as math only with a TeX
#: command or ``±`` inside, so "costs $5 and $10" is left alone.
_MATH_INLINE_RE = re.compile(
    r"\$\$(.{1,200}?)\$\$|\$([^$\n]{1,200}?)\$|\\\((.{0,200}?)\\\)"
)
_TEX_WRAP_RE = re.compile(r"\\(?:mathrm|text|textrm|mathit)\{([^{}]*)\}")
#: LaTeX commands PDF extractors leave in the text, mapped to the symbol.
_TEX_SYMBOLS = (
    (
        re.compile(r"\^\s*\{?\s*\\circ\s*\}?|\\deg(?![A-Za-z])|\\degree(?![A-Za-z])"),
        "°",
    ),
    (re.compile(r"\\pm(?![A-Za-z])"), "±"),
    (re.compile(r"\\times(?![A-Za-z])"), "×"),
    (re.compile(r"\\sim(?![A-Za-z])"), "~"),
    (re.compile(r"\\approx(?![A-Za-z])"), "≈"),
    (re.compile(r"\\%"), "%"),
)
#: Degree glyph variants PDF extractors emit: ring operator ``∘`` (U+2218),
#: ring above ``˚`` (U+02DA), ordinal ``º`` (U+00BA), and the one-glyph ``℃``/``℉``;
#: all read as ``°`` (``450 ∘C`` / ``450 ° C`` -> ``450 °C``).
_DEGREE_UNIT_RE = re.compile(r"(?<=\d)\s*[°∘˚º]\s*(?=[CF](?!\w))")
_DEGREE_LONE_RE = re.compile(r"(?<=\d)\s*[∘˚º](?![\w])")
_DEGREE_LIGATURE_RE = re.compile(r"\s*([℃℉])")
_PLUS_MINUS_RE = re.compile(r"\s*(?:±|\+/-|\+−|\+-)\s*")
#: PDF micro: ``$\mu$ m`` / ``\mu m`` / Greek ``μm`` all read as ``µm`` (U+00B5).
_TEX_MU_RE = re.compile(r"\$?\\mu\$?\s*(?=[mAMgs]\b)")
#: PDF TeX angstrom: ``6 \AA`` / ``6\,\AA`` / ``6~$\AA$`` / ``6\,{\AA}``
#: read as ``6 Å`` (gr716055: a correctly-cited ``6 Å`` cutoff printed with
#: the TeX macro flagged ungrounded against the UTF-8 spelling). The leading
#: group eats only TeX spacing (``\,``/``\;``/``~``/math dollars/braces),
#: never plain whitespace, so ``6 \AA`` keeps its separating space; a letter
#: right after the macro name means a different command and is left alone.
_TEX_ANGSTROM_RE = re.compile(
    r"(?:\\[,;:! ]|[~${])*(?:\\r\s*\{A\}|\\(?:AA|Angstrom|angstrom))"
    r"(?![A-Za-z])\}?\$?"
)
#: Super/subscript runes a duplicated equation rendering glues the next
#: rendering's number against (``…Å⁻¹0.15 eV…``). Only a rune run that
#: actually glues a number — a recognized unit immediately before it, a
#: digit immediately after — is spaced, never deleted (deletion would
#: join the digits into a bogus run, ``10⁻³`` → ``103``). Spacing every
#: rune instead detaches a mantissa from its own exponent and pairs it
#: with whatever unit follows (``10⁻³ eV`` → ``10 eV``, ``10²³ cm⁻³`` →
#: ``10 cm``), fabricating quantities the evidence never stated; reword's
#: grounding canon drops runes and the ``10`` mantissa for the same
#: reason (:data:`precis.taproot.reword._SUPERSCRIPT_DIGITS`).
_GLUE_RUNE_RE = re.compile(
    rf"(?<![\w])({_UNITS_RE_PART})([⁰¹²³⁴⁵⁶⁷⁸⁹⁻₀₁₂₃₄₅₆₇₈₉]+)(?=\d)"
)

#: TeX spacing a math extraction leaves between a value and its unit
#: (``50\ \mathrm{K}``): ``\ ``/``\,``/``\;``/``\:``/``\!`` and a ``~``
#: tie right after a digit (``50~K``) all read as a plain space. A ``~``
#: anywhere else is the ``\sim`` approximation marker (``~4K``) — a
#: temperature cue :data:`_TEMP_CUE_RE` reads, so collapsing it to a
#: space would strip ``4K``'s kelvin reading (review td794577). Commands
#: that take an argument keep their own handlers (``\%`` → ``%``,
#: ``\AA`` → ``Å``).
_TEX_SPACE_RE = re.compile(r"\\[,;:! ]|(?<=\d)~(?!\d)")

#: A doubled MathML/LaTeX equation rendering writes each quantity twice —
#: ``5​GPa5\ \mathrm{GPa}`` (rendered copy then source copy) or
#: ``50\ \mathrm{K}50\ \mathrm{K}`` — and the glue between the copies
#: hides both from the tokenizer (the first unit's ``(?!\w)`` fails on
#: the second copy's number). Two immediately-adjacent spellings of the
#: same ``<number> <unit>`` collapse to the *second* copy verbatim — the
#: source spelling, keeping its own number–unit spacing: ``5​GPa5\
#: \mathrm{GPa}`` → ``5 GPa`` while ``130K130K`` → ``130K`` stays glued,
#: so the count filter (``130K molecules`` is a thousands count, not
#: 130 kelvin — :data:`_GLUED_K_RE`) still sees the shape it keys on
#: (review td794577). Real prose never
#: repeats a quantity with only whitespace between the copies. A *bare*
#: doubled digit run (``00`` for ``0``) is not halved here — reword's
#: grounding canon folds leading zeros, which is the same value, while
#: ``11`` halved to ``1`` would not be. A sentence-ending ``.`` may
#: follow the second copy (``50 K50 K.`` at the end of a clause); only a
#: ``.`` that starts a decimal fraction blocks the match.
_DOUBLED_QUANTITY_RE = re.compile(
    rf"(?<![\w.])(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
    rf"({_UNITS_RE_PART})\s*(\1\s*\2)(?![\w]|\.(?=\d))"
)

#: A markdown table header cell naming its column's unit: ``[Å]`` / ``(K)``;
#: ``T [K]/P [kPa]`` names two, the cells then read ``298/100``.
_HEADER_UNIT_RE = re.compile(rf"[\[(]\s*({_UNITS_RE_PART})\s*[\])]")
_CELL_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_NUMBER_PART_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

#: "10-20 nm", "10–20 nm", "10 to 20 nm": a range sharing one trailing unit.
_RANGE_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)"
    rf"\s*({_UNITS_RE_PART})(?!\w)"
)

#: "130K molecules" is a count, not 130 kelvin: a glued ``K`` counts as a
#: temperature only after a cue ("at 77K", "T = 4K", "below 20K",
#: "~300K"). Grounding and evidence indexing opt into the filter. Reword
#: validation keeps ambiguous tokens so it can catch a unit change.
_GLUED_K_RE = re.compile(r"\d+K")
_TEMP_CUE_RE = re.compile(
    r"(?:\bat|\bT\s*[=~≈<>∼]|\btemperature|\bbelow|\babove|\bto|\bfrom|[~≈<>∼])\s*$",
    re.IGNORECASE,
)
_TEMP_CUE_CHARS = 20


#: Spelling variants of one unit that numerics._UNITS lists separately. No
#: conversion: A (ampere) is deliberately not Å.
_UNIT_CANON = {
    "um": "µm",
    "uA": "µA",
    "uM": "µM",
    "ug": "µg",
    "us": "µs",
    "μm": "µm",  # Greek mu (U+03BC), not the micro sign (U+00B5)
    "μA": "µA",
    "μM": "µM",
    "μg": "µg",
    "μs": "µs",
    "cm−1": "cm-1",
    "Angstrom": "Å",
}


def _list_heads(text: str, spans: list[tuple[str, int, int]]) -> list[str]:
    """Earlier members of a unit-sharing list, as "number unit" tokens:
    "0.44 and 0.26 eV" yields "0.44 eV" for the span "0.26 eV"."""
    out: list[str] = []
    for tok, s, _e in spans:
        _number, unit = _split_token(tok)
        if not unit:
            continue
        sep = "" if unit == "%" else " "
        at = s
        while True:
            m = _LIST_HEAD_RE.search(text, max(0, at - _LIST_HEAD_CHARS), at)
            if m is None:
                break
            out.append(f"{m.group(1)}{sep}{unit}")
            at = m.start(1)
    return out


def _table_quantities(text: str) -> list[str]:
    """Cells of a markdown table paired with the unit their header names:
    header ``Pore aperture [Å]`` over cell ``2.6`` yields "2.6 Å"; header
    ``T [K]/P [kPa]`` over ``298/100`` yields "298 K" and "100 kPa". A unit in the row label
    (``Cutoff [Å]``) covers the row's cells under unit-less headers."""
    out: list[str] = []
    header: list[list[str]] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            header = [_HEADER_UNIT_RE.findall(c) for c in cells]
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        row_units = _HEADER_UNIT_RE.findall(cells[0]) if cells else []
        for i, (units, cell) in enumerate(zip(header, cells, strict=False)):
            if not units and i and len(row_units) == 1:
                # The unit sits in the row label ("Cutoff [Å] | 10.0 | 12.0").
                units = row_units
            if not units:
                continue
            parts = cell.split("/") if len(units) > 1 else [cell]
            for unit, part in zip(units, parts, strict=False):
                if _CELL_NUMBER_RE.fullmatch(part.strip()):
                    unit = _UNIT_CANON.get(unit, unit)
                    out.append(f"{part.strip()}{'' if unit == '%' else ' '}{unit}")
    return out


def _split_token(token: str) -> tuple[str, str]:
    m = _NUMBER_PART_RE.match(token)
    if m is None:
        return token, ""
    return m.group(0), _UNIT_CANON.get(
        token[m.end() :].strip(), token[m.end() :].strip()
    )


def extract_numerics(text: str) -> list[str]:
    """Return ``["<number> <unit>", …]`` for every recognized token.

    Deduplicated case-sensitively (preserving paper casing) and
    ordered by first occurrence. Empty input → ``[]``.

    The output is suitable for direct inclusion in a ``TEXT[]``
    column — the intended query side was ``WHERE numerics @>
    ARRAY['1.523 eV']`` with a GIN index, but that read path never
    materialized, so the unused index was dropped (migration 0118);
    recreate it if a predicate consumer ever lands. For approximate
    matching the agent issues multiple parallel queries (``"1.5 eV"``,
    ``"1.52 eV"``, …); that's the path-2 ergonomics tradeoff we
    accepted for v1.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token, _start, _end in numeric_spans(text):
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def numeric_spans(
    text: str, *, filter_glued_k: bool = False
) -> list[tuple[str, int, int]]:
    """``[(token, start, end), …]`` for every recognized token, in order,
    **not** deduplicated — for callers that need to know *where* a value
    sits (the memory attribution gate). ``token`` is spelled exactly as
    :func:`extract_numerics` spells it. With ``filter_glued_k``, a cue-less
    glued ``K`` such as ``130K molecules`` is treated as a count."""
    if not text:
        return []
    out: list[tuple[str, int, int]] = []
    for m in _NUMERIC_RE.finditer(text):
        if (
            filter_glued_k
            and _GLUED_K_RE.fullmatch(text, m.start(), m.end())
            and not _TEMP_CUE_RE.search(
                text, max(0, m.start() - _TEMP_CUE_CHARS), m.start()
            )
        ):
            continue
        number = m.group(1)
        unit = m.group(2)
        # Special-case ``%`` — print without the space between number
        # and unit ("12%" is more idiomatic than "12 %" in queries).
        token = f"{number}%" if unit == "%" else f"{number} {unit}"
        out.append((token, m.start(), m.end()))
    return out


def _unwrap_math(m: re.Match[str]) -> str:
    inner = m.group(1) or m.group(2) or m.group(3) or ""
    # A single-$ pair padded with spaces is math only if it carries a TeX
    # command or ±; otherwise it is two dollar amounts ("$5 and $10").
    if (
        m.group(2) is not None
        and inner != inner.strip()
        and not re.search(r"[\\±]", inner)
    ):
        return m.group(0)
    return inner.strip()


#: Invisible PDF glue can split a range, decimal, or number/unit pair.
#: Remove it only in the normalized reading; raw span offsets stay intact.
_ZERO_WIDTH_TRANSLATION = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")


def normalize_spellings(text: str) -> str:
    """PDF number spellings the tokenizer misses, rewritten to the plain
    form: "2,000 p.p.m." → "2000 ppm", "$1.78\\pm0.11$  Å" → "1.78 and 0.11 Å"
    (inline math unwrapped, LaTeX ``\\pm``/``\\times``/``\\%``/``^\\circ`` mapped
    to symbols, the ``±`` as a two-member list), "$\\mu$ m" / "μm" → "µm",
    a super/subscript rune run that glues a duplicated equation rendering's
    number to the previous copy's unit (``Å⁻¹0.15`` → ``Å 0.15``) spaced,
    the TeX angstrom macro (``6 \\AA``, ``6\\,\\AA``) → "Å", TeX spacing
    (``50\\ \\mathrm{K}`` → ``50 K``) mapped to a plain space while a
    ``\\sim`` cue ``~`` is preserved (``\\sim4K`` → ``~4K``), and a doubled
    MathML/LaTeX quantity render collapsed to its source copy verbatim
    (``50\\ \\mathrm{K}50\\ \\mathrm{K}`` → ``50 K``, ``130K130K`` →
    ``130K`` — the glued count shape is preserved, not respelled)."""
    norm = _THOUSANDS_RE.sub("", _PPM_RE.sub("ppm", text))
    norm = _TEX_MU_RE.sub("µ", norm)
    norm = norm.translate(_ZERO_WIDTH_TRANSLATION)
    norm = _GLUE_RUNE_RE.sub(r"\1 ", norm)
    norm = _TEX_WRAP_RE.sub(r"\1", norm)
    for rx, sym in _TEX_SYMBOLS:
        norm = rx.sub(sym, norm)
    norm = _MATH_INLINE_RE.sub(_unwrap_math, norm)
    norm = _TEX_ANGSTROM_RE.sub("Å", norm)
    norm = _DEGREE_LIGATURE_RE.sub(lambda m: "°C" if m.group(1) == "℃" else "°F", norm)
    norm = _DEGREE_UNIT_RE.sub("°", norm)
    norm = _DEGREE_LONE_RE.sub("°", norm)
    norm = _TEX_SPACE_RE.sub(" ", norm)
    norm = _DOUBLED_QUANTITY_RE.sub(r"\3", norm)
    norm = _PLUS_MINUS_RE.sub(" and ", norm)
    return norm.replace("μ", "µ")  # Greek mu (U+03BC) → micro sign (U+00B5)


def extract_numerics_wide(text: str) -> list[str]:
    """:func:`extract_numerics` plus the readings the memory attribution gate
    needs from a document-level cite: the raw and :func:`normalize_spellings`
    forms, earlier members of a unit-sharing list ("0.44 and 0.26 eV"),
    range endpoints ("10-20 nm" → "10 nm"), and table cells that take the
    unit from their header. This is what ingest stores in ``chunks.numerics``."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for variant in (text, normalize_spellings(text)):
        spans = numeric_spans(variant, filter_glued_k=True)
        toks = [t for t, _s, _e in spans]
        toks += _list_heads(variant, spans)
        for r in _RANGE_RE.finditer(variant):
            unit = _UNIT_CANON.get(r.group(3), r.group(3))
            sep = "" if unit == "%" else " "
            toks += [f"{r.group(1)}{sep}{unit}", f"{r.group(2)}{sep}{unit}"]
        toks += _table_quantities(variant)
        for t in toks:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


__all__ = [
    "extract_numerics",
    "extract_numerics_wide",
    "normalize_spellings",
    "numeric_spans",
]
