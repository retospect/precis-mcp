"""Stage 1 — deterministic number+unit census over a frozen snapshot.

``taxonomy-bootstrap.md`` §Design, stage 1. The whole reason this module
exists is that the same snapshot must scan to byte-identical output (AC1),
and that "what counts as a unit" is never a hand-maintained word list —
``pint`` decides. That is what keeps the grammar domain-neutral: this file
imports :class:`~precis.taxonomy.config.CampaignConfig` for phrase rules and
extra unit definitions and nothing else campaign-specific.

**Own registry, deliberately.** :func:`build_registry` constructs a private
``pint.UnitRegistry`` and applies ``config.unit_definitions`` to it via
``registry.define(...)``. It does **not** call
``precis.utils.units._registry()`` — that module owns a process-wide
singleton shared with ``cad``/``structsolve``, and a campaign's ``decade``/
``wt%``/molar definitions must never leak into unrelated CAD unit parsing
(see that module's docstring and ``taxonomy/__init__.py``'s "own unit
registry" boundary).

Two things are hard to get right here and worth reading before touching the
regexes:

* **The number grammar is one alternation, ordered by specificity**
  (tolerance, range, scientific-multiply, bare power-of-ten, plain), so that
  e.g. ``10-20`` reads as a range and never as "ten to the power minus
  twenty" — see :data:`_NUMBER_RE` for the ordering rationale.
* **Unit resolution tries pint, not a word list**, but pint's own registry
  defines a handful of single-letter/short English words as units (``in``
  → inch, ``a`` → year, ``at`` → atmosphere, ``as`` → attosecond, ``are`` →
  the area unit) — see :data:`_AMBIGUOUS_ENGLISH_STOPWORDS`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

import pint

from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.types import Anchor, Mention, Snapshot

# ── number grammar ──────────────────────────────────────────────────
#
# No domain knowledge lives here — only "what does a number look like in
# running text", including the English convention for thousands and the
# handful of ways papers write scientific notation.

#: Leading sign. Deliberately excludes ``+`` — nothing in the AC list asks
#: for a leading plus on a plain number, and admitting it would make the
#: tolerance separator ``+/-`` ambiguous with "a plus-signed number followed
#: by a bare minus".
_SIGN: Final[str] = r"[\-−]?"

#: English-convention thousands grouping only (``1,234``, ``12,345.6``). A
#: comma as a *decimal* separator (the other convention used worldwide) is
#: ambiguous with this one from the string alone — a lone ``1,23`` could be
#: "one thousand, two hundred (typo)" or "1.23" — so it is out of scope; the
#: three-digit-group requirement (`\d{3}` after each comma) is exactly what
#: keeps this from also matching that other convention by accident.
_NUM_CORE: Final[str] = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"

#: ASCII/upper-case e-notation glued directly onto the mantissa:
#: ``1.2e-3``, ``1.2E+3``.
_EXP_TAIL: Final[str] = r"(?:[eE][\-−+]?\d+)?"

#: One plain (possibly signed, possibly e-notated) decimal.
_PLAIN_NUM: Final[str] = rf"{_SIGN}{_NUM_CORE}{_EXP_TAIL}"

#: Tolerance separator: ``±`` or the ASCII spelling ``+/-``.
_TOLERANCE: Final[str] = rf"{_PLAIN_NUM}\s*(?:±|\+/-)\s*{_PLAIN_NUM}"

#: Range separator: en dash or hyphen with optional surrounding spaces
#: (``1.2–3.4``, ``10-20``), or the word ``to`` (``1.2 to 3.4``). The
#: Unicode minus ``−`` is deliberately *not* a range separator here — it is
#: reserved for numeric sign/exponent use, and admitting it would make
#: ``1.2−3.4`` ambiguous between "range" and "1.2, then a separately signed
#: -3.4" in a way the ASCII/en-dash forms are not.
_RANGE_SEP: Final[str] = r"(?:\s*[–-]\s*|\s+to\s+)"
_RANGE: Final[str] = rf"{_PLAIN_NUM}{_RANGE_SEP}{_PLAIN_NUM}"

#: A mantissa times a power of ten written out: ``1.2 × 10−3``, ``1.2 x
#: 10^-3``, ``1.2 × 10⁻³``. The ``×``/``x`` token is what makes this
#: unambiguous — without it, a bare ``10-3`` glued exponent would collide
#: with the range grammar above (see :data:`_BARE_POWER_OF_TEN`).
_SCI_MULT: Final[str] = (
    rf"{_PLAIN_NUM}\s*[×x]\s*10"
    r"(?:\^[\-−]?\d+|[\-−]\d+|[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
)

#: A bare power of ten with no mantissa (``10⁻³``, ``10^-3``) — one of the
#: grammar's required scientific forms. Deliberately excludes the bare
#: ``10-3`` (ascii-minus, no caret, no superscript) spelling: that spelling
#: is indistinguishable from the range ``10-3`` ("ten to three") without
#: more context, and the range reading wins (`_RANGE` is tried first).
_BARE_POWER_OF_TEN: Final[str] = r"10(?:\^[\-−]?\d+|[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)"

#: Ordered most-specific-first: each later alternative is a strict fallback
#: for text the earlier ones don't match, and Python's ``re`` alternation
#: takes the first alternative that matches at a given position rather than
#: the longest, so this order *is* the disambiguation rule.
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    "|".join([_TOLERANCE, _RANGE, _SCI_MULT, _BARE_POWER_OF_TEN, _PLAIN_NUM])
)

# ── unit resolution ──────────────────────────────────────────────────
#
# pint is the arbiter of what is a unit; this module supplies no unit word
# list, only a character class for "what a unit token can look like" and a
# short normalisation pass so pint's own grammar (which wants ``cm ** -2``
# or ``cm^-2``, not the bare ``cm-2``/``cm−2`` papers actually write) has
# something it accepts.

#: One character that can appear inside a unit token: letters (incl. the
#: handful of unit-bearing symbols pint's default registry does not treat
#: as plain letters), digits, ``%``, ``°``, ``/``, the two dot-product
#: glyphs, ``*``/``^``/``-``/``−`` for exponents/products, and the
#: superscript digit block.
_UNIT_CHAR: Final[str] = "A-Za-zΩµÅ°%/·⋅*\\^\\-−0-9⁰¹²³⁴⁵⁶⁷⁸⁹⁻"
#: A run of unit characters with *at most single interior spaces*
#: (``mA cm−2``, ``M KOH``) — the continuation branch matches a literal
#: ASCII space only (not ``\s``, which would also swallow a newline and
#: run straight into the next sentence/paragraph), the lookahead on that
#: branch stops the run from ending on a trailing space, and the
#: ``{0,23}`` cap plus the mandatory first character gives the ~24-char
#: ceiling from the spec.
_UNIT_RUN_RE: Final[re.Pattern[str]] = re.compile(
    rf"[{_UNIT_CHAR}](?:[{_UNIT_CHAR}]| (?=[{_UNIT_CHAR}])){{0,23}}"
)

#: The whole of the text allowed between two numbers for the first to
#: borrow the second's unit (``taxonomy-bootstrap.md`` blocker 3b: ``10 and
#: 30 mA cm−2`` gives ``10`` no unit of its own because the unit sits after
#: the second number). A list or range connective and nothing else — a
#: comma, ``and``/``or``/``to``, a dash — so ``2 electrons and 3 h`` does
#: not hand ``2`` an hour. Anchored on both ends by the caller (fullmatch on
#: the gap), so the connective must be the entire gap.
_SHARED_UNIT_GAP_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:,|,?\s*(?:and|or|to)|[-–—−])\s*"
)

#: Superscript digit/minus block → plain-ASCII translation, used to turn a
#: run like ``⁻²`` into ``^-2`` (pint's ``**``/``^`` grammar) rather than
#: leaving it as a glyph pint's ``Unit()`` cannot parse at all.
_SUPERSCRIPT_MAP: Final[dict[str, str]] = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "⁻": "-",
}
_SUPERSCRIPT_RUN_RE: Final[re.Pattern[str]] = re.compile(
    "[" + "".join(_SUPERSCRIPT_MAP) + "]+"
)

#: After the literal substitutions, a bare ``-3`` glued to a preceding unit
#: letter (``cm-2``, ``s-1``, ``dec-1`` once ``−`` has already become ``-``)
#: is *not* something pint's ``Unit()``/``parse_expression`` accepts on its
#: own — both read it as subtraction, not an exponent (verified against
#: pint 0.23+: ``Unit("cm-2")`` raises, ``Unit("cm^-2")`` parses). Papers
#: routinely omit the caret, so this inserts one before any minus that is
#: not already preceded by ``^`` and is immediately followed by a digit.
#: This is beyond the literal glyph-substitution list in the spec but is
#: required for AC3's ``cm−2``/``s−1``/``dec−1`` cases to resolve at all.
_BARE_EXPONENT_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\^)-(?=\d)")

#: Tokens pint's *default* registry happens to define as units that are
#: overwhelmingly English function words in running prose — accepting them
#: would turn ``"3 in the cell"`` into "3 inches" and ``"1664 papers, a
#: yield of..."`` into treating a stray ``a`` as a year. Checked against the
#: campaign registry (`docs/backlog/taxonomy-bootstrap.md` census-tokenizer
#: spec): ``in``→inch, ``a``→year, ``at``→atmosphere, ``as``→attosecond,
#: ``are``→the area unit are pint's actual traps; the rest of this list is
#: the spec's "at minimum" set, kept for defensiveness against a campaign
#: registry that later defines more of them. ``s`` is deliberately absent —
#: it is a real, extremely common unit (seconds) and stopping it would
#: break far more than it protects.
_AMBIGUOUS_ENGLISH_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "in",
        "a",
        "as",
        "at",
        "is",
        "are",
        "of",
        "to",
        "or",
        "and",
        "the",
        "for",
        "by",
        "on",
        "no",
        "not",
        "each",
        "per",
    }
)

#: pint exceptions that mean "this candidate is not a unit" rather than a
#: real bug. ``TypeError``/``ValueError``/``SyntaxError``/``AttributeError``
#: are included because pint's parser raises those (not just its own
#: exception types) on malformed input — e.g. ``Unit("cm-2")`` raises a bare
#: ``TypeError`` internally, not ``pint.PintError``. ``AssertionError`` is
#: included for the same reason: pint's expression-tree builder hits a bare
#: ``assert result is not None`` on a lone trailing operator (a candidate
#: like ``"-"``, e.g. from "NO3−" in running prose — a real corpus hit, not
#: a fixture) rather than raising a parse error of its own.
#: ``ZeroDivisionError`` is the same story for a candidate like ``"V/0"``
#: (from "0.003 V/0.39 V" — two adjacent values sharing a slash in prose,
#: not a real compound unit): pint tries to fold the literal ``0`` into a
#: unit scale and divides by it.
_UNIT_PARSE_ERRORS: Final[tuple[type[Exception], ...]] = (
    pint.PintError,
    TypeError,
    ValueError,
    SyntaxError,
    AttributeError,
    AssertionError,
    ZeroDivisionError,
)

# ── reference-state / currency grammar ──────────────────────────────

#: The library-level fallback reference-state phrase (`taxonomy-bootstrap.md`
#: §Design, stage 1): a campaign that has not enumerated every electrode
#: still gets a visible, ``marker=None`` mention rather than silently
#: dropping the reference. Token grammar is deliberately loose (letters,
#: digits, ``/``, ``+``, ``-``) so compound electrode names like
#: ``Zn/Zn2+`` come through as one token.
_GENERIC_REFERENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"vs\.?\s+[A-Za-z][\w/+-]{0,20}", re.IGNORECASE
)

#: Currency symbols this module recognises directly (glued to the number,
#: either side). An unrecognised symbol is not an error — it simply never
#: matches here, so the mention falls through to the ordinary pint unit
#: scan (and typically resolves to ``raw_unit=None``, since pint has no
#: opinion on currency symbols either).
_CURRENCY_SYMBOLS: Final[dict[str, str]] = {"$": "USD", "€": "EUR", "£": "GBP"}

#: A currency-code word glued or single-space-separated from the number
#: (``USD 1.2``, ``1.2 USD``). 2-6 letters is generous enough for every ISO
#: 4217 code without also swallowing an adjacent ordinary word.
_CURRENCY_WORD_AFTER_RE: Final[re.Pattern[str]] = re.compile(r" ?([A-Za-z]{2,6})\b")
_CURRENCY_WORD_BEFORE_RE: Final[re.Pattern[str]] = re.compile(r"([A-Za-z]{2,6}) ?$")

#: A bare 4-digit year — the trigger for the ``price-base-year`` mention
#: when it sits directly before a currency code (``2015 USD``).
_FOUR_DIGIT_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"\d{4}")


def build_registry(config: CampaignConfig) -> pint.UnitRegistry:
    """A fresh, campaign-scoped ``pint`` registry.

    Deliberately **not** ``precis.utils.units``'s process-wide singleton —
    see the module docstring's "own registry, deliberately" note. Every
    stage-1 call site should build (or receive) one of these rather than
    reach for that module.
    """
    registry = pint.UnitRegistry()
    for definition in config.unit_definitions:
        registry.define(definition)
    return registry


def scan_text(
    text: str,
    ref_id: int,
    config: CampaignConfig,
    registry: pint.UnitRegistry,
) -> tuple[Mention, ...]:
    """Scan one piece of text (one hub/chunk) for mentions.

    Three independent passes over the same text — numbers (which may
    resolve to a currency instead of a pint unit), campaign + generic
    reference-state phrases, and campaign normalisation-basis phrases —
    merged and sorted by ``(ref_id, start, end, kind)`` so the result does
    not depend on pass order (AC1).
    """
    mentions: list[Mention] = []

    # A number's accepted unit run can itself contain digits that
    # `_NUMBER_RE` also matches on its own (the "2" in "cm−2"'s exponent).
    # `blocked_until` is how those ghost matches are suppressed: once a
    # number's unit run has been resolved, nothing that starts before the
    # end of the *accepted* candidate gets its own mention — only the
    # accepted span, never the over-eager initial capture (see
    # `_resolve_unit`), so an unrelated number just beyond a *failed* unit
    # guess is still found.
    blocked_until = 0
    for match in _NUMBER_RE.finditer(text):
        start, end = match.span()
        if start < blocked_until:
            continue
        if start > 0 and text[start - 1].isalnum():
            # A number glued directly onto a preceding letter/digit is not
            # a measurement — it is part of a larger token (a formula
            # subscript, a material code, an identifier): "NO3RR", "Ti3C2",
            # "H2 evolution", "g-C3N4"'s "3"/"4" (the sign in the match
            # itself, if any, is what sits at `start`; the character just
            # before *that* is what is checked — "NZT-1"'s "-1" starts
            # with "-", but the "T" right before it is still the boundary
            # this rejects). Domain-neutral: identical rule for a
            # sociology corpus's "COVID19" or "Q3".
            continue
        literal = match.group(0)
        if _is_parenthesised_identifier(text, start, end, literal):
            # "(111)", "(0001)", "(001)": a parenthesised group whose
            # entire content is a bare digit run of three or more — a
            # Miller index, a cycle/entry label, a bare year — is an
            # identifier, not a value. Nothing follows the digits inside
            # the parentheses, so no unit could have resolved for it, and
            # the discovery stage otherwise mints one "crystallographic
            # facet" measurand per such surface (21 of 204 discovered rows
            # on the first norr-her-meta probe). "(204 kJ/mol)" is left
            # alone: the closing parenthesis is not directly after the
            # digits.
            continue
        context = _context(text, start, end)
        currency = _detect_currency(text, start, end, config)
        if currency is not None:
            code, is_suffix_code = currency
            mentions.append(
                Mention(
                    anchor=Anchor(ref_id, start, end),
                    kind="value",
                    literal=literal,
                    raw_unit=code,
                    context=context,
                )
            )
            if is_suffix_code and _FOUR_DIGIT_YEAR_RE.fullmatch(literal):
                # A bare year directly in front of a currency code ("2015
                # USD") is a price base year, not just a value — a price
                # figure without one is not comparable across papers.
                mentions.append(
                    Mention(
                        anchor=Anchor(ref_id, start, end),
                        kind="reference_state",
                        literal=literal,
                        raw_unit=None,
                        context=context,
                        marker="price-base-year",
                    )
                )
            blocked_until = max(blocked_until, end)
            continue
        raw_unit, consumed_end = _resolve_unit(text, end, registry, config)
        mentions.append(
            Mention(
                anchor=Anchor(ref_id, start, end),
                kind="value",
                literal=literal,
                raw_unit=raw_unit,
                context=context,
            )
        )
        blocked_until = max(blocked_until, consumed_end)
    _share_units(text, mentions)

    claimed_spans: list[tuple[int, int]] = []
    for rule in config.reference_states:
        for rmatch in rule.pattern.finditer(text):
            start, end = rmatch.span()
            claimed_spans.append((start, end))
            mentions.append(
                Mention(
                    anchor=Anchor(ref_id, start, end),
                    kind="reference_state",
                    literal=rmatch.group(0),
                    raw_unit=None,
                    context=_context(text, start, end),
                    marker=rule.id,
                )
            )
    for gmatch in _GENERIC_REFERENCE_RE.finditer(text):
        start, end = gmatch.span()
        if any(start < ce and cs < end for cs, ce in claimed_spans):
            continue  # a campaign rule already named this exact electrode
        mentions.append(
            Mention(
                anchor=Anchor(ref_id, start, end),
                kind="reference_state",
                literal=gmatch.group(0),
                raw_unit=None,
                context=_context(text, start, end),
                marker=None,
            )
        )

    for basis_rule in config.normalisation_bases:
        for bmatch in basis_rule.pattern.finditer(text):
            start, end = bmatch.span()
            mentions.append(
                Mention(
                    anchor=Anchor(ref_id, start, end),
                    kind="normalisation_basis",
                    literal=bmatch.group(0),
                    raw_unit=None,
                    context=_context(text, start, end),
                    marker=basis_rule.id,
                )
            )

    mentions.sort(
        key=lambda m: (m.anchor.source_ref_id, m.anchor.start, m.anchor.end, m.kind)
    )
    return tuple(mentions)


def scan_snapshot(
    rows: Iterable[Mapping[str, object]],
    config: CampaignConfig,
    *,
    registry: pint.UnitRegistry | None = None,
) -> tuple[Mention, ...]:
    """Scan every row of a snapshot. Same input, same output (AC1) — the
    final sort is over the *whole* result, not per row, so this is
    independent of the iteration order ``rows`` happens to supply.
    """
    reg = registry if registry is not None else build_registry(config)
    mentions: list[Mention] = []
    for row in rows:
        ref_id = _coerce_ref_id(row[config.snapshot.ref_field])
        text = str(row.get(config.snapshot.text_field) or "")
        mentions.extend(scan_text(text, ref_id, config, reg))
    mentions.sort(
        key=lambda m: (m.anchor.source_ref_id, m.anchor.start, m.anchor.end, m.kind)
    )
    return tuple(mentions)


def load_snapshot_rows(path: Path) -> tuple[dict[str, object], ...]:
    """Read a frozen snapshot dump. JSONL, one row per line; a line not
    starting with ``{`` (blank line, stray log output) is skipped rather
    than raising, matching the shape of the hand-written pilot dumps this
    replaces.
    """
    raw = path.expanduser().read_text(encoding="utf-8")
    rows: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        rows.append(json.loads(line))
    return tuple(rows)


def census_digest(mentions: Sequence[Mention]) -> str:
    """The sha256 hex digest AC1's "byte-identical output" is checked
    against — canonical JSON (sorted keys, ASCII-only, no incidental
    whitespace) over each mention's own ``to_json()``, so the digest is
    stable across process/interpreter and never depends on dict insertion
    order.
    """
    payload = json.dumps(
        [m.to_json() for m in mentions],
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_snapshot(path: Path, snapshot: Snapshot) -> None:
    """Raise if the file at ``path`` no longer matches ``snapshot``'s
    pinned identity (row count, sha256) — the check every stage-1 run
    should make before trusting a snapshot record it did not just produce
    itself.
    """
    resolved = path.expanduser()
    raw = resolved.read_bytes()
    row_count = len(load_snapshot_rows(resolved))
    if row_count != snapshot.row_count:
        raise ValueError(
            f"snapshot row_count mismatch for {resolved}: "
            f"pinned {snapshot.row_count}, found {row_count}"
        )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != snapshot.sha256:
        raise ValueError(
            f"snapshot sha256 mismatch for {resolved}: "
            f"pinned {snapshot.sha256}, found {digest}"
        )


# ── internals ────────────────────────────────────────────────────────


def _coerce_ref_id(raw: object) -> int:
    if isinstance(raw, bool):  # bool is an int subclass — reject before it
        raise TypeError(f"ref_id cannot be a boolean: {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        return int(raw)
    raise TypeError(f"unsupported ref_id type: {type(raw)!r}")


def _is_parenthesised_identifier(text: str, start: int, end: int, literal: str) -> bool:
    """``(111)``-shaped: the number is the *whole* content of a
    parenthesised group and is a plain digit run of length >= 3 (no sign,
    no decimal separator, no exponent). Such a group names something — a
    Miller index, a label — rather than measuring it.
    """
    return (
        len(literal) >= 3
        and literal.isdigit()
        and start > 0
        and text[start - 1] == "("
        and end < len(text)
        and text[end] == ")"
    )


def _context(text: str, start: int, end: int) -> str:
    """±60 raw characters, whitespace (including any newline) collapsed to
    single spaces — deterministic and never longer than ~121 chars.
    """
    lo = max(0, start - 60)
    hi = min(len(text), end + 60)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def _normalize_unit_candidate(raw: str) -> str:
    """Turn what a paper actually wrote into what pint's grammar accepts,
    without changing what it *means*. See the module docstring and
    :data:`_BARE_EXPONENT_RE` for why this goes one step past the literal
    glyph list.
    """
    text = _SUPERSCRIPT_RUN_RE.sub(
        lambda m: "^" + "".join(_SUPERSCRIPT_MAP[c] for c in m.group(0)), raw
    )
    text = text.replace("·", " ").replace("⋅", " ")
    text = text.replace("−", "-")
    text = text.replace("×", "x")
    text = _BARE_EXPONENT_RE.sub("^-", text)
    return re.sub(r" {2,}", " ", text).strip()


def _resolve_unit(
    text: str, pos: int, registry: pint.UnitRegistry, config: CampaignConfig
) -> tuple[str | None, int]:
    """The candidate-shrinking loop AC3/AC4 depend on: capture the unit-ish
    run after a number, then try registry.Unit() on successively shorter
    *token* prefixes (longest first, token boundaries from the ORIGINAL
    text's single interior spaces) until one parses. ``raw_unit`` is
    exactly the (normalised) string pint accepted — never a guess, and
    never the original glyphs if pint only accepted them after
    normalisation.

    Returns ``(raw_unit, consumed_end)``. ``consumed_end`` is the source
    offset just past the *accepted* candidate (or ``pos`` if nothing
    parsed) — never the initial run's own, possibly much longer, capture.
    The run's "single interior space" rule means it can run on past the
    unit into unrelated following words (``"3 h at 25 °C"`` captures all
    the way to "°C"); the shrinking loop finds "h" as the winning *token*
    prefix, and reporting only that prefix's end keeps the following
    number (the "25") from being silently swallowed as unconsumed text a
    caller might otherwise skip.

    The ambiguous-English-stopword guard (:data:`_AMBIGUOUS_ENGLISH_STOPWORDS`)
    truncates the token list at the *first* stopword found at any position,
    not only a leading one: without this, "h at 25 °C" would try the whole
    run, fail (a bare "25" is not a unit), then shrink to "h at" — pint
    happily parses that as hour·atmosphere, a real unit with no relationship
    to what was written. Truncating before "at" ever enters a candidate
    means the loop only ever tries "h", which is what the text actually
    means.

    ``config.glued_unit_denylist`` is consulted only for a candidate with no
    space before it (``"2D"``, not ``"0.5 D"``) — pint's SI-prefix machinery
    turns a bare campaign-specific label into a unit (``D`` → debye,
    ``R`` → the gas constant), and a *glued* single-letter label is
    overwhelmingly that, not a measurement with that unit. The spaced form
    is left alone because that spacing is how a real quantity in that unit
    is actually written.
    """
    glued = not (pos < len(text) and text[pos] == " ")
    start = pos if glued else pos + 1
    run_match = _UNIT_RUN_RE.match(text, start)
    if run_match is None:
        return None, pos
    raw_tokens = run_match.group(0).split(" ")
    if not raw_tokens or not raw_tokens[0]:
        return None, pos
    for i, token in enumerate(raw_tokens):
        if _normalize_unit_candidate(token) in _AMBIGUOUS_ENGLISH_STOPWORDS:
            raw_tokens = raw_tokens[:i]
            break
    if not raw_tokens:
        return None, pos  # "3 in the cell" must not yield inches

    token_ends: list[int] = []
    offset = start
    for token in raw_tokens:
        offset += len(token)
        token_ends.append(offset)
        offset += 1  # the single separating space; unused past the last token

    for width in range(len(raw_tokens), 0, -1):
        candidate = _normalize_unit_candidate(" ".join(raw_tokens[:width]))
        if glued and candidate in config.glued_unit_denylist:
            continue
        try:
            registry.Unit(candidate)
        except _UNIT_PARSE_ERRORS:
            continue
        return candidate, token_ends[width - 1]
    return None, pos


def _share_units(text: str, mentions: list[Mention]) -> None:
    """Blocker 3b — a unit-less number in a list borrows the next number's unit.

    ``current densities of 10 and 30 mA cm−2`` resolves a unit for ``30``
    only; without this pass ``10`` is a real measurement carrying no unit,
    which stage 3 then files as a dimensionless node of its own — worse
    than a label, because a label is obviously junk and this is not.

    The rule: a ``value`` mention with no unit, whose gap to the *next*
    ``value`` mention is exactly a list/range connective
    (:data:`_SHARED_UNIT_GAP_RE`), takes that next mention's unit. The pass
    walks the list from the end so a chain (``10, 30 and 100 mA cm−2``)
    propagates: ``30`` borrows from ``100`` first, then ``10`` from ``30``.
    A borrowed unit is marked ``marker="shared-unit"`` so the provenance
    stays visible in the dumps — the token was never adjacent to the number.

    Mutates ``mentions`` in place (only ``value`` rows, only the ones with
    no unit), in text order, so the census stays deterministic (AC1).
    Currency figures and identifier-skipped numbers never reach this list
    as unit-less values, so they cannot borrow.
    """
    values = [i for i, m in enumerate(mentions) if m.kind == "value"]
    for position in range(len(values) - 2, -1, -1):
        here, following = mentions[values[position]], mentions[values[position + 1]]
        if here.raw_unit is not None or following.raw_unit is None:
            continue
        gap = text[here.anchor.end : following.anchor.start]
        if not gap or _SHARED_UNIT_GAP_RE.fullmatch(gap) is None:
            continue
        mentions[values[position]] = replace(
            here, raw_unit=following.raw_unit, marker="shared-unit"
        )


def _detect_currency(
    text: str, start: int, end: int, config: CampaignConfig
) -> tuple[str, bool] | None:
    """Whether the number at ``[start, end)`` is a currency figure.

    Returns ``(code, is_suffix_code)`` — ``is_suffix_code`` is what gates
    the ``price-base-year`` check, which only makes sense for the "year
    directly before a code" spelling (``2015 USD``), not a symbol or a
    code written before the number.
    """
    if start > 0 and text[start - 1] in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[text[start - 1]], False
    if end < len(text) and text[end] in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[text[end]], False
    before = _CURRENCY_WORD_BEFORE_RE.search(text[:start])
    if before and before.group(1).upper() in config.currency_codes:
        return before.group(1).upper(), False
    after = _CURRENCY_WORD_AFTER_RE.match(text, end)
    if after and after.group(1).upper() in config.currency_codes:
        return after.group(1).upper(), True
    return None
