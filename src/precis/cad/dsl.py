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
* ``field:<sha256>``      — a sampled signed-distance grid
                            (:class:`~precis.cad.primitives.Field`), by
                            the content address of its stored payload
                            (``chunk_blobs``; :func:`precis.cad.fieldops.
                            encode_field`). ``>= 12`` hex chars of the
                            hash are accepted on the boundary; the stored
                            canonical form is the full 64. Building one
                            needs a ``field_loader`` (see :func:`build`)
                            — the DSL never inlines a grid. Takes no
                            dims and no ``rd`` (rounding a field is
                            ``fieldops.open``/``close``/``offset``).

Optional ``rd<len>`` on every convex solid but the sphere — ``box``,
``cyl``, ``cone``, ``tcone``, ``hex``, ``ngon``, ``frustum``, ``pyramid``
— rounds **every edge and corner** to radius ``rd``
(``box:w40mmd20mmh10mmrd2mm``). Rounding is the leaf-shrink + field-
offset rule of ``docs/backlog/cad-sdf-rounding-and-field-export.md``:
:func:`build` shrinks the shape by ``rd`` on every side (:func:`shrunk`)
and wraps it in :class:`~precis.cad.primitives.Rounded`, whose SDF is the
shrunk shape's exact distance minus ``rd``. Bounding dimensions are
unchanged for the box/cylinder/prism family (the base is lifted by ``rd``
so it stays at ``z=0``); a cone/pyramid apex becomes a sphere cap and the
solid ends short of the sharp apex (the offset lateral line meets the
axis below it). Refused at parse, naming the shape and the dimension,
when ``rd >= ½·min_dimension`` (a thin feature would vanish — never
clamped), and when a cone/pyramid's shrunk body would close up at its
slant (the ½ rule alone keeps a ``tcone``/``frustum``'s caps, not an
apex). ``sphere`` and ``torus`` refuse ``rd`` (already round — a no-op
would only route the export through the sampled field backend for
nothing); ``chamfer`` has no extent to shrink and its special grammar
cannot carry it.

