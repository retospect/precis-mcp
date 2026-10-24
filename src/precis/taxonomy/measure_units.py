"""Pure helpers behind ``insert_measure``: parse a printed literal, normalise a
printed unit, convert it to a measurand's canonical unit, compute a molar mass.

No DB, no I/O. Unit text goes through the census normaliser
(:func:`precis.taxonomy.census._normalize_unit_candidate`: Unicode
superscripts, ``·``/``⋅``, ``−``, a caret before a bare exponent) and then
``pint``. Three things are added here:

* a **basis label** on a unit token — ``mg_cat⁻¹`` (per catalyst mass),
  ``mg_Fe⁻¹`` (per metal mass) — is stripped before pint sees it and returned
  separately, to be stored as the measure's ``normalization``;
* a **mass <-> amount bridge** through a molar mass, for yield rates printed
  as ``µg h⁻¹ cm⁻²`` against a ``mol s⁻¹ m⁻²`` measurand;
* a refusal naming both units when no dimension match exists.

**Store SI, convert at the edges.** The display half (:func:`display_numbers`,
:func:`format_value`, :func:`to_canonical`) turns a stored canonical (SI) value
into the unit a person expects: an explicit ``unit=``, else the taxon's
``display_unit``, else the canonical unit with a pint ``to_compact()`` prefix
(1.4e-10 m prints ``140 pm``, never ``0.00000000014 m``). pH and dB are
logarithmic and never convert or take a prefix (pint would read ``pH`` as a
petahenry); a dimensionless canonical (a fraction) prints bare, and ``%``
appears only when the display unit says so; affine units (°C) convert as
absolute temperatures.

``pint`` does not know ``%`` as a unit symbol in every build, so the
canonical-unit strings ``%`` / ``percent`` / ``mol%`` are mapped to ``percent``
up front.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import pint
from ase.data import atomic_masses, atomic_numbers, chemical_symbols
from ase.formula import Formula

from precis.errors import BadInput
from precis.taxonomy.census import _normalize_unit_candidate
from precis.utils.text import markup_surface, markup_unit

_UREG: pint.UnitRegistry | None = None

#: pint parse failures that mean "not a unit" (see ``census._UNIT_PARSE_ERRORS``).
_PARSE_ERRORS: Final[tuple[type[Exception], ...]] = (
    pint.PintError,
    TypeError,
    ValueError,
    SyntaxError,
    AttributeError,
    AssertionError,
    ZeroDivisionError,
)

#: ``mg_cat`` -> label ``cat``. The label is letters then letters/digits, so it
#: stops at a Unicode superscript, a caret or a minus.
_BASIS_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[A-Za-zµμ])_([A-Za-z][A-Za-z0-9]*)"
)

_SUBSCRIPT_DIGITS: Final[dict[int, str]] = {
    ord(c): str(i) for i, c in enumerate("₀₁₂₃₄₅₆₇₈₉")
}

#: Logarithmic or scale units: never converted, never given an SI prefix. pint
#: parses ``pH`` as petahenry, so they are recognised by name before pint.
_LOG_UNITS: Final[frozenset[str]] = frozenset({"ph", "poh", "pka", "db"})

#: The dashes print uses for a minus: hyphen, non-breaking hyphen, figure
#: dash, en and em dash, horizontal bar and the true minus sign. Folded to
#: ``-`` wherever a sign is read — chunks print these routinely.
MINUS_CHARS: Final[str] = "‐‑‒–—―−"
MINUS_FOLD: Final[dict[int, str]] = {ord(c): "-" for c in MINUS_CHARS}

#: A leading dash of any print form, optionally followed by spaces
#: (``– 1.0``, ``−0.5``, ``‒3``, ``— 5``), folded to a plain minus.
_LEAD_MINUS_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[-{re.escape(MINUS_CHARS)}]+\s*"
)

_NUM: Final[str] = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_POINT_RE: Final[re.Pattern[str]] = re.compile(rf"^({_NUM})$")
_ERR_RE: Final[re.Pattern[str]] = re.compile(rf"^({_NUM})\s*(?:±|\+/-|\+-)\s*({_NUM})$")
_UPPER_RE: Final[re.Pattern[str]] = re.compile(rf"^(?:<=|≤|<)\s*({_NUM})$")
_LOWER_RE: Final[re.Pattern[str]] = re.compile(rf"^(?:>=|≥|>)\s*({_NUM})$")
_APPROX_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:~|≈|∼|approx\.?|about)\s*({_NUM})$"
)
_INTERVAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^({_NUM})\s*(?:–|—|\bto\b|(?<=\d)\s-\s|(?<=\d)-(?=\d|\.))\s*({_NUM})$"
)


@dataclass(frozen=True, slots=True)
class ParsedLiteral:
    """The derived reading of a printed literal. ``form`` is the
    ``measures.value_form`` value; ``num`` carries the bound for a
    one-sided form and is NULL for an interval (``low``/``high`` hold it)."""

    form: str
    num: float | None = None
    low: float | None = None
    high: float | None = None
    err: float | None = None
    text: str | None = None
    flag: bool | None = None


def parse_literal(literal: str) -> ParsedLiteral:
    """``9.6 ± 1.7`` -> point 9.6 err 1.7; ``<1`` -> upper_bound; ``550–575``
    -> interval; ``~3`` -> approximate_point; ``true``/``false`` -> boolean;
    a bare number -> point; anything else -> categorical (text = literal).
    The literal is markup-folded first so a verbatim-copied
    ``$-1.8 \\pm 0.1$`` still parses numeric — a markup literal left
    unparsed would read as categorical and skip the unit dimension
    checks (gr511458)."""
    s = _LEAD_MINUS_RE.sub("-", markup_surface(literal)).translate(MINUS_FOLD)
    s = s.replace(" ", " ")
    if (m := _ERR_RE.match(s)) is not None:
        return ParsedLiteral("point", num=float(m.group(1)), err=abs(float(m.group(2))))
    if (m := _UPPER_RE.match(s)) is not None:
        return ParsedLiteral("upper_bound", num=float(m.group(1)))
    if (m := _LOWER_RE.match(s)) is not None:
        return ParsedLiteral("lower_bound", num=float(m.group(1)))
    if (m := _APPROX_RE.match(s)) is not None:
        return ParsedLiteral("approximate_point", num=float(m.group(1)))
    if (m := _POINT_RE.match(s)) is not None:
        return ParsedLiteral("point", num=float(m.group(1)))
    if (m := _INTERVAL_RE.match(s)) is not None:
        a, b = float(m.group(1)), float(m.group(2))
        return ParsedLiteral("interval", low=min(a, b), high=max(a, b))
    if s.lower() in ("true", "false"):
        return ParsedLiteral("boolean", flag=s.lower() == "true")
    return ParsedLiteral("categorical", text=literal.strip())


def _registry() -> pint.UnitRegistry:
    global _UREG
    if _UREG is None:
        _UREG = pint.UnitRegistry()
    return _UREG


def is_log_unit(unit: str | None) -> bool:
    """pH, pOH, pKa, dB: a scale, not a scalable unit."""
    return unit is not None and unit.strip().casefold() in _LOG_UNITS


def split_basis_label(raw_unit: str) -> tuple[str, str | None]:
    """Strip a subscript basis label (``mg_cat⁻¹`` -> ``mg⁻¹``, ``'cat'``).
    Only the first label is returned; every ``_label`` is removed."""
    labels = _BASIS_LABEL_RE.findall(raw_unit)
    return _BASIS_LABEL_RE.sub("", raw_unit), (labels[0] if labels else None)


def basis_normalization(label: str) -> str:
    """The ``measures.normalization`` text for a unit basis label."""
    if label.lower() in ("cat", "catalyst"):
        return "per catalyst mass"
    sym = label[:1].upper() + label[1:].lower()
    if sym in chemical_symbols[1:]:
        return f"per metal mass ({sym})"
    return f"per {label}"


#: A greenhouse-gas qualifier on a per-tonne / per-year unit — 'Gt CO2 yr-1',
#: 'USD/t CO2-eq', 't CH4' — is the *what*, not the unit. It is stripped
#: before pint sees the text and lands as the row's ``gas`` condition
#: (gr512368). A bare 'C' counts only beside a mass token: 'Gt C' is
#: gigatonnes of carbon but 'A C' is ampere-coulombs.
_GAS_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.])(CO2-?eq|CO2e|CO2|CH4|N2O)(?![\w.])", re.IGNORECASE
)
_MASS_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.])(?:Gt|Mt|kt|kg|g|mg|t|ton|tonne)(?![\w.])"
)


def _strip_gas_qualifier(unit_text: str) -> tuple[str, str | None]:
    """``'Gt CO2 yr^-1'`` -> ``('Gt yr^-1', 'CO2')``."""
    match = _GAS_TOKEN_RE.search(unit_text)
    if match is None and _MASS_TOKEN_RE.search(unit_text):
        match = re.search(r"(?<![\w.])C(?![\w.])", unit_text)
    if match is None:
        return unit_text, None
    gas = match.group(1)
    text = (unit_text[: match.start()] + " " + unit_text[match.end() :]).strip()
    return re.sub(r" {2,}", " ", text), gas


def _pint_unit_ex(raw_unit: str) -> tuple[str, str | None, str | None]:
    """``(text pint can parse, basis label, gas qualifier)``."""
    stripped, label = split_basis_label(markup_unit(raw_unit.strip()))
    text = _normalize_unit_candidate(stripped)
    text, gas = _strip_gas_qualifier(text)
    if text.replace(" ", "") in ("%", "percent", "mol%"):
        # mol% is a percent of an amount fraction: dimensionless like %, which
        # pint would read as mol * percent (the legacy catalyst_loading unit)
        text = "percent"
    else:
        text = text.replace("%", " percent ")
    return text, label, gas


def pint_unit_text(raw_unit: str) -> tuple[str, str | None]:
    """``(text pint can parse, basis label or None)`` for a printed unit.
    Markup is folded first (:func:`precis.utils.text.markup_unit` —
    ``W m^{-2}``/``m<sup>−</sup><sup>2</sup>`` -> ``W m^-2``,
    ``<sub>`` -> ``_`` keeps basis labels) so the spelling a paper's
    extraction left behind still reaches pint (gr511458); a gas qualifier
    is stripped the same way (:func:`_pint_unit_ex`)."""
    text, label, _gas = _pint_unit_ex(raw_unit)
    return text, label


def _parse_unit(
    raw_unit: str, *, what: str, delta: bool = False
) -> tuple[Any, str | None, str | None]:
    text, label, gas = _pint_unit_ex(raw_unit)
    if delta:
        text = _delta_unit_text(text)
    try:
        return _registry().Unit(text), label, gas
    except _PARSE_ERRORS as exc:
        raise BadInput(
            f"{what} unit {raw_unit!r} is not a unit pint can parse",
            next="state the unit as printed, e.g. 'µg h⁻¹ cm⁻²'; a basis label goes "
            "after an underscore: 'mg_cat⁻¹'",
        ) from exc


def molar_mass(formula: str) -> float | None:
    """g/mol of a chemical formula (``NH3``, ``NH₃``, ``Cu2O``), or None when
    it does not parse or names a symbol ase does not know."""
    text = formula.strip().translate(_SUBSCRIPT_DIGITS)
    if not text:
        return None
    try:
        counts = Formula(text).count()
    except (ValueError, KeyError, TypeError):
        return None
    if not counts or any(sym not in atomic_numbers or sym == "X" for sym in counts):
        return None
    return float(sum(atomic_masses[atomic_numbers[s]] * n for s, n in counts.items()))


class NeedsMolarMass(Exception):
    """The reported and canonical units differ by mass vs amount; the caller
    must supply a molar mass (or flag the row)."""

    def __init__(self, direction: str) -> None:
        super().__init__(direction)
        self.direction = direction


@dataclass(frozen=True, slots=True)
class Conversion:
    """A reported-unit -> canonical-unit converter. ``value`` maps a number;
    ``scale`` is the slope (for an uncertainty); ``label`` is the basis label
    stripped from the reported unit. ``error`` maps an uncertainty exactly when
    the slope alone (a float product) would not (the compat-row path); None
    means ``abs(err * scale)``."""

    value: Callable[[float], float]
    scale: float
    label: str | None
    error: Callable[[float], float] | None = None
    #: A greenhouse-gas qualifier stripped from the reported unit
    #: ('Gt CO2 yr-1' -> 'CO2') — lands as the row's ``gas`` condition.
    gas: str | None = None


_TOKEN_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[\s*/]+")
_EXPONENT_RE: Final[re.Pattern[str]] = re.compile(r"(?:\^|\*\*)\(?-?\d+(?:\.\d+)?\)?$")

#: Affine temperature spellings a ``difference`` measurand reads as deltas:
#: a 0.5 °C *change* is 0.5 K, not 273.65 K — pint converts ``degC``
#: absolutely unless the unit is spelled ``delta_degC`` (gr512366).
_AFFINE_TOKENS: Final[dict[str, str]] = {
    "degc": "delta_degC",
    "°c": "delta_degC",
    "celsius": "delta_degC",
    "degf": "delta_degF",
    "°f": "delta_degF",
    "fahrenheit": "delta_degF",
}


def _delta_unit_text(unit_text: str) -> str:
    """Affine temperature tokens -> pint's delta units, token-wise, so a
    ``'°C/yr'`` rate becomes ``'delta_degC yr'`` too."""
    parts = _TOKEN_SPLIT_RE.split(unit_text)
    return " ".join(_AFFINE_TOKENS.get(tok.lower(), tok) for tok in parts if tok)


def _names_dimension(unit_text: str, dimension: str) -> bool:
    """Does the printed unit contain a token whose own dimensionality is
    ``dimension``? Read from the text, not the parsed unit: pint cancels
    ``mg h⁻¹ mg⁻¹`` to ``h⁻¹`` and the mass token is gone."""
    ureg = _registry()
    want = ureg.get_dimensionality(dimension)
    for tok in _TOKEN_SPLIT_RE.split(unit_text):
        name = _EXPONENT_RE.sub("", tok)
        if not name:
            continue
        try:
            if ureg.Unit(name).dimensionality == want:
                return True
        except _PARSE_ERRORS:
            continue
    return False


def make_converter(
    reported_unit: str,
    canonical_unit: str,
    *,
    molar_mass_g_mol: float | None = None,
    delta: bool = False,
) -> Conversion:
    """Build the converter, or raise :class:`BadInput` naming both units when
    their dimensions do not match (and no molar-mass bridge applies), or
    :class:`NeedsMolarMass` when a bridge applies but none was given.

    ``delta`` marks a difference measurand (``meta.difference``): affine
    reported units (°C, °F) convert as deltas, not absolute temperatures."""
    ureg = _registry()
    src_text, label, gas = _pint_unit_ex(reported_unit)
    dst_text, _, _ = _pint_unit_ex(canonical_unit)
    if src_text == dst_text:
        # same unit once normalised: no pint needed (USD, count, a ``%``)
        return Conversion(value=lambda v: v, scale=1.0, label=label, gas=gas)
    if is_log_unit(src_text) or is_log_unit(dst_text):
        raise BadInput(
            f"unit {reported_unit!r} cannot be converted to {canonical_unit!r}: "
            "pH and dB are logarithmic scales, so only the identical unit is accepted",
            next="state the number in the measurand's own scale",
        )
    try:
        dst, _, _ = _parse_unit(canonical_unit, what="canonical")
    except BadInput as exc:
        raise BadInput(
            f"unit {reported_unit!r} cannot be converted to the measurand's "
            f"canonical unit {canonical_unit!r}: that is not a unit pint can "
            "convert, so only the identical unit is accepted",
            next="state the number in the canonical unit, or leave reported_unit empty",
        ) from exc
    src, label, gas = _parse_unit(reported_unit, what="reported", delta=delta)
    bridge: str | None = None
    if src.dimensionality != dst.dimensionality:
        ratio = src.dimensionality / dst.dimensionality
        mass_per_amount = ureg.get_dimensionality("[mass]/[substance]")
        # The ratio test alone would accept ``h⁻¹`` for a per-mass yield rate
        # (mass cancels against a basis mass), so the reported unit must name
        # the quantity being bridged: a mass unit for mass -> amount, a mole
        # unit for amount -> mass.
        if ratio == mass_per_amount and _names_dimension(src_text, "[mass]"):
            bridge = "mass_to_amount"
        elif ratio == 1 / mass_per_amount and _names_dimension(src_text, "[substance]"):
            bridge = "amount_to_mass"
        else:
            raise BadInput(
                f"unit {reported_unit!r} has no dimension match with the "
                f"measurand's canonical unit {canonical_unit!r}",
                next="correct the unit, or file the number under a measurand "
                "whose canonical unit has that dimension",
            )
    if bridge is not None and molar_mass_g_mol is None:
        raise NeedsMolarMass(bridge)

    def conv(v: float) -> float:
        q = ureg.Quantity(v, src)
        if bridge == "mass_to_amount":
            q = q / ureg.Quantity(molar_mass_g_mol, "g/mol")
        elif bridge == "amount_to_mass":
            q = q * ureg.Quantity(molar_mass_g_mol, "g/mol")
        return float(q.to(dst).magnitude)

    return Conversion(value=conv, scale=conv(1.0) - conv(0.0), label=label, gas=gas)


# ── legacy unit -> SI form (the legacy mint verbs' edge) ──────────────────

#: Coherent SI symbols a unit's dimensionality is matched against, base units
#: first. Anything else is spelled from pint's base units (``kg/m³``).
_COHERENT: Final[tuple[str, ...]] = (
    "m", "kg", "s", "K", "mol", "A", "cd", "N", "Pa", "J", "W", "C", "V", "ohm", "F",
)  # fmt: skip

#: The SI base unit each ``si_vector`` slot counts (same order as
#: :data:`precis.taxonomy.types.SI_BASE_ORDER`: length, mass, time, current,
#: temperature, substance, luminosity).
_SI_BASE_SYMBOLS: Final[tuple[str, ...]] = ("m", "kg", "s", "A", "K", "mol", "cd")

#: Dimensionless in pint, but their SI form is the radian, not ``1``.
_ANGLE_UNITS: Final[frozenset[str]] = frozenset(
    {"deg", "degree", "degrees", "°", "rad", "radian", "arcmin", "arcsec"}
)


@dataclass(frozen=True, slots=True)
class SiForm:
    """A unit's coherent SI unit and the exact linear map to it:
    ``si = legacy * factor + offset`` (Decimals, 15 significant digits)."""

    si_unit: str
    factor: Decimal
    offset: Decimal


def si_form(unit: str | None) -> SiForm | None:
    """The coherent SI form of ``unit`` (``nm`` -> ``m`` x 1e-9, ``%`` -> ``1`` x
    0.01, ``degC`` -> ``K`` + 273.15), computed with pint; None when ``unit`` is
    already coherent SI, is not a pint unit (USD, HV, a count), is logarithmic
    (pH, dB) or needs a molar mass. The factor and offset are taken from the
    converter as 15-significant-digit decimals, so a runtime row agrees with the
    ones migration 0188 seeded by hand."""
    text = (unit or "").strip()
    if not text or is_log_unit(text):
        return None
    try:
        parsed, _, _ = _parse_unit(text, what="legacy")
    except BadInput:
        return None
    ureg = _registry()
    if text.casefold() in _ANGLE_UNITS:
        si = "rad"
    elif parsed.dimensionless:
        si = "1"
    else:
        by_dim = {ureg.Unit(sym).dimensionality: sym for sym in reversed(_COHERENT)}
        si = by_dim.get(parsed.dimensionality) or (
            f"{ureg.Quantity(1, parsed).to_base_units().units:~P}"
        )
    if _same_unit(text, si):
        return None
    try:
        conv = make_converter(text, si)
    except (BadInput, NeedsMolarMass):
        return None
    offset_f = conv.value(0.0)
    factor = Decimal(f"{conv.value(1.0) - offset_f:.15g}")
    offset = Decimal(f"{offset_f:.15g}")
    if factor <= 0 or (factor == 1 and offset == 0):
        return None
    return SiForm(si_unit=si, factor=factor, offset=offset)


def is_coherent_si(unit: str | None) -> bool:
    """The unit already is its coherent SI form (``m``, ``kg m⁻³``, ``1``,
    ``kg/mol``): a legacy mint in it needs no conversion map, so a missing
    ``measure_unit_compat`` row is expected, not a defect."""
    text = (unit or "").strip()
    if not text:
        return False
    try:
        parsed, _, _ = _parse_unit(text, what="si")
    except BadInput:
        return False
    try:
        return bool(_registry().Quantity(1, parsed).to_base_units().magnitude == 1)
    except _PARSE_ERRORS:
        return False


def _si_vector_unit(si_vector: str) -> Any:
    """The pint unit an ``si_vector`` exponent string spells
    (``'2,1,-2,0,0,0,0'`` -> kg·m²/s² = J)."""
    exps = [int(e) for e in str(si_vector).split(",")]
    terms = " ".join(f"{sym}**{e}" for sym, e in zip(_SI_BASE_SYMBOLS, exps) if e)
    return _registry().Quantity(1, terms or "1")


def coherent_si_unit(si_vector: str) -> str:
    """The coherent SI unit of an ``si_vector`` — ``'2,1,-2,0,0,0,0'`` ->
    ``'J'``. A named derived unit from ``_COHERENT`` when the dimensionality
    has one, ``'1'`` for the all-zero vector, else the spelled base-unit
    product."""
    unit = _si_vector_unit(si_vector)
    if unit.dimensionless:
        return "1"
    ureg = _registry()
    by_dim = {ureg.Unit(sym).dimensionality: sym for sym in reversed(_COHERENT)}
    return by_dim.get(unit.dimensionality) or f"{unit.to_base_units().units:~P}"


def check_canonical_unit(si_vector: str, canonical_unit: str) -> None:
    """The taxon unit guard (ontology-v2 slice 0a): on a node carrying an
    ``si_vector``, ``canonical_unit`` must be the vector's coherent SI unit —
    values are stored in it, so a scaled or non-SI spelling would corrupt
    every stored measure. Raises :class:`BadInput` naming the coherent unit;
    the refused spelling belongs in ``display_unit``."""
    want = coherent_si_unit(si_vector)
    next_ = (
        f"canonical_unit={want!r}, display_unit={canonical_unit!r} "
        "(values stay SI; the display unit is what people read)"
    )
    if is_log_unit(canonical_unit):
        raise BadInput(
            f"canonical_unit {canonical_unit!r} is a logarithmic scale, not "
            f"the coherent SI unit of si_vector {si_vector!r} ({want!r})",
            next=next_,
        )
    parsed, _, _ = _parse_unit(canonical_unit, what="canonical")
    expected = _si_vector_unit(si_vector)
    base = _registry().Quantity(1, parsed).to_base_units()
    if base.dimensionality != expected.dimensionality:
        raise BadInput(
            f"canonical_unit {canonical_unit!r} does not match the dimension "
            f"of si_vector {si_vector!r} ({want!r})",
            next=next_,
        )
    if base.magnitude != 1:
        raise BadInput(
            f"canonical_unit {canonical_unit!r} is not the coherent SI unit of "
            f"si_vector {si_vector!r} ({want!r}) — a scaled spelling stores "
            "values off by its factor",
            next=next_,
        )


# ── display: SI value -> the unit a person expects ────────────────────────


def is_unit(text: str) -> bool:
    """Does pint read ``text`` as a unit? A plain word, or a log scale such as
    ``pH`` (which pint would misread as a petahenry), is not."""
    if is_log_unit(text):
        return False
    try:
        _parse_unit(text, what="query")
    except BadInput:
        return False
    return True


def _clean(unit: str | None) -> str | None:
    text = (unit or "").strip()
    return text or None


def _same_unit(a: str, b: str) -> bool:
    return pint_unit_text(a)[0] == pint_unit_text(b)[0]


def format_number(x: float) -> str:
    """Four significant figures, no trailing zeros, never ``-0``."""
    text = f"{x:.4g}"
    return "0" if text == "-0" else text


def to_canonical(value: float, unit: str, canonical_unit: str | None) -> float:
    """A number given in ``unit`` as the measurand's canonical (SI) value.
    Refuses, naming both units, when they do not match; a canonical unit
    that is absent accepts no unit at all."""
    canon = _clean(canonical_unit)
    if canon is None:
        raise BadInput(
            f"unit {unit!r} cannot be applied: this measurand has no canonical unit",
            next="drop the unit, or set the taxon's canonical_unit",
        )
    try:
        return make_converter(unit, canon).value(value)
    except NeedsMolarMass as exc:
        raise BadInput(
            f"unit {unit!r} and the canonical unit {canon!r} differ by mass versus "
            "amount, which needs a molar mass a query cannot supply",
            next="state the bound in a unit of the same kind as the canonical unit",
        ) from exc


def validate_display_unit(display_unit: str, canonical_unit: str | None) -> None:
    """A taxon's ``display_unit`` must be the canonical unit or a pint unit of
    the same dimensionality. With no canonical unit yet it is only checked
    once both are set. A canonical unit pint cannot parse (USD, a count)
    allows only an identical display unit; pH and dB likewise."""
    disp = _clean(display_unit)
    if disp is None:
        raise BadInput(
            "display_unit must be a non-empty unit string",
            next="display_unit='Å' (or drop the key)",
        )
    canon = _clean(canonical_unit)
    if canon is None or _same_unit(disp, canon):
        return
    if is_log_unit(canon) or is_log_unit(disp):
        raise BadInput(
            f"display_unit {display_unit!r} differs from canonical_unit "
            f"{canonical_unit!r}: pH and dB are logarithmic and only the identical "
            "unit is allowed",
            next=f"display_unit={canonical_unit!r}, or drop it",
        )
    try:
        canon_unit, _, _ = _parse_unit(canon, what="canonical")
    except BadInput as exc:
        raise BadInput(
            f"display_unit {display_unit!r} differs from canonical_unit "
            f"{canonical_unit!r}, which is not a unit pint can convert: only the "
            "identical unit is allowed",
            next=f"display_unit={canonical_unit!r}, or drop it",
        ) from exc
    disp_unit, _, _ = _parse_unit(disp, what="display")
    if disp_unit.dimensionality != canon_unit.dimensionality:
        raise BadInput(
            f"display_unit {display_unit!r} has a different dimension from "
            f"canonical_unit {canonical_unit!r}",
            next="pick a display unit of the same kind as the canonical unit",
        )


def _canon_label(canon: str) -> str:
    return "" if pint_unit_text(canon)[0] == "1" else canon


def _compact(values: Sequence[float], canon: str) -> tuple[list[float], str]:
    """The canonical unit with the SI prefix pint's ``to_compact()`` picks for
    the largest value. A scale, an unparseable or dimensionless unit, or an
    affine canonical keeps the canonical unit as stored."""
    plain = (list(values), _canon_label(canon))
    if is_log_unit(canon) or not values:
        return plain
    try:
        unit, _, _ = _parse_unit(canon, what="canonical")
    except BadInput:
        return plain
    if unit.dimensionless or f"{unit:~P}".startswith("1/"):
        # pint prefixes the FIRST term: on 1/s it would print 500 1/ks
        return plain
    ref = max(values, key=abs)
    if ref == 0:
        return plain
    ureg = _registry()
    try:
        picked = ureg.Quantity(ref, unit).to_compact()
        if picked.units == unit:
            return plain
        return (
            [float(ureg.Quantity(v, unit).to(picked.units).magnitude) for v in values],
            f"{picked.units:~P}",
        )
    except _PARSE_ERRORS:
        return plain


def display_numbers(
    values: Sequence[float],
    *,
    canonical_unit: str | None,
    display_unit: str | None = None,
    unit: str | None = None,
) -> tuple[list[float], str]:
    """Stored canonical values as numbers in the output unit, with its label.

    The unit is, in order: ``unit`` (an explicit request; a unit that does not
    match raises), the taxon's ``display_unit`` (a stale one that no longer
    matches falls back), else the canonical unit with an automatic prefix. The
    label is ``''`` for a bare number. All values share one unit, chosen from
    the largest, so an interval never mixes prefixes."""
    canon = _clean(canonical_unit)
    explicit = _clean(unit)
    target = explicit or _clean(display_unit)
    if canon is None:
        return list(values), target or ""
    if target is not None and _same_unit(target, canon):
        return list(values), _canon_label(canon) or (target if target != "1" else "")
    if target is not None:
        try:
            conv = make_converter(canon, target)
        except BadInput:
            if explicit is not None:
                raise
        except NeedsMolarMass as exc:
            if explicit is not None:
                raise BadInput(
                    f"unit {explicit!r} and the stored unit {canon!r} differ by mass "
                    "versus amount",
                    next="pick a unit of the same kind as the stored unit",
                ) from exc
        else:
            return [conv.value(v) for v in values], target
    return _compact(values, canon)


def format_value(
    value: float,
    *,
    canonical_unit: str | None,
    display_unit: str | None = None,
    unit: str | None = None,
) -> str:
    """One stored canonical value as text: ``1.4e-10`` m with display Å is
    ``1.4 Å``; with none, ``140 pm``; 0.95 with display % is ``95 %``;
    298.15 K with display °C is ``25 °C``."""
    nums, label = display_numbers(
        [value], canonical_unit=canonical_unit, display_unit=display_unit, unit=unit
    )
    return f"{format_number(nums[0])} {label}".strip()
