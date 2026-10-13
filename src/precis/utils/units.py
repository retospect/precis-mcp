"""Shared units utility — ingest-any → SI internal → neat formatter.

**This module is the units policy's home** (shipped 2026-09-12, the
units-policy cutover; superseded the now-deleted
`docs/backlog/units-policy-cutover.md` decisions log — cite this
docstring, not that path). Implements the map's §Units policy
(`docs/backlog/multiscale-design-architecture.md`): se/cad ops (`nm` was
folded into `se`, see `precis_se/__init__.py`) and the cad DSL accept any
pint-parseable unit at the boundary (`3 mm`, `1.4 Å`, `12 lbf`, ...);
everything downstream — handler state, plugin tables, kernel calls — is
SI base float64 (metre, newton, kilogram, cubic metre). Conversion
happens exactly once, inbound, via :func:`parse_quantity`.

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
  rewired yet).

`pint` is a core dependency (`pint>=0.23`, see pyproject) and is used
here at the ingest boundary only — no `pint.Quantity`/`pint.Unit` value
ever escapes this module; every public function takes/returns plain
`str`/`float`.

Explicitly out of scope (map boundary): interaction physics, `structure`'s
Å-native crystallography path, and `precis_se/atomic/mechanics.py`'s
Å/nN/eV signatures — those never route through this module; `se`'s
atomic mode converts m↔Å explicitly at its own seams.

**Angles** (the angle ruling below) are a fifth dimension,
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

**Display preferences** (the ``prefunits`` / respond-in-kind rounds of
docs/backlog/pose-vector-units.md — display only; storage stays SI).
Three scoped channels, all bound by a handler for the duration of one
call and read by :func:`format_quantity` / :func:`format_vector` /
:func:`format_quantity_column`, in precedence order: an explicit
``prefunits=`` argument > per-call :func:`prefunits_scope`
(``args.prefunits`` / a put payload's) > the caller's own *used* units
(:func:`capture_used_units` — respond-in-kind, recorded by
:func:`parse_quantity` at ingest) > sticky :func:`sticky_prefunits_scope`
(``meta.prefunits`` on the ref) > the neat default. The ContextVar
plumbing is the ``cad.scene`` ``_WARN_SINK`` precedent: handlers bind a
scope once and every formatter inside the call honours it with no
signature churn.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from typing import Final, Literal, cast

import pint
from pint.facets.plain import PlainUnit

from precis.errors import BadInput

log = logging.getLogger(__name__)

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
    warnings: list[str] | None = None,
    record_used: bool = True,
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

    ``warnings`` is an optional advisory accumulator (strict path only:
    ignored when ``require_unit`` is False, so the permissive storage
    parse stays silent forever). When the authored mantissa is far
    outside the readable range for its chosen unit
    (:func:`awkward_mantissa_advice`) an advisory string is appended;
    the value is still returned — warn-only, never a denial. Callers
    that pass nothing see no behaviour change.
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

    if dimension == "angle":
        text = _ANGLE_CIRC_RE.sub("deg", text)

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

    # ``record_used=False`` marks a re-parse of stored data (a registry
    # row's unit, not a unit the caller just typed) so it cannot steer
    # respond-in-kind — everything else lands in the used-units sink.
    if require_unit and record_used:
        _record_used(dimension, parsed.units)
    if warnings is not None and require_unit:
        advice = awkward_mantissa_advice(
            float(parsed.magnitude), float(si.magnitude), dimension, arg_name=arg_name
        )
        if advice:
            warnings.append(advice)
    return float(si.magnitude)


#: Authored mantissas with magnitude outside [_AWKWARD_LO, _AWKWARD_HI)
#: draw the advisory. The readable range is ~0.1-10000 (gr336344); the
#: trigger sits one decade beyond each end so ordinary `0.05 mm` or
#: `20000 mm` never nag — only genuine zero-run forms (`0.000000003 m`).
_AWKWARD_LO: Final[float] = 0.01
_AWKWARD_HI: Final[float] = 1e5


def awkward_mantissa_advice(
    authored: float,
    si_value: float,
    dimension: Dimension,
    *,
    arg_name: str | None = None,
) -> str | None:
    """Advisory for a unit-present but poorly-scaled authored quantity.

    ``authored`` is the mantissa as written (in the author's unit),
    ``si_value`` the same quantity in SI base. Returns e.g.
    ``"arg: parsed as 3 nm — prefer writing it that way"`` when
    ``|authored|`` is outside ``[0.01, 1e5)`` AND :func:`format_quantity`'s
    neat form (no second prefix table) exists as a prefixed unit; else
    ``None``. Zero, non-finite, angle (no prefix ladder) and values the
    ladder can't place (e-notation fallback) never warn.
    """
    if dimension == "angle" or authored == 0 or not math.isfinite(authored):
        return None
    if _AWKWARD_LO <= abs(authored) < _AWKWARD_HI:
        return None
    neat = _format_neat(si_value, dimension)
    if "e" in neat:
        return None
    who = f"{arg_name}: " if arg_name else ""
    return f"{who}parsed as {neat} — prefer writing it that way"


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


#: LaTeX-style degree spellings (``30\\circ``, ``30^\\circ``, ``30^{\\circ}``)
#: the angle grammar accepts beside ``deg`` / ``°`` / ``degrees``; pint
#: knows the latter, not the TeX forms, so they are rewritten to ``deg``.
_ANGLE_CIRC_RE: Final[re.Pattern[str]] = re.compile(r"\^?\s*\{?\\circ\}?")


def parse_angle_deg(
    value: object,
    *,
    arg_name: str,
    bare: Literal["reject", "degrees"] = "reject",
) -> float:
    """Angle arg at an MCP surface → degrees (``None``/``""`` → 0.0).

    Accepts ``"90deg"`` / ``"90°"`` / ``"90\\circ"`` / ``"1.5 rad"`` (any
    pint angle unit). A bare number is rejected with the two-reading hint
    unless it is zero, or ``bare="degrees"`` declares a unit enclave whose
    bare convention is degrees (pcb's ``rot``, stored in degrees).
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        raise BadInput(f"{arg_name} cannot be a boolean: {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise BadInput(f"{arg_name} must be finite, got {value!r}")
        if bare == "degrees" or value == 0:
            return float(value)
    elif isinstance(value, str) and bare == "degrees":
        try:
            bare_val = float(value)  # a numeral string is the same bare number
            if not math.isfinite(bare_val):
                raise BadInput(f"{arg_name} must be finite, got {value!r}")
            return bare_val
        except ValueError:
            pass
    if not isinstance(value, (int, float, str)):
        raise BadInput(f"{arg_name} must be a number or angle string, got {value!r}")
    return math.degrees(parse_quantity(value, "angle", arg_name=arg_name))


def format_angle_both(radians: float) -> str:
    """Both forms of an angle for display when the caller named no single
    unit (the respond-in-kind fallback): ``"90° (1.5708 rad)"``."""
    if not math.isfinite(radians):
        raise BadInput(f"cannot format a non-finite angle: {radians!r}")
    return f"{format_quantity(radians, 'angle')} ({radians:.5g} rad)"


def format_angle_in_kind(radians: float, unit: str | None) -> str:
    """Respond-in-kind angle display: echo the unit the caller used
    (``rad`` → ``1.5708 rad``; ``deg`` → ``90°``). ``unit=None`` (no
    single unit, or mixed) falls back to :func:`format_angle_both`."""
    if unit == "rad":
        if not math.isfinite(radians):
            raise BadInput(f"cannot format a non-finite angle: {radians!r}")
        return f"{radians:.5g} rad"
    if unit == "deg":
        return format_quantity(radians, "angle")
    return format_angle_both(radians)


#: Plausible unit readings offered for a bare nonzero VECTOR (the canonical
#: trailing-unit form echoes the caller's own vector under each). Angle
#: reuses :data:`_HINT_UNITS`.
_VEC_HINT_UNITS: Final[dict[Dimension, tuple[str, ...]]] = {
    **_HINT_UNITS,
    "length": ("nm", "mm", "m"),
}

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_NUMERAL_RE: Final[re.Pattern[str]] = re.compile(rf"^{_NUM}$")
_NUM_UNIT_RE: Final[re.Pattern[str]] = re.compile(rf"^({_NUM})\s+(\S.*)$")
_BRACKETED_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[\[(](.*?)[\])]\s*(.*)$", re.S)


def _is_numeral(c: object) -> bool:
    """A bare number: a non-bool int/float, or a unit-less numeral string."""
    if isinstance(c, bool):
        return False
    if isinstance(c, (int, float)):
        return True
    return isinstance(c, str) and bool(_NUMERAL_RE.match(c.strip()))


def vector_canonical(components: list[float], unit: str) -> str:
    """The canonical trailing-unit spelling of a vector whose components
    are already in ``unit``: ``[3 0 0] nm``."""
    return f"[{' '.join(format_dsl_number(c) for c in components)}] {unit}"


def _bare_vector_hint(components: list[float], dimension: Dimension) -> str:
    units = _VEC_HINT_UNITS[dimension]
    readings = "? ".join(f'"{vector_canonical(components, u)}"' for u in units)
    return f"state units — {readings}?"


def _example_hint(n: int, dimension: Dimension) -> str:
    unit = _VEC_HINT_UNITS[dimension][0]
    return f'e.g. "{vector_canonical([3.0, 0.0, 0.0][:n], unit)}"'


def _split_vector_text(text: str) -> tuple[list[str], str | None]:
    """Vector text → (component strings, trailing unit or None). Accepts
    ``"[3 0 0] nm"``, ``"3 0 0 nm"``, ``"[3nm, 0nm, 2Å]"``, ``"3, 0, 0 nm"``."""
    trailing: str | None = None
    m = _BRACKETED_RE.match(text)
    if m:
        inner, rest = m.group(1), m.group(2).strip()
        trailing = rest or None
    else:
        inner = text
    parts = [p.strip() for p in (inner.split(",") if "," in inner else inner.split())]
    if m is None and parts and not _is_numeral(parts[-1]):
        # unbracketed: the unit is its own token ("3 0 0 nm") or glued to
        # the last component by a space ("3, 0, 0 nm")
        nu = _NUM_UNIT_RE.match(parts[-1])
        head = parts[:-1]
        if nu and all(_is_numeral(p) for p in head):
            trailing, parts = nu.group(2), [*head, nu.group(1)]
        elif all(_is_numeral(p) for p in head) and not re.match(
            r"^[+-]?[\d.]", parts[-1]
        ):
            trailing, parts = parts[-1], head
    return parts, trailing


def parse_vector_quantity(
    value: object,
    dimension: Dimension,
    *,
    unit: str | None = None,
    n: int = 3,
    arg_name: str | None = None,
    warnings: list[str] | None = None,
) -> list[float]:
    """Ingest an n-vector of ``dimension`` quantities → SI floats (the
    ONE vector grammar; ``blocktree.ops._as_vec3`` and so se/nm ride it).

    Accepted: (1) trailing unit — ``"[3 0 0] nm"`` / ``"3 0 0 nm"``, the
    CANONICAL form; (2) per-component — ``["3nm", "0nm", "2Å"]``, units
    may differ; (3) ``unit=`` supplied beside a bare-numeric vector.
    A bare ``0`` component is always legal; e-notation mantissas are fine.
    A bare nonzero vector rejects with a hint echoing the caller's own
    vector under plausible unit readings; mixing a unit-carrying
    component with a bare nonzero one (or a trailing/``unit=`` unit on
    top of per-component units) rejects. Each component goes through
    :func:`parse_quantity` — pint owns the unit words.
    """
    who = arg_name or "vector"
    trailing = unit.strip() if isinstance(unit, str) and unit.strip() else None
    comps: list[object]
    if isinstance(value, str):
        if not value.strip():
            raise BadInput(f"{who} is empty", next=_example_hint(n, dimension))
        parts, text_unit = _split_vector_text(value.strip())
        if text_unit is not None:
            if trailing is not None:
                raise BadInput(
                    f"{who} states its unit twice ({text_unit!r} and {trailing!r})"
                )
            trailing = text_unit
        comps = list(parts)
    elif isinstance(value, (list, tuple)):
        comps = list(value)
    else:
        raise BadInput(
            f"{who} must be a {n}-vector, got {value!r}",
            next=_example_hint(n, dimension),
        )
    if len(comps) != n:
        raise BadInput(
            f"{who} must be a {n}-vector, got {value!r}",
            next=_example_hint(n, dimension),
        )
    for c in comps:
        if isinstance(c, bool) or not isinstance(c, (int, float, str)):
            raise BadInput(f"{who} component {c!r} is not a number or quantity string")
        if isinstance(c, (int, float)) and not math.isfinite(c):
            raise BadInput(f"{who} component {c!r} must be finite")

    bare = [_is_numeral(c) for c in comps]
    if trailing is not None:
        if not all(bare):
            raise BadInput(
                f"{who} states a unit ({trailing!r}) AND per-component units — "
                "use one form",
                next=f'"{vector_canonical([3.0, 0.0, 0.0][:n], trailing)}" or '
                "per-component units with no trailing one",
            )
        return [
            parse_quantity(
                f"{float(str(c).strip())!r} {trailing}",
                dimension,
                arg_name=f"{who}[{i}]",
                warnings=warnings,
            )
            for i, c in enumerate(comps)
        ]

    nums = [
        float(str(c).strip()) if is_bare else 0.0
        for c, is_bare in zip(comps, bare, strict=True)
    ]
    if all(bare):
        if all(x == 0 for x in nums):
            return [0.0] * n
        raise BadInput(
            f"{who} needs an explicit unit — bare nonzero vectors are rejected "
            f"for {dimension} quantities (zero-counting guard)",
            next=_bare_vector_hint(nums, dimension),
        )
    if any(b and x != 0 for b, x in zip(bare, nums, strict=True)):
        raise BadInput(
            f"{who} mixes united and bare nonzero components — give every "
            "nonzero component its own unit, or one trailing unit for all",
            next=_example_hint(n, dimension) + " or per-component units",
        )
    return [
        0.0
        if b
        else parse_quantity(
            str(c), dimension, arg_name=f"{who}[{i}]", warnings=warnings
        )
        for i, (c, b) in enumerate(zip(comps, bare, strict=True))
    ]


def _trim(x: float) -> float:
    """4 significant figures, -0 → 0 (display only)."""
    return 0.0 if x == 0 else float(f"{x:.4g}")


def format_vector(
    values: list[float], dimension: Dimension, *, prefunits: PrefUnits | None = None
) -> str:
    """Canonical neat display of an SI vector with ONE common unit for the
    whole vector (picked off the largest component): ``[3 0 0] nm``,
    ``[0 0 90] deg``. The echo-on-write spelling. A resolved display
    preference (``prefunits=`` arg, per-call/used/sticky scope —
    :func:`_pref_unit`) replaces the picked unit."""
    pref, f = _try_pref(dimension, prefunits)
    if pref is not None and f is not None:
        return vector_canonical([_trim(v * f) for v in values], pref)
    if dimension == "angle":
        return vector_canonical([_trim(math.degrees(v)) for v in values], "deg")
    factor, base_sym = _DISPLAY_BASE[dimension]
    big = max((abs(v) for v in values), default=0.0)
    ref = format_quantity(big, dimension)
    if big == 0 or "e" in ref:
        return vector_canonical([_trim(v * factor) for v in values], base_sym)
    sym = ref.split(" ", 1)[1]
    exp = next(e for e, p in _PREFIX_LADDER if p + base_sym == sym)
    return vector_canonical([_trim(v * factor / 10.0**exp) for v in values], sym)


def format_quantity(
    value: float, dimension: Dimension, *, prefunits: PrefUnits | None = None
) -> str:
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
    fidelity matters. A resolved display preference (:func:`_pref_unit`)
    renders in that unit instead — display only.
    """
    pref, _factor = _try_pref(dimension, prefunits)
    if pref is not None:
        return _format_in_unit(value, dimension, pref)
    return _format_neat(value, dimension)


def _format_neat(value: float, dimension: Dimension) -> str:
    """The neat default rendering of :func:`format_quantity` with no
    display preference — what the awkward-mantissa advisory checks
    against (a *policy* question: does a better-scaled unit exist? —
    never a display preference, so it deliberately bypasses the
    per-call/used/sticky scopes)."""
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


# ── display preferences, respond-in-kind, echo notes ─────────────────────

#: Preferred display units, keyed by dimension — the ``prefunits``
#: mechanism's resolved form (``{"length": "nm", "angle": "deg"}``).
PrefUnits = Mapping[Dimension, str]


#: Angle units by their base-unit identity, not dimensionality — pint's
#: registry has no angle dimension, so ``percent``/``ppm`` would otherwise
#: classify as angle (every dimensionless unit "converts" to radian).
def _is_angle_unit(q: pint.Quantity) -> bool:
    return q.to_base_units().units == _registry().radian


def _pref_symbol(units: PlainUnit) -> str:
    """The canonical short symbol for a parsed unit — ``nanometer`` →
    ``nm``, ``degree`` → ``deg`` (the ASCII spelling, matching the hint /
    header conventions rather than ``°``)."""
    sym = f"{units:~P}"
    return "deg" if sym == "°" else sym


def _unit_token(text: str, *, arg_name: str) -> tuple[Dimension | None, str]:
    """One bare unit token → ``(dimension or None, canonical symbol)`` —
    the per-token half of :func:`parse_prefunits`. ``None`` when the unit
    fits none of the five dimensions; raises for text that isn't a unit
    at all, or is a quantity (``"3 nm"``) rather than a bare unit."""
    ureg = _registry()
    try:
        parsed = ureg.parse_expression(text)
    except (pint.PintError, ValueError, TypeError, SyntaxError) as e:
        raise BadInput(f"{arg_name}: unknown unit {text!r}: {e}") from e
    if isinstance(parsed, pint.Unit):
        parsed = ureg.Quantity(1.0, parsed)
    if not isinstance(parsed, ureg.Quantity):
        raise BadInput(f"{arg_name}: {text!r} is a bare number, not a unit")
    if not isinstance(parsed.magnitude, (int, float)) or parsed.magnitude != 1:
        raise BadInput(
            f"{arg_name}: {text!r} is not a plain unit — give bare unit "
            "tokens ('nm,deg,kg'), not quantities"
        )
    sym = _pref_symbol(parsed.units)
    if _is_angle_unit(parsed):
        return "angle", sym
    for dim in ("length", "force", "mass", "volume"):
        try:
            parsed.to(_SI_UNIT[dim])
        except pint.DimensionalityError:
            continue
        return dim, sym
    return None, sym


def parse_prefunits(
    raw: object, *, arg_name: str = "prefunits"
) -> dict[Dimension, str]:
    """``prefunits`` input → ``{dimension: canonical symbol}``.

    Accepted: a comma list (``"nm,deg,kg"``), a list/tuple of unit tokens,
    or a ``{dimension: unit}`` map (the stored ``meta.prefunits`` form).
    A token's dimension is inferred — one entry per dimension wins (last
    one standing on a repeat). ``None``/empty → ``{}``. Display only —
    nothing here changes what ingest stores.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        items: list[object] = [t for t in (s.strip() for s in raw.split(",")) if t]
    elif isinstance(raw, Mapping):
        out_map: dict[Dimension, str] = {}
        for k, v in raw.items():
            key = str(k).strip().lower()
            if key not in _SI_UNIT:
                raise BadInput(
                    f"{arg_name}: unknown dimension {k!r} — keys are one of "
                    "length/force/mass/volume/angle"
                )
            got, sym = _unit_token(str(v).strip(), arg_name=arg_name)
            if got != key:
                raise BadInput(
                    f"{arg_name}: {v!r} is not a {key} unit"
                    + (f" (it is a {got} unit)" if got else "")
                )
            out_map[cast(Dimension, key)] = sym
        return out_map
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise BadInput(
            f"{arg_name} must be a unit list ('nm,deg') or "
            f"{{dimension: unit}} map, got {raw!r}"
        )
    out: dict[Dimension, str] = {}
    for tok in items:
        text = str(tok).strip()
        if not text:
            continue
        dim, sym = _unit_token(text, arg_name=arg_name)
        if dim is None:
            raise BadInput(
                f"{arg_name}: {text!r} is not a length/force/mass/volume/angle unit"
            )
        out[dim] = sym
    return out


#: Per-call preferred display units — ``args.prefunits`` / a put payload's
#: own. Bound by a handler for one call via :func:`prefunits_scope`.
_PREF_CALL: ContextVar[PrefUnits | None] = ContextVar("units_pref_call", default=None)
#: Sticky preferred display units — ``meta.prefunits`` on the design ref.
#: Lowest precedence of the three: a per-call pref or a used unit the
#: caller just typed both outrank it (per-call overrides sticky — the
#: design doc's own ordering).
_PREF_META: ContextVar[PrefUnits | None] = ContextVar("units_pref_meta", default=None)
#: The units the caller AUTHORED in this call's own arguments — the
#: respond-in-kind channel (gr336352). ``dimension -> canonical symbol``;
#: a value of ``None`` means conflicting units were used for that
#: dimension in one call (respond neutral — same rule
#: :func:`format_angle_in_kind` applies to a mixed ``unit``). Recorded by
#: :func:`parse_quantity` under :func:`capture_used_units`.
_USED_SINK: ContextVar[dict[Dimension, str | None] | None] = ContextVar(
    "units_used_sink", default=None
)
#: ``understood:`` echo lines for the op application in flight — the
#: echo-on-write channel. Bound by
#: :func:`~precis.blocktree.ops.apply_ops`; ``None`` outside it (a bare
#: parse then echoes nowhere, same as the warnings sink's caller-less
#: default).
_OP_NOTES: ContextVar[list[str] | None] = ContextVar("units_op_notes", default=None)


@contextlib.contextmanager
def prefunits_scope(pref: PrefUnits | None) -> Iterator[None]:
    """Bind per-call preferred display units for everything formatted
    inside the block (``args.prefunits`` / a put payload's own key)."""
    token = _PREF_CALL.set(dict(pref) if pref else None)
    try:
        yield
    finally:
        _PREF_CALL.reset(token)


@contextlib.contextmanager
def sticky_prefunits_scope(pref: PrefUnits | None) -> Iterator[None]:
    """Bind the ref's sticky ``meta.prefunits`` — the lowest-precedence
    display preference."""
    token = _PREF_META.set(dict(pref) if pref else None)
    try:
        yield
    finally:
        _PREF_META.reset(token)


@contextlib.contextmanager
def capture_used_units(
    sink: dict[Dimension, str | None],
) -> Iterator[None]:
    """Bind the respond-in-kind recorder: every strict
    :func:`parse_quantity` inside the block writes the unit the caller
    authored into ``sink`` (first unit wins; a conflicting second one
    turns the entry ``None``)."""
    token = _USED_SINK.set(sink)
    try:
        yield
    finally:
        _USED_SINK.reset(token)


@contextlib.contextmanager
def op_notes_scope(notes: list[str] | None) -> Iterator[None]:
    """Bind the echo-on-write notes list for an op application. A nested
    call passing ``notes=None`` INHERITS the enclosing sink, so an op that
    fans out into a second apply still echoes to the caller's list."""
    token = _OP_NOTES.set(notes if notes is not None else _OP_NOTES.get())
    try:
        yield
    finally:
        _OP_NOTES.reset(token)


def _record_used(dimension: Dimension, units: PlainUnit) -> None:
    """One authored unit into the respond-in-kind sink — the strict-ingest
    half only (``require_unit=False`` re-parses of stored canonical text
    are not the caller "using" a unit, so they never reach here)."""
    sink = _USED_SINK.get()
    if sink is None:
        return
    sym = _pref_symbol(units)
    if dimension not in sink:
        sink[dimension] = sym
    elif sink[dimension] != sym:
        sink[dimension] = None


def _pref_unit(dimension: Dimension, prefunits: PrefUnits | None) -> str | None:
    """Resolve the display unit for ``dimension``: explicit argument >
    per-call ``prefunits`` > the caller's own used unit (respond-in-kind)
    > sticky ``meta.prefunits`` > ``None`` (the neat default)."""
    if prefunits is not None and dimension in prefunits:
        return prefunits[dimension]
    call = _PREF_CALL.get()
    if call is not None and dimension in call:
        return call[dimension]
    used = _USED_SINK.get()
    if used is not None:
        sym = used.get(dimension)
        if sym:
            return sym
    meta = _PREF_META.get()
    if meta is not None:
        return meta.get(dimension)
    return None


def _try_pref(
    dimension: Dimension, prefunits: PrefUnits | None
) -> tuple[str | None, float | None]:
    """``(symbol, SI→unit factor)`` for the resolved preference, or
    ``(None, None)`` when none applies or the resolved symbol cannot
    convert (a corrupt sticky/arg pref must never fail a *committed
    write* on the render path — it falls back to the canonical unit and
    leaves a note on the op-notes sink plus a log line)."""
    pref = _pref_unit(dimension, prefunits)
    if pref is None:
        return None, None
    try:
        return pref, _pref_factor(dimension, pref)
    except BadInput:
        sink = _OP_NOTES.get()
        if sink is not None:
            sink.append(
                f"note: display pref {pref!r} is not a {dimension} unit — "
                "rendering the canonical unit"
            )
        log.warning("display pref %r is not a %s unit; canonical used", pref, dimension)
        return None, None


def _pref_factor(dimension: Dimension, sym: str) -> float:
    """SI → ``sym`` magnitude multiplier (``1 m`` → ``1000 mm``)."""
    ureg = _registry()
    try:
        return float(ureg.Quantity(1.0, _SI_UNIT[dimension]).to(sym).magnitude)
    except pint.DimensionalityError as e:
        raise BadInput(f"{sym!r} is not a {dimension} unit") from e


def _format_in_unit(value: float, dimension: Dimension, sym: str) -> str:
    """``value`` (SI) rendered in an explicit unit — the
    prefunits/respond-in-kind path. ``deg`` keeps the ``90°`` spelling
    (the angle ruling); ``rad`` matches :func:`format_angle_in_kind`."""
    if not math.isfinite(value):
        raise BadInput(f"cannot format a non-finite {dimension}: {value!r}")
    if dimension == "angle":
        if sym in ("deg", "°"):
            return f"{math.degrees(value):.3g}°"
        if sym == "rad":
            return f"{value:.5g} rad"
    return f"{value * _pref_factor(dimension, sym):.4g} {sym}"


def op_note(text: str) -> None:
    """One ``understood:``-style line into the echo-on-write sink, if one
    is bound — the escape hatch for a field-level acknowledgement that
    isn't a single quantity (a whole waypoints path, say)."""
    sink = _OP_NOTES.get()
    if sink is not None:
        sink.append(text)


def _echo_seen(sink: list[str], what: str) -> bool:
    """Dedupe per op: one ``understood:`` line per field, not one per
    parse — a re-parsed arg (defaults folded into the same ``what``)
    doesn't echo twice."""
    head = f"understood: {what} ="
    return any(line.startswith(head) for line in sink)


def echo_vector(what: str, values: list[float], dimension: Dimension) -> None:
    """``understood: <what> = <canonical vector>`` — the echo-on-write
    acknowledgement, appended only while an op-notes sink is bound
    (:data:`_OP_NOTES`, i.e. an ``apply_ops`` walk that a handler asked
    for notes on)."""
    sink = _OP_NOTES.get()
    if sink is not None and not _echo_seen(sink, what):
        sink.append(f"understood: {what} = {format_vector(values, dimension)}")


def echo_quantity(what: str, value: float, dimension: Dimension) -> None:
    """The scalar sibling of :func:`echo_vector` — one dimensioned
    quantity the op just ingested, acknowledged in canonical neat form."""
    sink = _OP_NOTES.get()
    if sink is not None and not _echo_seen(sink, what):
        sink.append(f"understood: {what} = {format_quantity(value, dimension)}")


#: The column-header mantissa window: a shared header unit is only offered
#: when every (nonzero) cell's mantissa in it lands in [1e-3, 1e4) — the
#: readable range the awkward-mantissa advisory is tuned from.
_COL_LO: Final[float] = 1e-3
_COL_HI: Final[float] = 1e4


def _column_candidates(dimension: Dimension) -> list[tuple[float, str]]:
    """``(SI→unit factor, symbol)`` per display unit, ascending unit size —
    the prefix ladder for the dimension's display base (``nm``, ``µm``,
    …); angle's whole ladder is ``deg``."""
    if dimension == "angle":
        return [(180.0 / math.pi, "deg")]
    factor, base_sym = _DISPLAY_BASE[dimension]
    return [(factor / 10.0**exp, f"{p}{base_sym}") for exp, p in _PREFIX_LADDER]


def _col_num(x: float) -> str:
    """One bare cell mantissa — 4 significant figures, ``-0`` → ``0``."""
    return "0" if x == 0 else f"{x:.4g}"


def format_quantity_column(
    values: Sequence[float | None],
    dimension: Dimension,
    *,
    prefunits: PrefUnits | None = None,
) -> tuple[str | None, list[str]]:
    """One homogeneous column → ``(header unit or None, cell strings)`` —
    the TOON column rule (docs/backlog/pose-vector-units.md): ONE common
    unit in the header (``gap (nm)``) with bare-mantissa cells when a
    single unit leaves every mantissa readable; per-value neat
    :func:`format_quantity` cells with NO header unit when no common unit
    fits (a 1 nm row beside a 1 m row). Never a header unit over
    divergent cell suffixes. ``None`` cells render as ``""``.

    Unit choice: a resolved preference (prefunits, per-call, used or
    sticky — :func:`_pref_unit`) wins unconditionally; otherwise the unit
    :func:`format_quantity` would pick for the largest magnitude, then
    successively smaller units until every nonzero cell reads
    0.001–10000.
    """
    pref, f = _try_pref(dimension, prefunits)
    if pref is not None and f is not None:
        return pref, ["" if v is None else _col_num(v * f) for v in values]
    present = [v for v in values if v is not None]
    candidates = _column_candidates(dimension)
    chosen: tuple[float, str] | None = None
    if present:
        big = max(present, key=abs)
        # Start where format_quantity would put the largest value (the
        # biggest unit still leaving it a mantissa ≥ 1), then walk to
        # smaller units — a too-small sibling is the only failure mode,
        # and shrinking the unit only ever grows mantissas.
        if big == 0 or dimension == "angle":
            # All-zero column: the unsuffixed base unit reads best.
            start = 0
            if dimension != "angle":
                start = next(
                    i
                    for i, (f, _s) in enumerate(candidates)
                    if f == _DISPLAY_BASE[dimension][0]
                )
        else:
            start = 0
            for i, (f, _) in enumerate(candidates):
                if abs(big) * f >= 1:
                    start = i
        for factor, sym in candidates[start::-1]:
            if all(
                v == 0 or (math.isfinite(v) and _COL_LO <= abs(v * factor) < _COL_HI)
                for v in present
            ):
                chosen = (factor, sym)
                break
    if chosen is None:
        return None, [
            "" if v is None else format_quantity(v, dimension) for v in values
        ]
    factor, sym = chosen
    return sym, ["" if v is None else _col_num(v * factor) for v in values]
