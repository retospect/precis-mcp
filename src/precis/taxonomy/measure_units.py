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

``pint`` does not know ``%`` as a unit symbol in every build, so the
canonical-unit strings ``%`` / ``percent`` are mapped to ``percent`` up front.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import pint
from ase.data import atomic_masses, atomic_numbers, chemical_symbols
from ase.formula import Formula

from precis.errors import BadInput
from precis.taxonomy.census import _normalize_unit_candidate

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
    a bare number -> point; anything else -> categorical (text = literal)."""
    s = literal.strip().replace("−", "-").replace(" ", " ")
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


def pint_unit_text(raw_unit: str) -> tuple[str, str | None]:
    """``(text pint can parse, basis label or None)`` for a printed unit."""
    stripped, label = split_basis_label(raw_unit.strip())
    text = _normalize_unit_candidate(stripped)
    if text in ("%", "percent"):
        text = "percent"
    else:
        text = text.replace("%", " percent ")
    return text, label


def _parse_unit(raw_unit: str, *, what: str) -> tuple[Any, str | None]:
    text, label = pint_unit_text(raw_unit)
    try:
        return _registry().Unit(text), label
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
    stripped from the reported unit."""

    value: Callable[[float], float]
    scale: float
    label: str | None


_TOKEN_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[\s*/]+")
_EXPONENT_RE: Final[re.Pattern[str]] = re.compile(r"(?:\^|\*\*)\(?-?\d+(?:\.\d+)?\)?$")


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
    reported_unit: str, canonical_unit: str, *, molar_mass_g_mol: float | None = None
) -> Conversion:
    """Build the converter, or raise :class:`BadInput` naming both units when
    their dimensions do not match (and no molar-mass bridge applies), or
    :class:`NeedsMolarMass` when a bridge applies but none was given."""
    ureg = _registry()
    src_text, label = pint_unit_text(reported_unit)
    dst_text, _ = pint_unit_text(canonical_unit)
    if src_text == dst_text:
        # same unit once normalised: no pint needed (USD, count, a ``%``)
        return Conversion(value=lambda v: v, scale=1.0, label=label)
    try:
        dst, _ = _parse_unit(canonical_unit, what="canonical")
    except BadInput as exc:
        raise BadInput(
            f"unit {reported_unit!r} cannot be converted to the measurand's "
            f"canonical unit {canonical_unit!r}: that is not a unit pint can "
            "convert, so only the identical unit is accepted",
            next="state the number in the canonical unit, or leave reported_unit empty",
        ) from exc
    src, label = _parse_unit(reported_unit, what="reported")
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

    return Conversion(value=conv, scale=conv(1.0) - conv(0.0), label=label)
