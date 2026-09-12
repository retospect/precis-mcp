"""Shared units utility — ingest-any → SI internal → neat formatter.

Implements the map's §Units policy (`docs/backlog/multiscale-design-
architecture.md`) per `docs/backlog/units-policy-cutover.md`: se/nm/cad
ops and the cad DSL accept any pint-parseable unit at the boundary
(`3 mm`, `1.4 Å`, `12 lbf`, ...); everything downstream — handler state,
plugin tables, kernel calls — is SI base float64 (metre, newton,
kilogram, cubic metre). Conversion happens exactly once, inbound, via
:func:`parse_quantity`.

Two separate formatter contexts (dossier §4's closing distinction —
do not merge them):

* :func:`format_quantity` — human display. Picks the scale-appropriate
  SI prefix (`2.3 nm`, `1.2 kN`, `350 ml`); e-notation fallback outside
  the prefix ladder. Deliberately lossy (3 significant figures) — for
  reading, not for round-tripping.
* :func:`format_dsl_number` — the canonical, exponent-capable numeric
  emitter. Full float64 precision, guaranteed to re-parse under
  `cad.dsl._NUM`'s grammar. This is the shared implementation meant to
  replace both `cad.dsl._fmt_num` and `precis_se.catalog._fmt` (not
  rewired yet — see the units-policy-cutover backlog item).

`pint` is a core dependency (`pint>=0.23`, see pyproject) and is used
here at the ingest boundary only — no `pint.Quantity`/`pint.Unit` value
ever escapes this module; every public function takes/returns plain
`str`/`float`.

Explicitly out of scope (map boundary, `units-policy-cutover.md`
"Explicitly NOT in scope"): interaction physics, `structure`'s
Å-native crystallography path, and `precis_nm/mechanics.py`'s Å/nN/eV
signatures — those never route through this module; the nm handler
converts m↔Å explicitly at its own seams.

**Angles** (the decisions log's angle ruling) are a fifth dimension,
``"angle"``, canonical unit radian — everywhere `pint` treats an angle
as dimensionless (its default registry has no separate angle
dimension: ``ureg.Quantity(1.0, "degree").dimensionless`` is ``True``),
so :func:`parse_quantity` special-cases it: a bare numeral still parses
to a plain Python number (never a `pint.Quantity`, regardless of
dimension) and is caught by the existing bare-number guard exactly like
length/force/mass/volume; anything that *does* parse to a `Quantity`
for the ``"angle"`` dimension necessarily carries an explicit angle
unit (`deg`, `rad`, `turn`, …), so the generic "reject a dimensionless
Quantity as bare" branch (correct for the other four dimensions, where
a stray dimensionless `Quantity` really is unit-less) is skipped for
angle. :func:`format_quantity` renders degrees (`"90°"`), never the SI
prefix ladder — nobody reads femto-degrees.
"""

from __future__ import annotations

import math
from typing import Final, Literal

import pint

from precis.errors import BadInput

#: The five dimensioned quantity families this cutover covers (map
#: boundary — dimensionless ratios and currency/time stay outside the
#: unit ladder; see module docstring). Angle joined length/force/mass/
#: volume per the decisions log's angle ruling (radians internal,
#: explicit unit at ingest, degrees on display).
Dimension = Literal["length", "force", "mass", "volume", "angle"]

#: One canonical internal SI unit per dimension. Mass keeps kilogram (the
#: SI base unit) for parsing/storage, but *display* prefixes attach to
#: gram (SI prefix rule — "Mg", not "kkg"), so the formatter re-bases;
#: see ``_DISPLAY_BASE`` below. Angle's canonical unit is the radian —
#: never SI-prefixed (there is no "kilo-radian" convention), so it has
#: no ``_DISPLAY_BASE``/``_PREFIX_LADDER`` entry; see ``format_quantity``.
_SI_UNIT: Final[dict[Dimension, str]] = {
    "length": "meter",
    "force": "newton",
    "mass": "kilogram",
    "volume": "meter ** 3",
    "angle": "radian",
}

#: 2-3 plausible unit readings offered in the bare-number HINT, per
#: dimension — deliberately a fixed short list (deterministic, not
#: magnitude-tuned): the point is to make the agent's retry a one-token
#: edit, not to guess the "right" one.
_HINT_UNITS: Final[dict[Dimension, tuple[str, ...]]] = {
    "length": ("mm", "m", "in"),
    "force": ("N", "kN", "lbf"),
    "mass": ("g", "kg"),
    "volume": ("ml", "l"),
    "angle": ("deg", "rad"),
}

#: Display base unit + its symbol, and the factor from the internal SI
#: unit to that display base. Length/force keep their SI unit (prefixes
#: attach directly); mass re-bases kg→g (×1000, SI prefix rule); volume
#: re-bases m³→l (×1000) since nobody writes "350 mm³" for a beaker.
_DISPLAY_BASE: Final[dict[Dimension, tuple[float, str]]] = {
    "length": (1.0, "m"),
    "force": (1.0, "N"),
    "mass": (1000.0, "g"),
    "volume": (1000.0, "l"),
}