**Draft.** ``box`` and ``cyl`` accept an explicit mold draft:
``draft<angle>`` (boundary mode: an angle unit, ``deg``/``rad``;
canonical storage: bare SI radians) tapers every side face by that
angle along the pull axis, so the face at the pull-side end is smaller
by ``h·tan(draft)`` on each side — a drafted cylinder is exactly a
:class:`~precis.cad.primitives.CircularFrustum`, a drafted box a
:class:`~precis.cad.primitives.PolyFrustum` (:func:`build` reuses both).
``pullz-1`` pulls toward the base instead of the default ``+z``; pull
along ``x``/``y`` is refused (the frustum kernels are z-axis — tilt the
node's ``rot:`` placement instead). ``draft`` must be positive and
below 45°, and small enough that the drafted end keeps a positive face;
``rd`` and ``draft`` compose — :func:`shrunk` offsets the drafted solid
exactly, the round wrapping it the usual way. The taper family
(``cone``/``tcone``/``frustum``/``pyramid``) already expresses draft
through ``rb``/``rt`` and refuses ``draft=`` saying so.

Grammar: ``<alias>:<tokens>`` where each token is a ``<key><number><unit>``
triple (``<key><number>`` for the dimensionless ``n``). Keys are matched
longest-first so ``rb`` / ``rt`` / ``rd`` win over ``r``; ``R`` (major
radius) is distinct from ``r``. ``chamfer`` uses the special
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from precis.cad.hmap import HeightMap
from precis.cad.primitives import (
    CircularFrustum,
    CylRelief,
    Field,
    HalfSpace,
    PolyFrustum,
    Primitive,
    Rounded,
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
    Dimension,
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
    #: ``field`` only: the grid's content address as written (``>= 12`` hex
    #: chars; the full sha256 once canonicalised). ``None`` for every shape.
    ref: str | None = None


#: The sampled-field leaf's alias — no dims, a content address instead.
FIELD_ALIAS = "field"

#: ``field:`` accepts a sha256 or a unique prefix of at least this many
#: hex chars (the store resolves the prefix; the canonical stored form is
#: the full 64). ``relief:``'s ``map:`` token takes the same form.
FIELD_REF_MIN = 12
_FIELD_REF_RE = re.compile(rf"^[0-9a-f]{{{FIELD_REF_MIN},64}}$")

#: Resolves a ``field:`` reference (full sha256 or accepted prefix) to the
#: loaded :class:`~precis.cad.primitives.Field`. Injected — the kernel
#: never reaches for the store; the one production loader is built by
#: ``Store.cad_load`` and rides on :attr:`precis.cad.scene.SceneSpec.field_loader`.
FieldLoader = Callable[[str], Field]

#: Resolves a ``relief:`` node's ``map:<sha>`` reference to the stored
#: :class:`~precis.cad.hmap.HeightMap`. Same injection pattern as
#: :data:`FieldLoader` — rides on
#: :attr:`precis.cad.scene.SceneSpec.hmap_loader`.
HmapLoader = Callable[[str], HeightMap]

#: The relief leaf's alias — dims plus a ``map:<sha>`` content address.
RELIEF_ALIAS = "relief"


#: ``<key><number>`` — keys longest-first so ``rb``/``rt``/``rd`` beat ``r``
#: and the payload keys (``draft``/``pull*``) beat ``d``. Numbers take an
#: optional exponent (``3e-9``) — unambiguous since no DSL key is ``e``;
#: nm-scale dims are unreadable otherwise (gr332020).
#: Canonical/storage-mode grammar (``require_units=False``) — bare
#: numbers only, unchanged since before the units cutover.
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_TOKEN_RE = re.compile(rf"(rb|rt|rd|draft|pullx|pully|pullz|R|r|w|d|h|n)({_NUM})")
_CHAMFER_RE = re.compile(rf"^({_NUM})x({_NUM})$")

#: Boundary-mode grammar (``require_units=True``) — every key's number
#: may be immediately followed by a unit from :data:`LENGTH_UNIT_TOKEN`
#: (mandatory for every length key) or :data:`ANGLE_UNIT_TOKEN`
#: (mandatory for ``draft``); ``n`` and the ``pull*`` signs are
#: dimensionless and take none. The unit is optional *in the regex* so a
#: bare number still matches (letting :func:`parse` raise the structured
#: :class:`~precis.utils.units.UnitRequiredError` instead of a generic
#: syntax error).
_UNIT_TOKEN_RE = re.compile(
    rf"(rb|rt|rd|draft|pullx|pully|pullz|R|r|w|d|h|n)({_NUM})"
    rf"({LENGTH_UNIT_TOKEN}|{ANGLE_UNIT_TOKEN})?"
)
_UNIT_CHAMFER_RE = re.compile(
    rf"^({_NUM})({LENGTH_UNIT_TOKEN})?x({_NUM})({ANGLE_UNIT_TOKEN})?$"
)

#: Unit class per non-length key in the standard shape grammar
#: (:func:`parse`'s main token loop): ``draft`` is an angle (radians
#: canonical), the ``pull*`` signs are dimensionless. ``n`` keeps its
#: own branch in the loop and is deliberately not listed here.
_SHAPE_KEY_KIND: dict[str, Dimension | Literal["count"]] = {
    "draft": "angle",
    "pullx": "count",
    "pully": "count",
    "pullz": "count",
}

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
    FIELD_ALIAS: (),
    RELIEF_ALIAS: ("r", "h", "d"),
}

#: Optional keys per alias, in canonical output order, with defaults
#: applied at build. ``relief``: ``s`` sign (±1), ``n`` tile count,
#: ``k`` slide count (2|3), ``a`` draft, ``p`` parting angle, ``t``
#: twist — all of them recorded intent or modifiers, none required.
_ALIAS_OPTIONAL: dict[str, tuple[str, ...]] = {
    RELIEF_ALIAS: ("s", "n", "k", "a", "p", "t"),
}

#: The edge-rounding key (:class:`~precis.cad.primitives.Rounded`).
ROUND_KEY = "rd"

#: Aliases that accept ``rd``: every bounded convex solid with edges.
ROUNDABLE: frozenset[str] = frozenset(
    {"box", "cyl", "cone", "tcone", "hex", "ngon", "frustum", "pyramid"}
)

#: The mold-draft payload keys. ``draft`` is the taper angle (SI radians
#: in canonical storage; boundary mode takes ``deg``/``rad``);
#: ``pullx``/``pully``/``pullz`` carry the signed pull axis (±1), only
#: ``pullz`` buildable today — the frustum kernels are z-axis.
DRAFT_KEY = "draft"
PULL_KEYS: frozenset[str] = frozenset({"pullx", "pully", "pullz"})

#: Aliases that accept the draft payload — the two prims whose taper is
#: NOT already expressible in their required dims. The taper family
#: (``cone``/``tcone``/``frustum``/``pyramid``) already drafts via
#: ``rb``/``rt`` and refuses :data:`DRAFT_KEY` saying so.
DRAFTABLE: frozenset[str] = frozenset({"box", "cyl"})

#: The largest draft angle the payload admits (radians) — at 45° the side
#: face lies down flat and beyond it the "pull" inverts; the ruling caps
#: the payload below that.
DRAFT_MAX = math.pi / 4.0

#: Aliases that refuse ``rd`` by name, with the reason the refusal quotes.
_ROUND_REFUSED: dict[str, str] = {
    "sphere": "a sphere is already round — drop rd (it would be a no-op)",
    "torus": "a torus is already round — drop rd (it would be a no-op)",
    "chamfer": "a chamfer is an unbounded half-space with no extent to shrink",
    FIELD_ALIAS: (
        "rounding a sampled field is fieldops.open (convex) / close (concave) "
        "/ offset, applied to the grid before it is stored — not rd"
    ),
    RELIEF_ALIAS: (
        "rounding a relief leaf is a height-map filter applied before the "
        "map is stored — not rd"
    ),
}

#: Aliases that refuse the draft payload, with the reason the refusal
#: quotes — the taper family already expresses draft through its ``rb``/
#: ``rt`` dims.
_DRAFT_REFUSED: dict[str, str] = {
    "cone": "a cone already tapers to its apex — rb/rt IS the draft",
    "tcone": "a tcone already tapers — set rb/rt (that IS the draft)",
    "frustum": "a frustum already tapers — set rb/rt (that IS the draft)",
    "pyramid": "a pyramid already tapers to its apex — rb/rt IS the draft",
    "hex": "only box and cylinder carry draft= (a regular prism has no rb/rt)",
    "ngon": "only box and cylinder carry draft= (a regular prism has no rb/rt)",
    "sphere": "a sphere has no side faces to taper",
    "torus": "a torus has no side faces to taper",
    "chamfer": "a chamfer is an unbounded half-space, not a draftable solid",
    FIELD_ALIAS: "a sampled field cannot carry a draft — taper the grid before storing it",
    RELIEF_ALIAS: "a relief leaf drafts through its own a= angle",
}


def parse(
    config: str,
    *,
    require_units: bool = False,
    warnings: list[str] | None = None,
    arg_prefix: str = "",
) -> ShapeSpec:
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

    ``warnings`` (boundary mode only) collects advisory strings from
    :func:`~precis.utils.units.parse_quantity` — e.g. an awkward-mantissa
    ``w0.000000003m`` draws "parsed as 3 nm". Warn-only; parsing is
    unchanged.

    ``arg_prefix`` is prepended to every ``arg_name`` this parse reports —
    a caller that knows where the config sat (``"line 3: "`` from
    :mod:`precis.cad.scene`) passes it so a
    :class:`~precis.utils.units.UnitRequiredError` names the source
    position, matching the ``line N: @x`` convention the placement tokens
    already use (gr514497).
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

    if alias == FIELD_ALIAS:
        ref = rest.lower()
        if not _FIELD_REF_RE.match(ref):
            hint = f"; {_ROUND_REFUSED[FIELD_ALIAS]}" if ROUND_KEY in ref else ""
            raise DslError(
                f"field config must be 'field:<sha256>' — the stored grid's "
                f"content address, {FIELD_REF_MIN}..64 hex chars (got {rest!r})"
                f"{hint}"
            )
        return ShapeSpec(alias, {}, ref=ref)

    if alias == RELIEF_ALIAS:
        return _parse_relief(rest, config, require_units, warnings, arg_prefix)

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
                    float(size_num), "length", arg_name=f"{arg_prefix}{alias}:size"
                )
            if not angle_unit:
                raise UnitRequiredError(
                    float(angle_num), "angle", arg_name=f"{arg_prefix}{alias}:angle"
                )
            size = parse_quantity(
                f"{size_num}{unit}",
                "length",
                arg_name=f"{arg_prefix}{alias}:size",
                warnings=warnings,
            )
            angle = parse_quantity(
                f"{angle_num}{angle_unit}",
                "angle",
                arg_name=f"{arg_prefix}{alias}:angle",
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
        kind = _SHAPE_KEY_KIND.get(key, "length")
        if require_units:
            unit = m.group(3)
            if key == "n":
                if unit:
                    raise DslError(
                        f"{alias}:n is a dimensionless count, not a length — "
                        f"no unit allowed (got {m.group(0)!r})"
                    )
                value = float(num)
            elif kind == "count":
                if unit:
                    raise DslError(
                        f"{alias}:{key} is a dimensionless axis sign, not a "
                        f"quantity — no unit allowed (got {m.group(0)!r})"
                    )
                value = float(num)
            elif not unit:
                raise UnitRequiredError(
                    float(num), kind, arg_name=f"{arg_prefix}{alias}:{key}"
                )
            else:
                value = parse_quantity(
                    f"{num}{unit}",
                    kind,
                    arg_name=f"{arg_prefix}{alias}:{key}",
                    warnings=warnings,
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
    if (DRAFT_KEY in params or PULL_KEYS & params.keys()) and alias not in DRAFTABLE:
        raise DslError(
            f"{alias} does not take draft: "
            f"{_DRAFT_REFUSED.get(alias, 'only box and cylinder carry draft=')}"
        )
    if ROUND_KEY in params and alias not in ROUNDABLE:
        raise DslError(
            f"{alias} does not take rd: {_ROUND_REFUSED.get(alias, 'not roundable')}"
        )
    extra = params.keys() - required - {ROUND_KEY, DRAFT_KEY, *PULL_KEYS}
    if extra:
        raise DslError(f"{alias} got unexpected key(s) {sorted(extra)}")

    if "n" in params:
        n = params["n"]
        if n != int(n) or int(n) < 3:
            raise DslError(f"n must be an integer >= 3, got {n}")
    if DRAFT_KEY in params or PULL_KEYS & params.keys():
        _validate_draft(alias, params)
    if ROUND_KEY in params:
        shrunk(alias, params)  # validates; raises DslError naming the dimension
    return ShapeSpec(alias, params)


#: ``relief:`` grammar: numeric tokens then a terminating ``map:<sha>``.
#: The map ref must come last — a token after it would start with hex
#: characters and be ambiguous against the sha tail.
_RELIEF_RE = re.compile(rf"^(?P<num>.*?)map:(?P<ref>[0-9a-f]{{{FIELD_REF_MIN},64}})$")
_RELIEF_TOKEN_RE = re.compile(rf"(r|h|d|s|n|k|a|p|t)({_NUM})")
_RELIEF_UNIT_TOKEN_RE = re.compile(
    rf"(r|h|d|s|n|k|a|p|t)({_NUM})({LENGTH_UNIT_TOKEN}|{ANGLE_UNIT_TOKEN})?"
)

#: Per relief-key unit class: ``length`` keys take LENGTH_UNIT_TOKEN,
#: ``angle`` keys take ANGLE_UNIT_TOKEN (radians canonical), ``count``
#: keys are bare.
_RELIEF_KEY_KIND: dict[str, Dimension | Literal["count"]] = {
    "r": "length",
    "h": "length",
    "d": "length",
    "a": "angle",
    "p": "angle",
    "t": "angle",
    "s": "count",
    "n": "count",
    "k": "count",
}


def _parse_relief(
    rest: str,
    config: str,
    require_units: bool,
    warnings: list[str] | None,
    arg_prefix: str,
) -> ShapeSpec:
    """Parse the ``relief:`` body: numeric tokens then ``map:<sha>``."""
    m = _RELIEF_RE.match(rest.lower())
    if not m:
        raise DslError(
            f"relief config must end in 'map:<sha256>' — the stored height "
            f"map's content address, {FIELD_REF_MIN}..64 hex chars (got {rest!r})"
        )
    num_part, ref = m.group("num"), m.group("ref")
    token_re = _RELIEF_UNIT_TOKEN_RE if require_units else _RELIEF_TOKEN_RE
    params: dict[str, float] = {}
    pos = 0
    for tm in token_re.finditer(num_part):
        if tm.start() != pos:
            raise DslError(f"unexpected text in config near {num_part[pos:]!r}")
        key, num = tm.group(1), tm.group(2)
        if key in params:
            raise DslError(f"duplicate key {key!r} in config {config!r}")
        kind = _RELIEF_KEY_KIND[key]
        if require_units:
            unit = tm.group(3)
            if kind == "count":
                if unit:
                    raise DslError(
                        f"relief:{key} is a dimensionless count/sign, not a "
                        f"quantity — no unit allowed (got {tm.group(0)!r})"
                    )
                value = float(num)
            elif not unit:
                raise UnitRequiredError(
                    float(num), kind, arg_name=f"{arg_prefix}{RELIEF_ALIAS}:{key}"
                )
            else:
                value = parse_quantity(
                    f"{num}{unit}",
                    kind,
                    arg_name=f"{arg_prefix}{RELIEF_ALIAS}:{key}",
                    warnings=warnings,
                )
        else:
            value = float(num)
        params[key] = value
        pos = tm.end()
    if pos != len(num_part):
        raise DslError(f"unexpected text in config near {num_part[pos:]!r}")
    missing = set(_ALIAS_KEYS[RELIEF_ALIAS]) - params.keys()
    if missing:
        raise DslError(
            f"relief needs {sorted(_ALIAS_KEYS[RELIEF_ALIAS])}, missing "
            f"{sorted(missing)} in {config!r}"
        )
    extra = (
        params.keys()
        - set(_ALIAS_KEYS[RELIEF_ALIAS])
        - set(_ALIAS_OPTIONAL[RELIEF_ALIAS])
    )
    if extra:
        raise DslError(f"relief got unexpected key(s) {sorted(extra)}")
    if ROUND_KEY in params:
        raise DslError(f"relief does not take rd: {_ROUND_REFUSED[RELIEF_ALIAS]}")
    _validate_relief(params)
    return ShapeSpec(RELIEF_ALIAS, params, ref=ref)


def _validate_relief(p: dict[str, float]) -> None:
    if not (p["r"] > 0.0 and p["h"] > 0.0):
        raise DslError("relief needs r > 0 and h > 0")
    if not (p["d"] >= 0.0):
        raise DslError(f"relief depth must be >= 0, got {p['d']}")
    if "s" in p and p["s"] not in (-1.0, 1.0):
        raise DslError(f"relief s must be 1 (emboss) or -1 (deboss), got {p['s']}")
    if "n" in p and (p["n"] != int(p["n"]) or p["n"] < 1):
        raise DslError(f"relief n (tile count) must be an integer >= 1, got {p['n']}")
    if "k" in p and p["k"] not in (2.0, 3.0):
        raise DslError(f"relief k (slide count) must be 2 or 3, got {p['k']}")
    for key in ("a", "p"):
        if key in p and not (0.0 <= p[key] <= math.pi):
            raise DslError(f"relief {key} must lie in [0, 180°], got {p[key]} rad")


def _pull_sign(p: dict[str, float]) -> int:
    """The draft pull direction: ``+1`` toward ``+z`` (the default), ``-1``
    toward the base. Only ``pullz`` is buildable — parse refuses the other
    axes before a spec can carry them."""
    return -1 if p.get("pullz", 1.0) < 0.0 else 1


def draft_inset(p: dict[str, float]) -> float:
    """How far each side face steps inward over the full height:
    ``h·tan(draft)``."""
    return p["h"] * math.tan(p[DRAFT_KEY])


def draft_box_rings(
    p: dict[str, float],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """``(bottom, top)`` vertex rings of a drafted box — the full-size
    ``w × d`` ring at the pull-far end, inset by ``h·tan(draft)`` per side
    at the pull-near end (so the pull-side face is the smaller one)."""
    hw, hd = p["w"] / 2.0, p["d"] / 2.0
    ins = draft_inset(p)
    full = [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)]
    small = [
        (-hw + ins, -hd + ins),
        (hw - ins, -hd + ins),
        (hw - ins, hd - ins),
        (-hw + ins, hd - ins),
    ]
    return (full, small) if _pull_sign(p) > 0 else (small, full)


def draft_cyl_ends(p: dict[str, float]) -> tuple[float, float]:
    """``(rb, rt)`` of a drafted cylinder — full radius at the pull-far
    end, ``r - h·tan(draft)`` at the pull-near end."""
    ins = draft_inset(p)
    return (p["r"], p["r"] - ins) if _pull_sign(p) > 0 else (p["r"] - ins, p["r"])


def _draft_end_limit(alias: str, p: dict[str, float]) -> tuple[float, str]:
    """The largest side-step :func:`draft_inset` may take before the
    pull-side face vanishes, and the dimension that sets it."""
    if alias == "box":
        lim = min(p["w"], p["d"]) / 2.0
        return lim, (
            f"½·min(w, d) = ½·min({format_dsl_number(p['w'])}, "
            f"{format_dsl_number(p['d'])})"
        )
    return p["r"], f"r = {format_dsl_number(p['r'])}"


def _validate_draft(alias: str, p: dict[str, float]) -> None:
    """Refuse a draft payload the geometry cannot honour — bad angles and
    pull directions with :data:`DRAFT_KEY`'s ``next=`` recovery hints, and
    an over-deep taper named by the dimension it would erase (the kernel
    never clamps). Called by :func:`parse` for the draftable aliases."""
    pulls = sorted(PULL_KEYS & p.keys())
    if DRAFT_KEY not in p:
        raise DslError(
            f"{alias}: {'/'.join(pulls)} sets a draft pull direction, but no "
            "draft angle was given — the pull only means something with a draft",
            next=f"add a draft angle, e.g. "
            f"{alias}:…draft2deg{pulls[0] if pulls else 'pullz-1'}",
        )
    if len(pulls) > 1:
        raise DslError(
            f"{alias}: at most one pull key, got {pulls} — pick one pull axis",
            next="drop the extra pull key (pullz1 = up the +z face, "
            "pullz-1 = down toward the base)",
        )
    if pulls and pulls[0] in ("pullx", "pully"):
        raise DslError(
            f"{alias}: draft pull along {pulls[0][-1]} is not supported — the "
            "draft kernels (CircularFrustum/PolyFrustum) are z-axis only",
            next="author the prim with pullz±1 and tilt the pull direction "
            "with the node's rot: placement instead",
        )
    if "pullz" in p and p["pullz"] not in (-1.0, 1.0):
        raise DslError(
            f"{alias}: pullz is the signed z axis — pullz1 (up, the default) "
            f"or pullz-1 (down toward the base), got pullz={format_dsl_number(p['pullz'])}",
            next="use pullz1 or pullz-1",
        )
    draft = p[DRAFT_KEY]
    if not draft > 0.0:
        raise DslError(
            f"{alias}: draft must be a positive taper angle below 45° "
            f"(got {format_dsl_number(draft)}) — drop draft= for vertical sides",
            next="set draft to the taper angle in degrees, e.g. draft2deg "
            "(must be > 0 and < 45°)",
        )
    if draft >= DRAFT_MAX:
        deg = format_dsl_number(math.degrees(draft))
        raise DslError(
            f"{alias}: draft={deg}° >= 45° — at that slant the side face lies "
            "down flat and the payload stops meaning a draft",
            next="use a draft below 45° (mold draft is typically 0.5–5°)",
        )
    lim, who = _draft_end_limit(alias, p)
    ins = draft_inset(p)
    if ins >= lim:
        deg = format_dsl_number(math.degrees(draft))
        raise DslError(
            f"{alias}: draft={deg}° insets each side by {format_dsl_number(ins)} "
            f"over h — that reaches {who}, erasing the pull-side face",
            next="reduce draft or enlarge the cross-section it tapers",
        )


def _round_limit(alias: str, params: dict[str, float]) -> tuple[float, str]:
    """The largest ``rd`` the ½·min-dimension rule allows and the name of
    the dimension that sets it (``"h"``, ``"r"``, ``"w"`` …). A drafted
    prim measures its min at the pull-side (small) end."""
    p = params
    limits: list[tuple[float, str]] = []
    if alias == "box":
        if DRAFT_KEY in p:
            ins = draft_inset(p)
            limits = [
                (p["w"] / 2 - ins, "w at the drafted end"),
                (p["d"] / 2 - ins, "d at the drafted end"),
                (p["h"] / 2, "h"),
            ]
        else:
            limits = [(p["w"] / 2, "w"), (p["d"] / 2, "d"), (p["h"] / 2, "h")]
    elif alias in ("cyl", "cone"):
        if alias == "cyl" and DRAFT_KEY in p:
            limits = [
                (p["r"] - draft_inset(p), "r at the drafted end"),
                (p["h"] / 2, "h"),
            ]
        else:
            limits = [(p["r"], "r (the diameter 2r)"), (p["h"] / 2, "h")]
    elif alias == "tcone":
        limits = [(p["rb"], "rb"), (p["rt"], "rt"), (p["h"] / 2, "h")]
    elif alias in ("hex", "ngon", "pyramid"):
        n = 6 if alias == "hex" else int(p["n"])
        apothem = p["r"] * math.cos(math.pi / n)
        limits = [(apothem, f"r (across-flats 2·r·cos(π/{n}))"), (p["h"] / 2, "h")]
    elif alias == "frustum":
        n = int(p["n"])
        c = math.cos(math.pi / n)
        limits = [
            (p["rb"] * c, f"rb (across-flats 2·rb·cos(π/{n}))"),
            (p["rt"] * c, f"rt (across-flats 2·rt·cos(π/{n}))"),
            (p["h"] / 2, "h"),
        ]
    return min(limits)


def _shrunk_slant(
    ab: float, at: float, h: float, rd: float
) -> tuple[float, float, float]:
    """Inward parallel body of the meridian trapezoid ``(ab, 0)–(at, h)``
    at offset ``rd`` — the shrunk bottom/top radii (or apothems) and
    height. Caps move in by ``rd``; the lateral edge moves by ``rd``
    along its own normal, which changes the radii by ``rd·(L ∓ (at−ab))/h``
    (``L`` the slant length) — exactly ``rd`` only for a cylinder. When
    the top closes up (``at' <= 0``: a cone, or a steep taper) the shrunk
    body is the cone the offset lateral line makes with the base, height
    from where that line meets the axis. Returns ``(ab', at', h')``."""
    slant = math.hypot(at - ab, h)
    ab2 = ab - rd * (slant - (at - ab)) / h
    at2 = at - rd * (slant + (at - ab)) / h
    h2 = h - 2 * rd
    if at2 <= 0.0 and ab2 > 0.0:
        # apex case: the offset lateral line meets the axis below h - rd
        h2 = ab2 * h2 / (ab2 - at2)
        at2 = 0.0
    return ab2, at2, h2


def shrunk(alias: str, params: dict[str, float]) -> dict[str, float]:
    """The parameters of ``alias`` shrunk by ``params['rd']`` on every side
    — what :func:`build` wraps in :class:`~precis.cad.primitives.Rounded`.

    Raises :class:`DslError` (naming the shape and the dimension) when
    ``rd >= ½·min_dimension`` — the thin-feature rule: the shrunk shape
    would be empty or a face would vanish, and the kernel never clamps —
    or when a cone/pyramid's shrunk body closes up at its slant (the
    offset lateral line meets the axis below ``z = rd``; the ½ rule alone
    does not exclude that for an apex shape, whereas it does keep both
    caps of a ``tcone``/``frustum``).
    """
    rd = params[ROUND_KEY]
    if not rd > 0.0:
        raise DslError(f"{alias}: rd must be > 0 (got {rd}); drop rd for sharp edges")
    limit, dim = _round_limit(alias, params)
    if rd >= limit:
        raise DslError(
            f"{alias}: rd={format_dsl_number(rd)} >= ½·min dimension — the "
            f"round would erase {dim}; rd must be < {format_dsl_number(limit)}"
        )
    p = params
    out: dict[str, float]
    if alias == "box":
        if DRAFT_KEY in p:
            # Exact inward offset of the drafted box: the full-end faces
            # move in by rd·(√(1+k²) + k) per side (the side plane's own
            # normal carries the k term), the caps by rd, and the slope k
            # is preserved — so the shrunk body is the drafted box with
            # smaller full-end dims and height h − 2·rd.
            k = math.tan(p[DRAFT_KEY])
            corr = rd * (math.sqrt(1.0 + k * k) + k)
            out = {
                "w": p["w"] - 2.0 * corr,
                "d": p["d"] - 2.0 * corr,
                "h": p["h"] - 2.0 * rd,
                DRAFT_KEY: p[DRAFT_KEY],
            }
            if "pullz" in p:
                out["pullz"] = p["pullz"]
        else:
            out = {"w": p["w"] - 2 * rd, "d": p["d"] - 2 * rd, "h": p["h"] - 2 * rd}
    elif alias == "cyl":
        if DRAFT_KEY in p:
            # Same exact offset through _shrunk_slant's arithmetic: a
            # drafted cylinder is a circular frustum, whose offset keeps
            # the slope and moves the full-end radius in by the same
            # rd·(√(1+k²) + k).
            k = math.tan(p[DRAFT_KEY])
            corr = rd * (math.sqrt(1.0 + k * k) + k)
            out = {
                "r": p["r"] - corr,
                "h": p["h"] - 2.0 * rd,
                DRAFT_KEY: p[DRAFT_KEY],
            }
            if "pullz" in p:
                out["pullz"] = p["pullz"]
        else:
            out = {"r": p["r"] - rd, "h": p["h"] - 2 * rd}
    elif alias == "cone":
        rb2, _rt2, h2 = _shrunk_slant(p["r"], 0.0, p["h"], rd)
        out = {"r": rb2, "h": h2}
    elif alias == "tcone":
        rb2, rt2, h2 = _shrunk_slant(p["rb"], p["rt"], p["h"], rd)
        out = {"rb": rb2, "rt": rt2, "h": h2}
    elif alias in ("hex", "ngon"):
        n = 6 if alias == "hex" else int(p["n"])
        c = math.cos(math.pi / n)
        out = {"r": p["r"] - rd / c, "h": p["h"] - 2 * rd}
        if alias == "ngon":
            out["n"] = p["n"]
    elif alias == "pyramid":
        n = int(p["n"])
        c = math.cos(math.pi / n)
        a2, _t2, h2 = _shrunk_slant(p["r"] * c, 0.0, p["h"], rd)
        out = {"n": p["n"], "r": a2 / c, "h": h2}
    elif alias == "frustum":
        n = int(p["n"])
        c = math.cos(math.pi / n)
        ab2, at2, h2 = _shrunk_slant(p["rb"] * c, p["rt"] * c, p["h"], rd)
        out = {"n": p["n"], "rb": ab2 / c, "rt": at2 / c, "h": h2}
    else:  # pragma: no cover - parse guards alias
        raise DslError(f"{alias} does not take rd")
    # Only an apex shape can still close up here: for a tcone/frustum the
    # ½ rule provably keeps both cap faces (rd < min(rb, rt, h/2) ⇒
    # rb', rt' > 0 for every slant), but a cone/pyramid's offset lateral
    # line can meet the axis below z = rd, leaving no shrunk body at all.
    for key, value in out.items():
        if key != "n" and key not in PULL_KEYS and not value > 0.0 and key != "rt":
            raise DslError(
                f"{alias}: rd={format_dsl_number(rd)} closes the shrunk shape "
                f"up at its slant ({key} would be {format_dsl_number(value)}) "
                "— use a smaller rd"
            )
    if DRAFT_KEY in out:
        # The drafted end shrinks by the same caps step AND keeps the full
        # end's correction — its half-extent must stay positive.
        k = math.tan(out[DRAFT_KEY])
        if alias == "box":
            small = min(out["w"], out["d"]) / 2.0 - out["h"] * k
        else:
            small = out["r"] - out["h"] * k
        if not small > 0.0:
            raise DslError(
                f"{alias}: rd={format_dsl_number(rd)} closes the drafted end "
                f"up (its half-extent would be {format_dsl_number(small)}) — "
                "use a smaller rd or draft"
            )
    return out


def build(
    spec: ShapeSpec,
    *,
    field_loader: FieldLoader | None = None,
    hmap_loader: HmapLoader | None = None,
) -> Primitive:
    """Build a kernel :class:`Primitive` from a :class:`ShapeSpec`.

    A ``field:<sha>`` spec is resolved through ``field_loader`` (a
    :data:`FieldLoader`); without one it is a :class:`DslError` naming the
    reference — the grid lives in the store, and this module never does.
    The loader's own ``LookupError``/``ValueError`` (unknown or ambiguous
    reference) is re-raised as a ``DslError`` carrying its text.

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
    if a == FIELD_ALIAS:
        ref = spec.ref or ""
        if field_loader is None:
            raise DslError(
                f"field:{ref} needs a field loader to build — load the design "
                "through the store (cad_load) or pass field_loader= to "
                "build_design/build_config; the grid is never inlined in the DSL"
            )
        try:
            return field_loader(ref)
        except (LookupError, ValueError) as exc:
            raise DslError(f"field:{ref}: {exc}") from exc
    if a == RELIEF_ALIAS:
        ref = spec.ref or ""
        if hmap_loader is None:
            raise DslError(
                f"relief map:{ref} needs a height-map loader to build — load "
                "the design through the store (cad_load) or pass hmap_loader= "
                "to build_design/build_config; the map is never inlined in "
                "the DSL"
            )
        try:
            hm = hmap_loader(ref)
        except (LookupError, ValueError) as exc:
            raise DslError(f"relief map:{ref}: {exc}") from exc
        try:
            return CylRelief(
                r=p["r"],
                h=p["h"],
                hmap=hm,
                depth=p["d"],
                s=p.get("s", 1.0),
                repeat=int(p.get("n", 1)),
                twist=p.get("t", 0.0),
                k=int(p["k"]) if "k" in p else None,
                alpha=p.get("a"),
                phi=p.get("p"),
            )
        except ValueError as exc:
            raise DslError(f"relief: {exc}") from exc
    if ROUND_KEY in p:
        rd = p[ROUND_KEY]
        inner = build(ShapeSpec(a, shrunk(a, p)))
        # Every roundable shape is base-at-z=0; lift the shrunk body by rd
        # so the rounded solid's base plane stays at z=0 (contract).
        return Rounded(inner=inner, r=rd, lift=rd)
    return _build_sharp(a, p)


def _build_sharp(alias: str, p: dict[str, float]) -> Primitive:
    """The un-rounded primitive for a parsed spec (which may still carry
    the draft payload — a drafted box/cyl builds as the frustum whose ends
    :func:`draft_box_rings`/:func:`draft_cyl_ends` describe)."""
    if alias == "box":
        if DRAFT_KEY in p:
            bottom, top = draft_box_rings(p)
            return PolyFrustum(bottom, top, p["h"])
        return box(p["w"], p["d"], p["h"])
    if alias == "cyl":
        if DRAFT_KEY in p:
            rb, rt = draft_cyl_ends(p)
            return CircularFrustum(rb=rb, rt=rt, h=p["h"])
        return CircularFrustum(rb=p["r"], rt=p["r"], h=p["h"])
    if alias == "cone":
        return CircularFrustum(rb=p["r"], rt=0.0, h=p["h"])
    if alias == "tcone":
        return CircularFrustum(rb=p["rb"], rt=p["rt"], h=p["h"])
    if alias == "sphere":
        return Sphere(r=p["r"])
    if alias == "torus":
        return Torus(R=p["R"], r=p["r"])
    if alias == "hex":
        return regular_prism(6, p["r"], p["h"])
    if alias == "ngon":
        return regular_prism(int(p["n"]), p["r"], p["h"])
    if alias == "frustum":
        return regular_frustum(int(p["n"]), p["rb"], p["rt"], p["h"])
    if alias == "pyramid":
        return pyramid(int(p["n"]), p["r"], p["h"])
    if alias == "chamfer":
        angle = p["angle"]
        size = p["size"]
        sa, ca = math.sin(angle), math.cos(angle)
        return HalfSpace(
            point=vec3(-size * sa, 0.0, -size * ca), normal=vec3(-sa, 0.0, -ca)
        )
    raise DslError(
        f"{alias!r} is not a buildable shape"
    )  # pragma: no cover - parse guards alias


def build_config(
    config: str,
    *,
    require_units: bool = False,
    field_loader: FieldLoader | None = None,
    hmap_loader: HmapLoader | None = None,
) -> Primitive:
    """Convenience: ``parse`` then ``build``. See :func:`parse` for
    ``require_units``'s two-mode contract and :func:`build` for
    ``field_loader``/``hmap_loader``."""
    return build(
        parse(config, require_units=require_units),
        field_loader=field_loader,
        hmap_loader=hmap_loader,
    )


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
    if spec.alias == FIELD_ALIAS:
        return f"{FIELD_ALIAS}:{spec.ref or ''}"
    if spec.alias == RELIEF_ALIAS:
        tokens: list[str] = []
        for key in (*_ALIAS_KEYS[RELIEF_ALIAS], *_ALIAS_OPTIONAL[RELIEF_ALIAS]):
            if key not in spec.params:
                continue
            suffix_k = (
                ""
                if _RELIEF_KEY_KIND[key] == "count"
                else ("rad" if units and _RELIEF_KEY_KIND[key] == "angle" else suffix)
            )
            tokens.append(f"{key}{format_dsl_number(spec.params[key])}{suffix_k}")
        return f"{RELIEF_ALIAS}:{''.join(tokens)}map:{spec.ref or ''}"
    if spec.alias == "chamfer":
        return (
            f"chamfer:{format_dsl_number(spec.params['size'])}{suffix}"
            f"x{format_dsl_number(spec.params['angle'])}{angle_suffix}"
        )
    keys: tuple[str, ...] = _ALIAS_KEYS[spec.alias]
    for opt in (DRAFT_KEY, *PULL_KEYS):
        if opt in spec.params:
            keys = (*keys, opt)
    if ROUND_KEY in spec.params:
        keys = (*keys, ROUND_KEY)
    parts = "".join(
        f"{key}{format_dsl_number(spec.params[key])}"
        f"{'rad' if units and key == DRAFT_KEY else ('' if key in ('n', *PULL_KEYS) else suffix)}"
        for key in keys
    )
    return f"{spec.alias}:{parts}"


def _is_length_key(alias: str, key: str) -> bool:
    """Whether ``key`` carries a length under a uniform scale — the keys
    :func:`scale_config` multiplies. Counts (``n``, the ``pull*`` signs) and
    angles (chamfer's ``angle``, a prim's ``draft``, a ``relief``'s
    draft/parting/twist) are scale-invariant."""
    if alias == RELIEF_ALIAS:
        return _RELIEF_KEY_KIND.get(key) == "length"
    if alias == "chamfer":
        return key == "size"
    return key not in ("n", *PULL_KEYS, DRAFT_KEY)


def scale_config(config: str, factor: float) -> str:
    """Uniformly scale a canonical ``config``'s length dimensions by
    ``factor`` about the local origin, re-emitting canonical text.

    Operates on the parsed :class:`ShapeSpec` params (never the string), so
    a ``relief:``'s ``map:<sha>`` tail and every angle/count pass through
    untouched. A sampled ``field:`` leaf carries no scalable dims — its
    grid's extent is baked into the stored blob — so it is refused: rescale
    the grid before storing it (``fieldops``) or stay at ``factor == 1``.
    Mirrors :meth:`precis.cad.primitives.CylRelief.scaled` for a relief,
    which scales ``r``/``h``/``depth`` together and keeps the normalized
    map."""
    if factor <= 0.0:
        raise DslError(f"scale_config factor must be > 0, got {factor!r}")
    spec = parse(config)
    if spec.alias == FIELD_ALIAS:
        raise DslError(
            f"field:{spec.ref or ''} cannot be uniformly scaled by config — "
            "its grid extent is baked into the stored blob; rescale the field "
            "before storing (fieldops) or use factor 1"
        )
    scaled = {
        key: (val * float(factor) if _is_length_key(spec.alias, key) else val)
        for key, val in spec.params.items()
    }
    return format_spec(ShapeSpec(spec.alias, scaled, ref=spec.ref))
