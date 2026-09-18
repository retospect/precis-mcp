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

from precis.cad.primitives import (
    CircularFrustum,
    Field,
    HalfSpace,
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
#: the full 64).
FIELD_REF_MIN = 12
_FIELD_REF_RE = re.compile(rf"^[0-9a-f]{{{FIELD_REF_MIN},64}}$")

#: Resolves a ``field:`` reference (full sha256 or accepted prefix) to the
#: loaded :class:`~precis.cad.primitives.Field`. Injected — the kernel
#: never reaches for the store; the one production loader is built by
#: ``Store.cad_load`` and rides on :attr:`precis.cad.scene.SceneSpec.field_loader`.
FieldLoader = Callable[[str], Field]


#: ``<key><number>`` — keys longest-first so ``rb``/``rt``/``rd`` beat ``r``.
#: Numbers take an optional exponent (``3e-9``) — unambiguous since no
#: DSL key is ``e``; nm-scale dims are unreadable otherwise (gr332020).
#: Canonical/storage-mode grammar (``require_units=False``) — bare
#: numbers only, unchanged since before the units cutover.
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_TOKEN_RE = re.compile(rf"(rb|rt|rd|R|r|w|d|h|n)({_NUM})")
_CHAMFER_RE = re.compile(rf"^({_NUM})x({_NUM})$")

#: Boundary-mode grammar (``require_units=True``) — every key's number
#: may be immediately followed by one of :data:`LENGTH_UNIT_TOKEN`
#: (mandatory for every key but ``n``, the dimensionless count; forbidden
#: for ``n``). The unit is optional *in the regex* so a bare number still
#: matches (letting :func:`parse` raise the structured
#: :class:`~precis.utils.units.UnitRequiredError` instead of a generic
#: syntax error).
_UNIT_TOKEN_RE = re.compile(rf"(rb|rt|rd|R|r|w|d|h|n)({_NUM})({LENGTH_UNIT_TOKEN})?")
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
    FIELD_ALIAS: (),
}

#: The edge-rounding key (:class:`~precis.cad.primitives.Rounded`).
ROUND_KEY = "rd"

#: Aliases that accept ``rd``: every bounded convex solid with edges.
ROUNDABLE: frozenset[str] = frozenset(
    {"box", "cyl", "cone", "tcone", "hex", "ngon", "frustum", "pyramid"}
)

#: Aliases that refuse ``rd`` by name, with the reason the refusal quotes.
_ROUND_REFUSED: dict[str, str] = {
    "sphere": "a sphere is already round — drop rd (it would be a no-op)",
    "torus": "a torus is already round — drop rd (it would be a no-op)",
    "chamfer": "a chamfer is an unbounded half-space with no extent to shrink",
    FIELD_ALIAS: (
        "rounding a sampled field is fieldops.open (convex) / close (concave) "
        "/ offset, applied to the grid before it is stored — not rd"
    ),
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
    if ROUND_KEY in params and alias not in ROUNDABLE:
        raise DslError(
            f"{alias} does not take rd: {_ROUND_REFUSED.get(alias, 'not roundable')}"
        )
    extra = params.keys() - required - {ROUND_KEY}
    if extra:
        raise DslError(f"{alias} got unexpected key(s) {sorted(extra)}")

    if "n" in params:
        n = params["n"]
        if n != int(n) or int(n) < 3:
            raise DslError(f"n must be an integer >= 3, got {n}")
    if ROUND_KEY in params:
        shrunk(alias, params)  # validates; raises DslError naming the dimension
    return ShapeSpec(alias, params)


def _round_limit(alias: str, params: dict[str, float]) -> tuple[float, str]:
    """The largest ``rd`` the ½·min-dimension rule allows and the name of
    the dimension that sets it (``"h"``, ``"r"``, ``"w"`` …)."""
    p = params
    limits: list[tuple[float, str]] = []
    if alias == "box":
        limits = [(p["w"] / 2, "w"), (p["d"] / 2, "d"), (p["h"] / 2, "h")]
    elif alias in ("cyl", "cone"):
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
        out = {"w": p["w"] - 2 * rd, "d": p["d"] - 2 * rd, "h": p["h"] - 2 * rd}
    elif alias == "cyl":
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
        if key != "n" and not value > 0.0 and key != "rt":
            raise DslError(
                f"{alias}: rd={format_dsl_number(rd)} closes the shrunk shape "
                f"up at its slant ({key} would be {format_dsl_number(value)}) "
                "— use a smaller rd"
            )
    return out


def build(spec: ShapeSpec, *, field_loader: FieldLoader | None = None) -> Primitive:
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
    if ROUND_KEY in p:
        rd = p[ROUND_KEY]
        inner = build(ShapeSpec(a, shrunk(a, p)))
        # Every roundable shape is base-at-z=0; lift the shrunk body by rd
        # so the rounded solid's base plane stays at z=0 (contract).
        return Rounded(inner=inner, r=rd, lift=rd)
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


def build_config(
    config: str,
    *,
    require_units: bool = False,
    field_loader: FieldLoader | None = None,
) -> Primitive:
    """Convenience: ``parse`` then ``build``. See :func:`parse` for
    ``require_units``'s two-mode contract and :func:`build` for
    ``field_loader``."""
    return build(parse(config, require_units=require_units), field_loader=field_loader)


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
    if spec.alias == "chamfer":
        return (
            f"chamfer:{format_dsl_number(spec.params['size'])}{suffix}"
            f"x{format_dsl_number(spec.params['angle'])}{angle_suffix}"
        )
    keys: tuple[str, ...] = _ALIAS_KEYS[spec.alias]
    if ROUND_KEY in spec.params:
        keys = (*keys, ROUND_KEY)
    parts = "".join(
        f"{key}{format_dsl_number(spec.params[key])}{'' if key == 'n' else suffix}"
        for key in keys
    )
    return f"{spec.alias}:{parts}"