#: Decimal SI-prefix ladder, ascending by exponent. `format_quantity`
#: picks the largest entry whose factor still leaves the scaled value
#: >= 1; outside this whole range it falls back to e-notation.
_PREFIX_LADDER: Final[tuple[tuple[int, str], ...]] = (
    (-15, "f"),
    (-12, "p"),
    (-9, "n"),
    (-6, "µ"),  # micro sign
    (-3, "m"),
    (0, ""),
    (3, "k"),
    (6, "M"),
    (9, "G"),
    (12, "T"),
    (15, "P"),
)

#: Length units accepted glued directly onto a number with no separator,
#: inside a compact grammar token (`cad.dsl`'s `w40mm`, `cad.scene`'s
#: `@40mm,0mm,-1mm`) — the acceptance criteria's minimum length list.
#: Ordered longest-symbol-first so regex alternation can't shadow
#: `mm`/`cm`/`km`/`nm` with a bare `m` prefix match. Free-text quantity
#: args (a standalone `"12.4 mm"` string, space allowed) go through
#: :func:`parse_quantity` directly and get pint's full long tail; this
#: fixed list is only for the no-space embedded-token grammar, which
#: can't safely admit arbitrary multi-letter unit names (they'd collide
#: with the next token's key letters).
LENGTH_UNIT_TOKEN: Final[str] = "mm|cm|km|nm|Å|in|ft|m"

#: Angle units accepted glued directly onto a number with no separator,
#: inside a compact grammar token (``cad.dsl``'s ``chamfer:1mmx45deg``,
#: ``cad.scene``'s ``rot:45deg,0rad,0deg``) — the same no-space embedded
#: convention as :data:`LENGTH_UNIT_TOKEN`, deliberately just the two
#: ASCII spellings (no ``°`` in the glued grammar — free-text
#: :func:`parse_quantity` calls still take pint's full angle long tail,
#: e.g. ``turn``).
ANGLE_UNIT_TOKEN: Final[str] = "deg|rad"

_UREG: pint.UnitRegistry | None = None


def _registry() -> pint.UnitRegistry:
    global _UREG
    if _UREG is None:
        _UREG = pint.UnitRegistry()
    return _UREG


class UnitRequiredError(BadInput):
    """A dimensioned quantity arrived with no unit where one is required.

    ``hint`` echoes the bare value under 2-3 plausible unit readings
    (the decisions log's "one-edit retry" — ``3`` → ``"3 — state units:
    '3 mm'? '3 m'?"``); it is also passed as the error's ``next=`` so it
    surfaces at the MCP boundary without the caller doing anything extra.
    """

    def __init__(
        self, value: float, dimension: Dimension, *, arg_name: str | None = None
    ) -> None:
        self.value = value
        self.dimension = dimension
        self.arg_name = arg_name
        self.hint = _bare_number_hint(value, dimension)
        who = f"{arg_name} " if arg_name else ""
        super().__init__(
            f"{who}needs an explicit unit — bare numbers are rejected for "
            f"{dimension} quantities (zero-counting / silent exponent-slip "
            "guard)",
            next=self.hint,
        )


def _bare_number_hint(value: float, dimension: Dimension) -> str:
    try:
        shown = format_dsl_number(value)
    except BadInput:  # non-finite (nan/inf) — fall back to plain repr
        shown = repr(value)
    units = _HINT_UNITS[dimension]
    readings = "? ".join(f"'{shown} {u}'" for u in units)
    return f"{shown} — state units: {readings}?"


