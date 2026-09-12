"""The ``config`` mini-DSL — compact typed shape specs.

A single string names a primitive and its dimensions, e.g.

* ``box:w40mmd20mmh10mm`` — rectangular box (w × d × h)
* ``cyl:r3mmh12mm``       — cylinder (radius, height)
* ``cone:r4mmh8mm``       — cone (base radius → apex)
* ``tcone:rb4mmrt2mmh8mm`` — truncated cone (bottom → top radius)
* ``sphere:r5mm``         — sphere
* ``torus:R10mmr2mm``     — torus (major R, minor r)
* ``hex:r5mmh10mm``       — regular hexagonal prism (circumradius, height)
* ``ngon:n6r5mmh10mm``    — regular n-gon prism (``n`` is a dimensionless
                             count — never a unit)
* ``frustum:n6rb4mmrt2mmh5mm`` — regular n-gon frustum
* ``pyramid:n4r5mmh8mm``  — regular n-gon pyramid
* ``chamfer:1mmx45deg``   — planar bevel half-space tool: size × angle
                            (boundary mode requires a unit on both — a
                            length unit on size, ``deg``/``rad`` on angle;
                            canonical/storage mode is bare metres × bare
                            radians). Built entirely in the node's own
                            local frame (no anchor face) — see
                            :func:`build`.

Grammar: ``<alias>:<tokens>`` where each token is a ``<key><number><unit>``
triple (``<key><number>`` for the dimensionless ``n``). Keys are matched
longest-first so ``rb`` / ``rt`` win over ``r``; ``R`` (major radius) is
distinct from ``r``. ``chamfer`` uses the special
``<size><unit>x<angle><unit>`` form — a length unit on ``size``, an angle
unit (``deg``/``rad``) on ``angle``. The kernel this module builds against
(:mod:`precis.cad.primitives`) is unit-agnostic float64 — it has no
convention of its own; the unit lives entirely in this module's boundary.

Two parse modes, selected by ``require_units=`` (see :func:`parse`):

* **Boundary mode** (``require_units=True``) — a *fresh* config string
  arriving from an agent (a `put`/`edit` body, or any op argument): every
  dimensioned token must carry an explicit unit from
  :data:`precis.utils.units.LENGTH_UNIT_TOKEN`; a bare number raises
  :class:`~precis.utils.units.UnitRequiredError` (the zero-counting /
  exponent-slip guard) with its hint. Converts to SI metres inline via
  :func:`~precis.utils.units.parse_quantity` — this is the ingest boundary,
  exactly once.
* **Canonical/storage mode** (``require_units=False``, the default) — the
  config text a design has *already* stored: bare SI-base numbers, no
  units, parsed exactly as before this cutover. This is the mode every
  storage reload uses forever — ``se``/``nm`` envelope columns hold bare
  numbers in their own established internal unit (metres, both — nm's
  Å columns converted to metres in the same units-policy-cutover window)
  and must keep loading unchanged regardless of what boundary mode does;
  nothing about this default may ever require a unit. cad's own
  ``config`` strings are the one case where boundary and reload coincide
  (the *same* stored text is re-parsed on every build, unlike se/nm
  which parse a stored, already-SI structured value) — :mod:`precis.cad.scene`
  calls boundary mode (``require_units=True``) at every hand-authored
  ``config``/``@x,y,z``/``rot:`` token, and the built-in catalog
  (:mod:`precis.cad.catalog`) emits its own generated geometry with an
  explicit unit on every token (its designation numbers — a bolt's
  ``m3x12``, an extrusion's cut length — are catalogue-convention mm by
  the real-world standard's own part numbers, not a free quantity), so a
  design mixing catalog parts and hand-authored nodes never desyncs in
  scale.

This module raises :class:`DslError` for syntax errors (or
:class:`~precis.utils.units.UnitRequiredError`, itself a structured
``BadInput``, for a missing-unit boundary violation) — either propagates
to the dispatcher boundary as-is. ``DslError`` is *itself* a ``BadInput``
(see the class docstring) — a bare ``ValueError`` here would fall through
the dispatcher's ``except PrecisError`` branch and flatten to an opaque
``internal error in put: DslError (see server log)``, stripping the
grammar-teaching cause text at exactly the moment an agent is failing and
needs it (units-cutover prompt-surface audit, `docs/backlog/llm-prompt-
surface-audit.md`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from precis.cad.primitives import (
    CircularFrustum,
    HalfSpace,
    Primitive,
    Sphere,
    Torus,
    box,
    pyramid,
    regular_frustum,
    regular_prism,
)
from precis.cad.vec import vec3
from precis.errors import BadInput
from precis.utils.units import (
    ANGLE_UNIT_TOKEN,
    LENGTH_UNIT_TOKEN,
    UnitRequiredError,
    format_dsl_number,
    parse_quantity,
)


class DslError(BadInput, ValueError):
    """A malformed ``config`` string.

    Dual base: ``BadInput`` so the dispatcher renders the cause text (and
    any ``next=``) instead of flattening it to an opaque internal error —
    the same wiring :class:`~precis.utils.units.UnitRequiredError` already
    uses; ``ValueError`` so every existing ``except (DslError, ValueError)``
    / ``except ValueError`` call site (se/nm ingest, ``scene.part_spec``)
    keeps catching it unchanged.
    """


@dataclass(frozen=True)
class ShapeSpec:
    """A parsed shape: an ``alias`` and its numeric ``params``.

    Length params (everything but ``n`` and ``angle``) are SI metres;
    ``angle`` (chamfer only) is SI radians — whether that's because
    :func:`parse` just converted a boundary-mode unit-annotated token, or
    because canonical-mode parsed an already-SI-base stored number
    unchanged.
    """

    alias: str
    params: dict[str, float]


#: ``<key><number>`` — keys longest-first so ``rb``/``rt`` beat ``r``.
#: Numbers take an optional exponent (``3e-9``) — unambiguous since no
#: DSL key is ``e``; nm-scale dims are unreadable otherwise (gr332020).
#: Canonical/storage-mode grammar (``require_units=False``) — bare
#: numbers only, unchanged since before the units cutover.
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_TOKEN_RE = re.compile(rf"(rb|rt|R|r|w|d|h|n)({_NUM})")
_CHAMFER_RE = re.compile(rf"^({_NUM})x({_NUM})$")

#: Boundary-mode grammar (``require_units=True``) — every key's number
#: may be immediately followed by one of :data:`LENGTH_UNIT_TOKEN`
#: (mandatory for every key but ``n``, the dimensionless count; forbidden
#: for ``n``). The unit is optional *in the regex* so a bare number still
#: matches (letting :func:`parse` raise the structured
#: :class:`~precis.utils.units.UnitRequiredError` instead of a generic
#: syntax error).
_UNIT_TOKEN_RE = re.compile(rf"(rb|rt|R|r|w|d|h|n)({_NUM})({LENGTH_UNIT_TOKEN})?")
_UNIT_CHAMFER_RE = re.compile(
    rf"^({_NUM})({LENGTH_UNIT_TOKEN})?x({_NUM})({ANGLE_UNIT_TOKEN})?$"
)

#: Required keys per alias, in canonical output order (drives ``format_spec``).
_ALIAS_KEYS: dict[str, tuple[str, ...]] = {
    "box": ("w", "d", "h"),
    "cyl": ("r", "h"),
    "cone": ("r", "h"),
    "tcone": ("rb", "rt", "h"),
    "sphere": ("r",),
    "torus": ("R", "r"),
    "hex": ("r", "h"),
    "ngon": ("n", "r", "h"),
    "frustum": ("n", "rb", "rt", "h"),
    "pyramid": ("n", "r", "h"),
    "chamfer": ("size", "angle"),
}


def parse(config: str, *, require_units: bool = False) -> ShapeSpec:
    """Parse a ``config`` string into a :class:`ShapeSpec`.

    ``require_units=False`` (default) — canonical/storage mode: every
    number is bare SI-base, exactly the pre-cutover grammar; never
    rejects a bare number. ``require_units=True`` — boundary mode: every
    dimensioned token must carry an explicit unit
    (:data:`~precis.utils.units.LENGTH_UNIT_TOKEN`) and is converted to SI
    metres inline; a bare number raises
    :class:`~precis.utils.units.UnitRequiredError`. See the module
    docstring's two-mode contract — never flip this default; every
    existing storage caller depends on it staying permissive.
    """
    if not isinstance(config, str) or ":" not in config:
        raise DslError(
            f"config must be '<shape>:<dims>', got {config!r} "
            "(e.g. 'cyl:r3mmh12mm', 'box:w40mmd20mmh10mm')"
        )
    alias, _, rest = config.strip().partition(":")
    alias = alias.lower()
    rest = rest.strip()
    if alias not in _ALIAS_KEYS:
        known = ", ".join(sorted(_ALIAS_KEYS))
        raise DslError(f"unknown shape {alias!r}; known: {known}")

    if alias == "chamfer":
        if require_units:
            m = _UNIT_CHAMFER_RE.match(rest)
            if not m:
                raise DslError(
                    "chamfer config must be '<size><unit>x<angle><unit>', e.g. "
                    "'chamfer:1mmx45deg'"
                )
            size_num, unit, angle_num, angle_unit = (
                m.group(1),
                m.group(2),
                m.group(3),
                m.group(4),
            )
            if not unit:
                raise UnitRequiredError(
                    float(size_num), "length", arg_name=f"{alias}:size"
                )
            if not angle_unit:
                raise UnitRequiredError(
                    float(angle_num), "angle", arg_name=f"{alias}:angle"
                )
            size = parse_quantity(
                f"{size_num}{unit}", "length", arg_name=f"{alias}:size"
            )
            angle = parse_quantity(
                f"{angle_num}{angle_unit}", "angle", arg_name=f"{alias}:angle"
            )
            return ShapeSpec(alias, {"size": size, "angle": angle})
        m = _CHAMFER_RE.match(rest)
        if not m:
            raise DslError(
                "chamfer config must be '<size>x<angle>', e.g. 'chamfer:1x0.785'"
            )
        return ShapeSpec(alias, {"size": float(m.group(1)), "angle": float(m.group(2))})

    token_re = _UNIT_TOKEN_RE if require_units else _TOKEN_RE
    params: dict[str, float] = {}
    pos = 0
    for m in token_re.finditer(rest):
        if m.start() != pos:
            raise DslError(f"unexpected text in config near {rest[pos:]!r}")
        key, num = m.group(1), m.group(2)
        if key in params:
            raise DslError(f"duplicate key {key!r} in config {config!r}")
        if require_units:
            unit = m.group(3)
            if key == "n":
                if unit:
                    raise DslError(
                        f"{alias}:n is a dimensionless count, not a length — "
                        f"no unit allowed (got {m.group(0)!r})"
                    )
                value = float(num)
            elif not unit:
                raise UnitRequiredError(float(num), "length", arg_name=f"{alias}:{key}")
            else:
                value = parse_quantity(
                    f"{num}{unit}", "length", arg_name=f"{alias}:{key}"
                )
        else:
            value = float(num)
        params[key] = value
        pos = m.end()
    if pos != len(rest):
        raise DslError(f"unexpected text in config near {rest[pos:]!r}")

    required = set(_ALIAS_KEYS[alias])
    missing = required - params.keys()
    if missing:
        raise DslError(
            f"{alias} needs {sorted(required)}, missing {sorted(missing)} in {config!r}"
        )
    extra = params.keys() - required
    if extra:
        raise DslError(f"{alias} got unexpected key(s) {sorted(extra)}")

    if "n" in params:
        n = params["n"]
        if n != int(n) or int(n) < 3:
            raise DslError(f"n must be an integer >= 3, got {n}")
    return ShapeSpec(alias, params)


def build(spec: ShapeSpec) -> Primitive:
    """Build a kernel :class:`Primitive` from a :class:`ShapeSpec`.

    ``chamfer:SxA`` builds a :class:`~precis.cad.primitives.HalfSpace`
    cutting tool entirely in the node's own local frame — there is no
    anchor face; the node's usual ``@x,y,z``/``rot:`` transform places it in
    the world exactly like any other node. In the local frame the cutting
    plane's normal is ``n̂ = (sin A, 0, cos A)`` (``A`` radians, tilted off
    local ``+z`` toward local ``+x``, i.e. rotated about local ``y``); the
    plane passes through ``-size · n̂`` (``size`` in from the local origin
    along ``-n̂``, in whatever length unit the caller's params are in).
    The tool's material is the ``+n̂`` side, so a
    ``cut`` shaves off everything beyond the plane and an ``intersect``
    keeps only the near side.
    """
    p = spec.params
    a = spec.alias
    if a == "box":
        return box(p["w"], p["d"], p["h"])
    if a == "cyl":
        return CircularFrustum(rb=p["r"], rt=p["r"], h=p["h"])
    if a == "cone":
        return CircularFrustum(rb=p["r"], rt=0.0, h=p["h"])
    if a == "tcone":
        return CircularFrustum(rb=p["rb"], rt=p["rt"], h=p["h"])
    if a == "sphere":
        return Sphere(r=p["r"])
    if a == "torus":
        return Torus(R=p["R"], r=p["r"])
    if a == "hex":
        return regular_prism(6, p["r"], p["h"])
    if a == "ngon":
        return regular_prism(int(p["n"]), p["r"], p["h"])
    if a == "frustum":
        return regular_frustum(int(p["n"]), p["rb"], p["rt"], p["h"])
    if a == "pyramid":
        return pyramid(int(p["n"]), p["r"], p["h"])
    if a == "chamfer":
        angle = p["angle"]
        size = p["size"]
        sa, ca = math.sin(angle), math.cos(angle)
        return HalfSpace(
            point=vec3(-size * sa, 0.0, -size * ca), normal=vec3(-sa, 0.0, -ca)
        )
    raise DslError(
        f"{a!r} is not a buildable shape"
    )  # pragma: no cover - parse guards alias


def build_config(config: str, *, require_units: bool = False) -> Primitive:
    """Convenience: ``parse`` then ``build``. See :func:`parse` for
    ``require_units``'s two-mode contract."""
    return build(parse(config, require_units=require_units))


def format_spec(spec: ShapeSpec, *, units: bool = False) -> str:
    """Render a :class:`ShapeSpec` back to config text. Uses
    :func:`~precis.utils.units.format_dsl_number`, the shared full-
    precision, exponent-capable, re-parses-under-this-module's-grammar
    emitter.

    ``units=False`` (default) — canonical/storage-mode grammar: bare
    numbers, no units, symmetric with ``parse(..., require_units=False)``.
    This is what every stored ``config`` text looks like.

    ``units=True`` — boundary-mode-compatible emission: every length key
    (everything but the dimensionless ``n``) gets an explicit ``m`` suffix
    and chamfer's ``angle`` an explicit ``rad`` suffix (each key's internal
    SI unit), so the output re-parses under ``parse(..., require_units=True)``
    too — used when re-serialising a stored (bare) spec back to source a
    human/agent edits (e.g. ``scene.spec_to_source``).
    """
    suffix = "m" if units else ""
    angle_suffix = "rad" if units else ""
    if spec.alias == "chamfer":
        return (
            f"chamfer:{format_dsl_number(spec.params['size'])}{suffix}"
            f"x{format_dsl_number(spec.params['angle'])}{angle_suffix}"
        )
    parts = "".join(
        f"{key}{format_dsl_number(spec.params[key])}{'' if key == 'n' else suffix}"
        for key in _ALIAS_KEYS[spec.alias]
    )
    return f"{spec.alias}:{parts}"