def parse_quantity(
    text_or_number: str | float,
    dimension: Dimension,
    *,
    require_unit: bool = True,
    arg_name: str | None = None,
) -> float:
    """Ingest-any-unit boundary: parse one dimensioned quantity to SI.

    ``text_or_number`` is either an already-numeric value (``3``, ``3.0``)
    or free text pint can parse (``"3 mm"``, ``"1.4 Å"``, ``"12 lbf"``,
    pint's long tail included). Returns the SI-base float64 magnitude
    (metre / newton / kilogram / cubic metre per ``dimension``).

    A bare number — no unit attached, whether passed as a Python number
    or as a unit-less numeral string like ``"3"`` — raises
    :class:`UnitRequiredError` when ``require_unit`` is True (the
    default and the zero-counting/exponent-slip guard from the decisions
    log). Pass ``require_unit=False`` only for a caller that has already
    established the value is in SI base units by some other means (e.g.
    re-parsing this module's own canonical output) — never for raw
    agent-supplied text.
    """
    if isinstance(text_or_number, bool):  # bool is an int subclass — reject early
        raise BadInput(f"{dimension} quantity cannot be a boolean: {text_or_number!r}")
    if isinstance(text_or_number, (int, float)):
        value = float(text_or_number)
        if require_unit:
            raise UnitRequiredError(value, dimension, arg_name=arg_name)
        return value

    text = text_or_number.strip()
    if not text:
        raise BadInput(
            f"{dimension} quantity is empty",
            next=f"state a value and unit, e.g. {_HINT_UNITS[dimension][0]!r}",
        )

    ureg = _registry()
    try:
        parsed = ureg.parse_expression(text)
    except pint.UndefinedUnitError as e:
        raise BadInput(
            f"unknown unit in {text!r}: {e}",
            next=f"use a full unit name/symbol, e.g. {_HINT_UNITS[dimension][0]!r}",
        ) from e
    except (pint.PintError, ValueError, TypeError, SyntaxError) as e:
        raise BadInput(f"could not parse {text!r} as a {dimension}: {e}") from e

    if isinstance(parsed, pint.Unit):
        parsed = ureg.Quantity(1.0, parsed)

    if not isinstance(parsed, ureg.Quantity):
        # A bare numeral string ("3") parses to a plain Python number,
        # not a Quantity — same bare-number guard as the numeric-input path.
        value = float(parsed)
        if require_unit:
            raise UnitRequiredError(value, dimension, arg_name=arg_name)
        return value

    if parsed.dimensionless and dimension != "angle":
        # A stray dimensionless Quantity really is a bare number for the
        # other four dimensions. Angle is exempt: pint's default registry
        # has no separate angle dimension, so "90 degree"/"1.5 rad" are
        # *also* `.dimensionless` Quantities — but they carry a real unit
        # (a bare numeral never reaches here as a Quantity at all, see
        # the module docstring), so for "angle" this branch would wrongly
        # treat an explicitly-unit-tagged value as bare and either reject
        # it or return its magnitude un-converted (degrees passed off as
        # radians). Fall through to the ``.to(radian)`` conversion below.
        value = float(parsed.magnitude)
        if require_unit:
            raise UnitRequiredError(value, dimension, arg_name=arg_name)
        return value

    try:
        si = parsed.to(_SI_UNIT[dimension])
    except pint.DimensionalityError as e:
        raise BadInput(
            f"{text!r} is not a {dimension} quantity: {e}",
            next=f"use a {dimension} unit, e.g. {_HINT_UNITS[dimension][0]!r}",
        ) from e

    return float(si.magnitude)


def format_dsl_number(x: float) -> str:
    """Canonical DSL-safe numeric emitter — full float64 precision,
    guaranteed to re-parse under `cad.dsl._NUM`'s exponent-capable
    grammar (``-?\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?``).

    Meant to replace both `cad.dsl._fmt_num` and `precis_se.catalog._fmt`
    (not rewired this round). Unlike :func:`format_quantity`, this is
    *not* lossy — it emits a bare number, no unit, at whatever precision
    ``repr(float)`` needs to round-trip exactly.
    """
    if not math.isfinite(x):
        raise BadInput(f"cannot format a non-finite number: {x!r}")
    if x == 0:
        return "0"
    if x == int(x) and abs(x) < 1e16:
        return str(int(x))
    text = repr(float(x))
    if text.endswith(".0"):
        text = text[:-2]
    return text


def format_quantity(value: float, dimension: Dimension) -> str:
    """Neat human display: scale-appropriate SI-prefixed unit string —
    ``2.3 nm``, ``1.2 kN``, ``350 ml``. E-notation fallback outside the
    prefix ladder (< 1e-15 or >= 1e18 in the *display* base unit).

    ``dimension="angle"`` is the one exception to the SI-prefix scheme
    (the decisions log's angle ruling: "degrees on display") — no
    prefix ladder, ``value`` (radians) renders as a bare degree number
    with a trailing ``°`` (``"90°"``, matching the bare-``°``-no-space
    style already used at existing degree-literal call sites, e.g.
    `precis_se.fasten`'s axis-misalignment finding). ``:.3g`` still
    gives the same 3-significant-figure precision and the same
    automatic e-notation fallback for extreme magnitudes as the
    SI-prefixed branch below.

    Lossy by design (3 significant figures) — for reading, not for
    round-tripping; use :func:`format_dsl_number` + the SI unit when
    fidelity matters.
    """
    if dimension == "angle":
        degrees = math.degrees(value)
        if not math.isfinite(degrees):
            raise BadInput(f"cannot format a non-finite angle: {value!r}")
        return f"{degrees:.3g}°"

    factor, base_symbol = _DISPLAY_BASE[dimension]
    scaled_base = value * factor
    magnitude = abs(scaled_base)

    if magnitude == 0:
        return f"0 {base_symbol}"

    lo_exp = _PREFIX_LADDER[0][0]
    hi_exp = _PREFIX_LADDER[-1][0]
    if magnitude < 10.0**lo_exp or magnitude >= 10.0 ** (hi_exp + 3):
        return f"{scaled_base:.3e} {base_symbol}"

    chosen = _PREFIX_LADDER[0]
    for exp, sym in _PREFIX_LADDER:
        if magnitude >= 10.0**exp:
            chosen = (exp, sym)
        else:
            break
    exp, sym = chosen
    scaled = scaled_base / (10.0**exp)
    return f"{scaled:.3g} {sym}{base_symbol}"
