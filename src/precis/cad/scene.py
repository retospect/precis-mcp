"""Design source ↔ scene spec ↔ live :class:`Design`.

The MCP ``put`` surface only carries ``id`` / ``text`` / ``mode`` (no
arbitrary kwargs), so a CAD design is *authored as text*: a small
line-based language, one node per line, that this module parses into a
:class:`SceneSpec` (a flat, serialisable node list — exactly what the
handler persists as chunks) and *builds* into the in-memory
:class:`~precis.cad.graph.Design` the probe / relate layers run on.

Grammar (whitespace-separated tokens; ``#`` starts a comment)::

    # a flange
    component flange
    plate     add  cyl:r25mmh8mm
    hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm
    bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm
    rim       add  box:w4mmd4mmh4mm @20mm,0mm,0mm   rot:0,0,45

Every dimensioned token (``@x,y,z`` — one unit per component, ``polar:``'s
``r``, ``linear:``'s ``dx``/``dy``/``dz``, ``pitch:``, ``dim``'s value, and
the ``config`` mini-DSL's own length keys) requires an explicit unit from
:data:`~precis.utils.units.LENGTH_UNIT_TOKEN` — a bare number raises
:class:`~precis.utils.units.UnitRequiredError` with a retry hint (the
zero-counting / exponent-slip guard). ``rot:``/``spin:``/angle-kind
``limits:`` require an explicit unit too, from
:data:`~precis.utils.units.ANGLE_UNIT_TOKEN` (``deg``/``rad``) — the same
guard, angle-flavoured (the decisions log's angle ruling: radians
internal, explicit unit at ingest). ``ratio:`` (dimensionless) never takes
a unit. The one exemption: a ``{name}``-parametrized ``config`` (below)
parses its own literal numbers bare (canonical/SI: metres, radians), same
as the stored form every design reloads from — only the ``dim``
declaration that pins the referenced value still requires a unit. Stored
``config``/pose text is always canonical (bare, SI metres/radians); units
exist only at this text boundary, converted exactly once.

- ``desc: <text>`` / ``use: <text>`` (optional, anywhere) record what the
  design *is* and what it's *for*; folded into the searchable card.
- ``component <name>`` opens a part; nodes until the next ``component``
  belong to it (default part name ``part`` if none is declared).
- ``use <slug> as <name> [@x,y,z] [rot:...] [polar:/linear:]`` instances
  *another design* as a sub-assembly (:func:`expand_instances`). It is a
  top-level directive like ``component`` — it does not join, or close, the
  enclosing component block.
- ``part <name> <family>:<code> [@x,y,z] [rot:...] [polar:/linear:]`` places
  a **catalog atom** (:mod:`precis.cad.catalog` — bearings, bolts, rails,
  steppers…): an envelope + ports inlined at expansion exactly like ``use``,
  but resolved from the built-in catalog, so a parts-only design needs no
  store resolver. The catalog's ``part_slug`` is the procurement identity
  the handler links ``realized-by`` to a ``component`` ref and rolls into
  ``view='bom'``.
- ``port <name> [@x,y,z] [rot:...] [type:<t>] [of:<component>]`` names a
  frame on *this* design — the interface another design mates to. ``type:``
  is a free compatibility tag (two typed ports may only mate when the types
  match); ``of:`` scopes the port to a component (required for the pivot of
  a component ``joint``).
- ``dim <name> =|>=|<= <value+unit>`` / ``constrain <a> = <b>`` — named
  dimension bounds (one-sided allowed; bounds intersect) and equality
  constraints between dims. An impossible combination — ``a = 200``,
  ``b = 150``, ``constrain a = b`` — is **refused at parse**
  (:func:`_effective_dims`); the kernel never carries known-false
  declarations. A config may reference a dim as ``{name}``
  (``box:w{a}d{b}h10``): the stored spec keeps the parametric string and
  geometry resolves it at expansion (:func:`_resolve_node_dims`) — the
  dim (or its equality class) must be **pinned** to an exact value; a
  still-open bound is refused, never silently averaged. Sub-designs
  resolve against their own dim namespace. Payload configs may not
  reference dims (they splice into another design's namespace).
- ``material <component> <slug>`` — assign a ``material`` kind slug
  (drives the handler's ``view='mass'``).
- ``payload <name> <op> <config> at:<port> [@x,y,z] [rot:...]`` — geometry
  the port *brings to whatever it mates against* (a hinge's knuckle recess,
  its pin bore): when the port mates, each payload is spliced into the
  component on the **other** side of the mate as a node named
  ``<instance>~<name>``. Placement is relative to the port frame; ``op`` ∈
  {``add``, ``cut``}; the far side's port must be scoped ``of:`` a
  component (the host body). See :class:`PayloadSpec`.
- ``mate <inst>.<port> to <anchor> [flip] [spin:<angle>]`` places instance
  ``<inst>`` by making its port coincide with ``<anchor>`` (this design's own
  ``<port>``, or another instance's ``<inst>.<port>``) — see
  :func:`_solve_interfaces`. Also top-level. A mate is sugar for a ``fixed``
  joint.
- ``joint <inst>.<port> to <anchor> <kind> [limits:lo..hi] [pitch:<length>]
  [flip] [spin:<angle>]`` — an articulated mate: ``kind`` ∈ {``fixed``,
  ``revolute``, ``prismatic``, ``cylindrical``, ``screw``}. Motion is about /
  along the **anchor frame's z axis**; state is radians (revolute, screw,
  cylindrical angle) or metres (prismatic, cylindrical slide) internally;
  ``limits:``/``spin:`` at the text boundary always carry an explicit unit
  (a length unit for ``prismatic``, ``deg``/``rad`` for every other kind's
  angle limits). ``pitch:`` (screw only) is always a length.
- ``joint <component> <kind> at:<port> [limits:lo..hi] [pitch:<length>]`` —
  articulates a whole component of *this* design about a port scoped
  ``of:`` that component (the port names which body the frame rides on).
- ``gear <a> to <b> ratio:<r>`` / ``belt …`` — couples joint ``b``'s state
  to ``ratio × a``'s (the sign carries the sense; ``gear``/``belt`` record
  intent, the math is identical).
- Posing: ``expand_instances(..., state={'<joint>': q})`` — a joint's name
  is its subject instance / component. Defaults to 0 (clamped into
  ``limits:``); an *explicit* out-of-limits state is an error.
- ``<name> <op> <config> [@x,y,z] [rot:rx,ry,rz] [polar:nNrR] [linear:nNdx..dy..dz..]``
  — ``op`` ∈ {``add``, ``cut``, ``intersect``}; ``config`` is the §11
  mini-DSL (:mod:`precis.cad.dsl`). The first node in a part is its base;
  later ``add`` merges, ``cut`` subtracts, ``intersect`` intersects.
  ``chamfer:`` nodes are unbounded half-space tools, so they may only be
  ``cut``/``intersect`` (never ``add``, which would leave the component an
  infinite solid) and may never be a component's first (base) node.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypedDict

from precis.cad import catalog
from precis.cad.dsl import build, build_config, format_spec, parse
from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.vec import (
    Transform,
    as_float3,
    euler_rad_from_matrix,
    identity,
    rotation,
    translation,
)
from precis.utils.units import (
    ANGLE_UNIT_TOKEN,
    LENGTH_UNIT_TOKEN,
    UnitRequiredError,
    parse_quantity,
)

log = logging.getLogger(__name__)

_OPS = ("add", "cut", "intersect")

#: Ops a port payload may use. Never ``intersect`` — an intersect payload
#: would replace its whole host with the overlap, not feature it.
_PAYLOAD_OPS = ("add", "cut")

#: Separator in expansion-generated payload node names
#: (``<instance>~<payload>``). Outside :data:`_IDENT_RE`, so a spliced node
#: can never collide with an authored one — and the handler recognises a
#: spliced payload row by it for attribution.
PAYLOAD_SEP = "~"

#: A node whose ``config`` starts with this instances another design rather
#: than building a primitive — see :func:`expand_instances`.
INSTANCE_PREFIX = "use:"

#: A node whose ``config`` starts with this places a built-in catalog part
#: (``part:<family>:<code>`` — :mod:`precis.cad.catalog`); inlined like an
#: instance but needing no resolver.
PART_PREFIX = "part:"

#: Separator between an instance's name and the sub-design's own node /
#: component names once inlined (``f1.plate``).
NAMESPACE_SEP = "."

#: How deep ``use`` may nest before we call it a runaway.
MAX_INSTANCE_DEPTH = 8

#: Ceiling on the expanded node count — a 6-deep tree of 6-way polar
#: instances is 46656 nodes, which would wedge a worker rather than fail.
MAX_EXPANDED_NODES = 20_000

#: Every dimensioned scene token requires an explicit unit — a length
#: unit from :data:`~precis.utils.units.LENGTH_UNIT_TOKEN`, or (``rot:``/
#: ``spin:``/angle-kind ``limits:``) an angle unit from
#: :data:`~precis.utils.units.ANGLE_UNIT_TOKEN`. The unit is optional
#: *in the regex* only so a bare number still matches (letting the caller
#: raise the structured :class:`~precis.utils.units.UnitRequiredError`
#: instead of a generic syntax error), exactly the ``cad.dsl`` convention.
#: ``ratio:`` (dimensionless) never takes a unit — ratios are outside the
#: unit ladder.
_NUM = r"-?\d+(?:\.\d+)?"
_LEN = rf"({_NUM})({LENGTH_UNIT_TOKEN})?"
_ANGLE = rf"({_NUM})({ANGLE_UNIT_TOKEN})?"
#: ``limits:`` serves both a length-valued joint (``prismatic``) and an
#: angle-valued one (every other kind) through the *same* regex — the
#: kind-aware unit check happens in :func:`_parse_limits`, not here.
_LIMIT_UNIT = rf"{LENGTH_UNIT_TOKEN}|{ANGLE_UNIT_TOKEN}"
_LIMIT = rf"({_NUM})({_LIMIT_UNIT})?"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LOC_RE = re.compile(rf"^@{_LEN},{_LEN},{_LEN}$")
_ROT_RE = re.compile(rf"^rot:{_ANGLE},{_ANGLE},{_ANGLE}$")
_POLAR_RE = re.compile(rf"^polar:n(\d+)r{_LEN}$")
_SPIN_RE = re.compile(rf"^spin:{_ANGLE}$")
_TYPE_RE = re.compile(r"^type:([A-Za-z_][A-Za-z0-9_-]*)$")
_OF_RE = re.compile(r"^of:([A-Za-z_][A-Za-z0-9_-]*)$")
_AT_RE = re.compile(r"^at:([A-Za-z_][A-Za-z0-9_-]*)$")
_LIMITS_RE = re.compile(rf"^limits:{_LIMIT}\.\.{_LIMIT}$")
_PITCH_RE = re.compile(rf"^pitch:{_LEN}$")
_RATIO_RE = re.compile(rf"^ratio:({_NUM})$")
_DIM_RE = re.compile(rf"^dim\s+([A-Za-z_][A-Za-z0-9_-]*)\s*(=|>=|<=)\s*{_LEN}$")
_CONSTRAIN_RE = re.compile(
    r"^constrain\s+([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*([A-Za-z_][A-Za-z0-9_-]*)$"
)

#: Joint kinds and their one state parameter (about/along the joint frame's
#: local z): revolute = radians, prismatic = metres, screw = radians (z
#: advance coupled via ``pitch`` metres/rev), cylindrical =
#: ``[radians, metres]`` (two DOF), fixed = no state (what a plain ``mate``
#: is).
JOINT_KINDS = ("fixed", "revolute", "prismatic", "cylindrical", "screw")

#: A joint's state: one number, or ``[angle_rad, slide_m]`` for cylindrical.
JointState = float | tuple[float, float]
_LINEAR_RE = re.compile(
    r"^linear:n(\d+)"
    rf"(?:dx{_LEN})?"
    rf"(?:dy{_LEN})?"
    rf"(?:dz{_LEN})?$"
)


class SceneError(ValueError):
    """A malformed design source line."""


class PolarPattern(TypedDict):
    """A ``polar:nNrR`` radial-array node config — ``n`` copies spread
    evenly around a circle of radius ``r`` centred on the node's ``@x,y``
    (``z`` from the node's own ``loc``)."""

    kind: Literal["polar"]
    n: float
    r: float


class LinearPattern(TypedDict):
    """A ``linear:nNdx..dy..dz..`` grid-array node config — ``n`` copies
    stepped by ``(dx, dy, dz)`` per instance from the node's ``loc``."""

    kind: Literal["linear"]
    n: float
    dx: float
    dy: float
    dz: float


#: A node's array-pattern config. The ``kind`` field discriminates the
#: union — narrowing on it (``pat["kind"] == "polar"``) gives mypy a real,
#: correctly-typed comparison (each member's ``kind`` is a ``Literal``, not
#: a same-typed-as-the-rest ``float``), unlike the previous flat
#: ``dict[str, float]`` shape where ``kind`` held a ``str`` in a
#: nominally-all-``float`` mapping.
NodePattern = PolarPattern | LinearPattern


def coerce_pattern(raw: Any) -> NodePattern | None:
    """Reconstruct a :class:`NodePattern` from a stored pattern payload
    (the inverse of :meth:`NodeSpec.to_meta`'s ``m["pattern"] = dict(...)``).

    Public so every reader of a persisted pattern shape — ``chunks.meta``
    (:meth:`NodeSpec.from_meta`) and the ``cad_nodes.pattern`` JSONB column
    (``precis.store._cad_ops.cad_load`` / ``cad_node``) alike — goes through
    one coercion instead of each re-deriving its own ``dict``-to-``NodePattern``
    cast.

    Deserialization, not validation of untrusted input — the shape was
    written by ``to_meta`` on a previous ``put``. An unrecognised /
    missing ``kind`` (a payload from a future pattern type, or hand-edited
    JSON) degrades to ``None`` (no pattern) rather than raising, so a
    stored node with a pattern this build doesn't understand still loads
    as a plain (unpatterned) node instead of failing the whole design.
    ⚠ The degrade is lossy on a load→edit→save round trip (``to_meta``
    omits a ``None`` pattern), so the drop is logged at WARNING — if a
    future pattern kind ever ships, old builds discard it noisily, not
    silently.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            log.warning("coerce_pattern: dropping non-dict pattern %r", raw)
        return None
    kind = raw.get("kind")
    if kind == "polar":
        return PolarPattern(
            kind="polar", n=float(raw.get("n", 0)), r=float(raw.get("r", 0))
        )
    if kind == "linear":
        return LinearPattern(
            kind="linear",
            n=float(raw.get("n", 0)),
            dx=float(raw.get("dx", 0.0)),
            dy=float(raw.get("dy", 0.0)),
            dz=float(raw.get("dz", 0.0)),
        )
    log.warning(
        "coerce_pattern: dropping pattern with unrecognised kind %r "
        "(a re-save will discard it)",
        kind,
    )
    return None


def instance_slug(config: str) -> str | None:
    """The design slug a ``use:`` node instances, or ``None`` for a shape.

    The one place the ``config`` encoding of an instance is decoded, so
    every consumer (parser, expander, exporter, tree render) agrees on what
    an instance node looks like without re-deriving the prefix test.
    """
    if config.startswith(INSTANCE_PREFIX):
        return config[len(INSTANCE_PREFIX) :] or None
    return None


def part_code(config: str) -> str | None:
    """The ``family:code`` a ``part:`` node places, or ``None`` for a shape.

    Sibling of :func:`instance_slug` — the one decode point for catalog-part
    nodes, so every consumer agrees on the encoding.
    """
    if config.startswith(PART_PREFIX):
        return config[len(PART_PREFIX) :] or None
    return None


def part_spec(code: str) -> SceneSpec:
    """A catalog part's envelope+ports as a parsed :class:`SceneSpec`.

    ``ValueError`` from an unknown code in a *stored* spec surfaces as a
    :class:`SceneError` (the parser refuses bad codes at ``put``, so this
    only fires on specs stored before a catalog entry was renamed)."""
    try:
        return parse_source(catalog.resolve_part(code).source)
    except ValueError as exc:
        raise SceneError(f"catalog part {code!r}: {exc}") from None


@dataclass(frozen=True)
class NodeSpec:
    """One parsed design node — the serialisable unit the handler stores.

    Two flavours share the row: a **shape** node (``config`` is the §11
    mini-DSL) and an **instance** node (``config`` is ``use:<slug>``, whose
    ``component`` is the instance's namespace). Instancing therefore needs
    no schema change — see :func:`expand_instances`.
    """

    name: str
    op: str  # add | cut | intersect
    config: str  # the mini-DSL shape string, or "use:<slug>"
    component: str
    loc: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pattern: NodePattern | None = None

    def to_meta(self) -> dict[str, Any]:
        """The ``chunks.meta`` payload for this node."""
        m: dict[str, Any] = {
            "op": self.op,
            "config": self.config,
            "component": self.component,
            "loc": list(self.loc),
            "rot": list(self.rot),
        }
        if self.pattern is not None:
            m["pattern"] = dict(self.pattern)
        return m

    @classmethod
    def from_meta(cls, name: str, meta: dict[str, Any]) -> NodeSpec:
        """Reconstruct from a stored ``chunks.meta`` payload."""
        loc = [float(x) for x in meta.get("loc", [0, 0, 0])]
        rot = [float(x) for x in meta.get("rot", [0, 0, 0])]
        return cls(
            name=name,
            op=str(meta.get("op", "add")),
            config=str(meta.get("config", "")),
            component=str(meta.get("component", "part")),
            loc=(loc[0], loc[1], loc[2]),
            rot=(rot[0], rot[1], rot[2]),
            pattern=coerce_pattern(meta.get("pattern")),
        )


@dataclass
class SceneSpec:
    """A whole design: ordered nodes grouped into named components."""

    nodes: list[NodeSpec] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=lambda: {"units": "mm"})


@dataclass(frozen=True)
class PayloadSpec:
    """Geometry a port *brings to whatever it mates against* — the
    straddling-module half of an interface (a hinge's knuckle recess, its
    pin bore), cad slice 5.

    Declared ``payload <name> <op> <config> at:<port> [@x,y,z] [rot:...]``;
    ``loc``/``rot`` are relative to the owning port's frame. When the port
    mates, :func:`_solve_mates` splices each payload into the component on
    the **other** side of the mate as a plain node named
    ``<instance>~<name>`` — the host's numbers change, but its node tree
    names who did it (attribution crosses boundaries loudly).
    """

    name: str
    op: str  # one of _PAYLOAD_OPS
    config: str  # the §11 mini-DSL shape string
    loc: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "op": self.op,
            "config": self.config,
            "loc": list(self.loc),
            "rot": list(self.rot),
        }

    @classmethod
    def from_meta(cls, raw: Any) -> PayloadSpec:
        if not isinstance(raw, dict) or not str(raw.get("name") or ""):
            raise SceneError(f"malformed stored payload {raw!r}")
        return cls(
            name=str(raw["name"]),
            op=str(raw.get("op") or "cut"),
            config=str(raw.get("config") or ""),
            loc=as_float3(raw.get("loc")),
            rot=as_float3(raw.get("rot")),
        )

    def to_source(self, port: str) -> str:
        config = (
            self.config
            if "{" in self.config
            else format_spec(parse(self.config), units=True)
        )
        parts = ["payload", self.name, self.op, config, f"at:{port}"]
        if self.loc != (0.0, 0.0, 0.0):
            parts.append(_fmt_loc(self.loc))
        if self.rot != (0.0, 0.0, 0.0):
            parts.append(_fmt_rot(self.rot))
        return " ".join(parts)


@dataclass(frozen=True)
class PortSpec:
    """A named frame on a design — the interface a :class:`MateSpec` targets.

    A port is not geometry, so it lives in ``SceneSpec.meta`` (which
    round-trips verbatim through ``refs.meta``) rather than as a node row:
    every consumer of ``spec.nodes`` would otherwise have to learn to skip
    it, and a missed skip is a wrong solid, not a visible error.
    """

    name: str
    loc: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: Free compatibility tag — two typed ports may only mate when equal.
    type: str = ""
    #: Component this frame rides on ("" = the design as a whole). Required
    #: on the pivot port of a component ``joint``.
    component: str = ""
    #: Geometry this port splices into whatever it mates to (slice 5).
    payloads: tuple[PayloadSpec, ...] = ()

    def to_meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "name": self.name,
            "loc": list(self.loc),
            "rot": list(self.rot),
        }
        if self.type:
            m["type"] = self.type
        if self.component:
            m["component"] = self.component
        if self.payloads:
            m["payloads"] = [p.to_meta() for p in self.payloads]
        return m

    @classmethod
    def from_meta(cls, raw: Any) -> PortSpec:
        if not isinstance(raw, dict) or not str(raw.get("name") or ""):
            raise SceneError(f"malformed stored port {raw!r}")
        return cls(
            name=str(raw["name"]),
            loc=as_float3(raw.get("loc")),
            rot=as_float3(raw.get("rot")),
            type=str(raw.get("type") or ""),
            component=str(raw.get("component") or ""),
            payloads=tuple(
                PayloadSpec.from_meta(p) for p in (raw.get("payloads") or ())
            ),
        )

    def to_source(self) -> str:
        """The ``port …`` source line (also what the node tree shows)."""
        parts = ["port", self.name]
        if self.loc != (0.0, 0.0, 0.0):
            parts.append(_fmt_loc(self.loc))
        if self.rot != (0.0, 0.0, 0.0):
            parts.append(_fmt_rot(self.rot))
        if self.type:
            parts.append(f"type:{self.type}")
        if self.component:
            parts.append(f"of:{self.component}")
        return " ".join(parts)

    def source_lines(self) -> list[str]:
        """The port line plus its ``payload …`` lines (parse round-trip)."""
        return [self.to_source(), *(p.to_source(self.name) for p in self.payloads)]

    def frame(self) -> Transform:
        """The port's pose in its own design's coordinates."""
        return _node_xform(self.loc, self.rot)


@dataclass(frozen=True)
class MateSpec:
    """``mate <instance>.<port> to <anchor> [flip] [spin:<deg>]`` — or its
    articulated generalisation ``joint … to … <kind> [limits:] [pitch:]``.

    A mate **is** a ``fixed`` joint (``kind`` defaults to it); the other
    kinds insert a state-dependent transform at the interface — see
    :func:`_joint_xform`. ``anchor_instance`` is ``None`` when the anchor is
    one of *this* design's own ports (fixed in the design frame); otherwise
    it names another instance whose pose must be solved first.
    """

    instance: str
    port: str
    anchor_instance: str | None
    anchor_port: str
    flip: bool = False
    spin: float = 0.0
    kind: str = "fixed"
    limits: tuple[float, float] | None = None
    pitch: float = 0.0

    @property
    def subject(self) -> str:
        return f"{self.instance}{NAMESPACE_SEP}{self.port}"

    @property
    def anchor(self) -> str:
        if self.anchor_instance is None:
            return self.anchor_port
        return f"{self.anchor_instance}{NAMESPACE_SEP}{self.anchor_port}"

    def to_source(self) -> str:
        """The ``mate …`` / ``joint …`` source line (also the node tree)."""
        if self.kind == "fixed":
            parts = ["mate", self.subject, "to", self.anchor]
        else:
            parts = ["joint", self.subject, "to", self.anchor, self.kind]
            if self.limits is not None:
                parts.append(_fmt_limits(self.limits, self.kind))
            if self.pitch:
                parts.append(_fmt_pitch(self.pitch))
        if self.flip:
            parts.append("flip")
        if self.spin:
            parts.append(f"spin:{_fmt_ang(self.spin)}")
        return " ".join(parts)

    def to_meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {"subject": self.subject, "anchor": self.anchor}
        if self.flip:
            m["flip"] = True
        if self.spin:
            m["spin"] = self.spin
        if self.kind != "fixed":
            m["kind"] = self.kind
        if self.limits is not None:
            m["limits"] = list(self.limits)
        if self.pitch:
            m["pitch"] = self.pitch
        return m

    @classmethod
    def from_meta(cls, raw: Any) -> MateSpec:
        if not isinstance(raw, dict):
            raise SceneError(f"malformed stored mate {raw!r}")
        return cls.build(
            subject=str(raw.get("subject") or ""),
            anchor=str(raw.get("anchor") or ""),
            flip=bool(raw.get("flip")),
            spin=float(raw.get("spin") or 0.0),
            kind=str(raw.get("kind") or "fixed"),
            limits=_coerce_limits(raw.get("limits"), where="stored mate"),
            pitch=float(raw.get("pitch") or 0.0),
            where="stored mate",
        )

    @classmethod
    def build(
        cls,
        *,
        subject: str,
        anchor: str,
        flip: bool,
        spin: float,
        where: str,
        kind: str = "fixed",
        limits: tuple[float, float] | None = None,
        pitch: float = 0.0,
    ) -> MateSpec:
        """Parse the two dotted addresses. ``where`` prefixes any error."""
        parts = subject.split(NAMESPACE_SEP)
        if len(parts) != 2 or not all(_IDENT_RE.match(x) for x in parts):
            raise SceneError(
                f"{where}: mate subject must be '<instance>{NAMESPACE_SEP}<port>', "
                f"got {subject!r}"
            )
        a_parts = anchor.split(NAMESPACE_SEP)
        if len(a_parts) > 2 or not all(_IDENT_RE.match(x) for x in a_parts):
            raise SceneError(
                f"{where}: mate anchor must be '<port>' or "
                f"'<instance>{NAMESPACE_SEP}<port>', got {anchor!r}"
            )
        _check_joint_options(kind, limits, pitch, where=where)
        return cls(
            instance=parts[0],
            port=parts[1],
            anchor_instance=a_parts[0] if len(a_parts) == 2 else None,
            anchor_port=a_parts[-1],
            flip=flip,
            spin=spin,
            kind=kind,
            limits=limits,
            pitch=pitch,
        )


def _parse_limits(m: re.Match[str], kind: str, where: str) -> tuple[float, float]:
    """A matched ``_LIMITS_RE`` → ``(lo, hi)`` SI (metres or radians),
    joint-kind-aware: ``prismatic`` limits are a length (its state is a
    translation) and require an explicit length unit; every other kind's
    limits are the joint's angle (revolute/screw directly, cylindrical's
    angle component) and require an explicit angle unit (``deg``/``rad``)."""
    lo_num, lo_unit, hi_num, hi_unit = m[1], m[2], m[3], m[4]
    if kind == "prismatic":
        return (
            _length(lo_num, lo_unit, f"{where}: limits lo"),
            _length(hi_num, hi_unit, f"{where}: limits hi"),
        )
    return (
        _angle(lo_num, lo_unit, f"{where}: limits lo"),
        _angle(hi_num, hi_unit, f"{where}: limits hi"),
    )


def _parse_pitch(m: re.Match[str], where: str) -> float:
    """A matched ``_PITCH_RE`` → SI metres. ``pitch:`` is always a length
    (a screw's mm-per-revolution lead) — unlike ``limits:``, its unit
    requirement never depends on the joint kind (only ``screw`` ever
    carries one)."""
    return _length(m[1], m[2], f"{where}: pitch")


def _fmt_limits(limits: tuple[float, float], kind: str) -> str:
    """The ``limits:lo..hi`` source token, kind-aware inverse of
    :func:`_parse_limits`: each kind's own internal SI unit, ``m`` for
    ``prismatic``, ``rad`` for every angle-valued kind."""
    lo, hi = limits
    unit = "m" if kind == "prismatic" else "rad"
    return f"limits:{_fmt_num(lo)}{unit}..{_fmt_num(hi)}{unit}"


def _fmt_pitch(pitch: float) -> str:
    """The ``pitch:<length>`` source token, inverse of :func:`_parse_pitch`."""
    return f"pitch:{_fmt_num(pitch)}m"


def _coerce_limits(raw: Any, *, where: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise SceneError(f"{where}: malformed limits {raw!r}")
    return (float(raw[0]), float(raw[1]))


def _check_joint_options(
    kind: str, limits: tuple[float, float] | None, pitch: float, *, where: str
) -> None:
    """The shared kind/limits/pitch consistency rules for both joint forms."""
    if kind not in JOINT_KINDS:
        raise SceneError(f"{where}: joint kind {kind!r} not one of {list(JOINT_KINDS)}")
    if kind == "fixed" and (limits is not None or pitch):
        raise SceneError(f"{where}: 'fixed' has no state — limits:/pitch: don't apply")
    if kind == "screw" and pitch <= 0.0:
        raise SceneError(f"{where}: 'screw' requires pitch:<mm-per-rev>")
    if kind != "screw" and pitch:
        raise SceneError(f"{where}: pitch: only applies to 'screw'")
    if limits is not None and limits[0] >= limits[1]:
        raise SceneError(
            f"{where}: limits lo must be < hi, got {limits[0]:g}..{limits[1]:g}"
        )


@dataclass(frozen=True)
class ComponentJointSpec:
    """``joint <component> <kind> at:<port> [limits:lo..hi] [pitch:<mm>]``.

    Articulates a whole component of *this* design about/along the z axis of
    ``port``'s frame — the port must be scoped ``of:`` the jointed component
    (it names which body the frame rides on). Never ``fixed``: a fixed
    component joint is a no-op (components are already rigid in the design
    frame).
    """

    component: str
    kind: str
    port: str
    limits: tuple[float, float] | None = None
    pitch: float = 0.0

    def to_source(self) -> str:
        parts = ["joint", self.component, self.kind, f"at:{self.port}"]
        if self.limits is not None:
            parts.append(_fmt_limits(self.limits, self.kind))
        if self.pitch:
            parts.append(_fmt_pitch(self.pitch))
        return " ".join(parts)

    def to_meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {
            "component": self.component,
            "kind": self.kind,
            "port": self.port,
        }
        if self.limits is not None:
            m["limits"] = list(self.limits)
        if self.pitch:
            m["pitch"] = self.pitch
        return m

    @classmethod
    def from_meta(cls, raw: Any) -> ComponentJointSpec:
        if not isinstance(raw, dict) or not str(raw.get("component") or ""):
            raise SceneError(f"malformed stored joint {raw!r}")
        kind = str(raw.get("kind") or "")
        limits = _coerce_limits(raw.get("limits"), where="stored joint")
        pitch = float(raw.get("pitch") or 0.0)
        _check_joint_options(kind, limits, pitch, where="stored joint")
        return cls(
            component=str(raw["component"]),
            kind=kind,
            port=str(raw.get("port") or ""),
            limits=limits,
            pitch=pitch,
        )


@dataclass(frozen=True)
class CoupleSpec:
    """``gear <drive> to <driven> ratio:<r>`` / ``belt …`` — the driven
    joint's state is ``ratio × drive``'s. The sign carries the sense (contact
    gears reverse: write a negative ratio); ``via`` records the author's
    intent, the math is identical."""

    via: str  # "gear" | "belt"
    drive: str
    driven: str
    ratio: float

    def to_source(self) -> str:
        return f"{self.via} {self.drive} to {self.driven} ratio:{_fmt_num(self.ratio)}"

    def to_meta(self) -> dict[str, Any]:
        return {
            "via": self.via,
            "drive": self.drive,
            "driven": self.driven,
            "ratio": self.ratio,
        }

    @classmethod
    def from_meta(cls, raw: Any) -> CoupleSpec:
        if not isinstance(raw, dict) or not str(raw.get("drive") or ""):
            raise SceneError(f"malformed stored couple {raw!r}")
        return cls(
            via=str(raw.get("via") or "gear"),
            drive=str(raw["drive"]),
            driven=str(raw.get("driven") or ""),
            ratio=float(raw.get("ratio") or 0.0),
        )


def ports_of(spec: SceneSpec) -> list[PortSpec]:
    """This design's declared ports (empty when it declares none)."""
    return [PortSpec.from_meta(r) for r in spec.meta.get("ports") or ()]


def mates_of(spec: SceneSpec) -> list[MateSpec]:
    """This design's declared mates *and* instance joints (a mate is a
    ``fixed`` joint, so both live in ``meta['mates']``)."""
    return [MateSpec.from_meta(r) for r in spec.meta.get("mates") or ()]


def joints_of(spec: SceneSpec) -> list[ComponentJointSpec]:
    """This design's component joints (empty when it declares none)."""
    return [ComponentJointSpec.from_meta(r) for r in spec.meta.get("joints") or ()]


def couples_of(spec: SceneSpec) -> list[CoupleSpec]:
    """This design's gear/belt couplings (empty when it declares none)."""
    return [CoupleSpec.from_meta(r) for r in spec.meta.get("couples") or ()]


def _length(num: str, unit: str | None, arg_name: str) -> float:
    """One boundary-mode length token → SI metres. ``unit`` is the regex's
    optional unit-suffix group — ``None`` raises the structured
    :class:`~precis.utils.units.UnitRequiredError` (the zero-counting /
    exponent-slip guard), never a generic parse error."""
    if not unit:
        raise UnitRequiredError(float(num), "length", arg_name=arg_name)
    return parse_quantity(f"{num}{unit}", "length", arg_name=arg_name)


def _angle(num: str, unit: str | None, arg_name: str) -> float:
    """One boundary-mode angle token (``rot:``/``spin:``/angle-kind
    ``limits:``) → SI radians. Same contract as :func:`_length`, angle-
    flavoured (the decisions log's angle ruling)."""
    if not unit:
        raise UnitRequiredError(float(num), "angle", arg_name=arg_name)
    return parse_quantity(f"{num}{unit}", "angle", arg_name=arg_name)


def _parse_placement(
    toks: list[str], lineno: int
) -> tuple[tuple[float, float, float], tuple[float, float, float], NodePattern | None]:
    """Parse the trailing ``@x,y,z`` / ``rot:`` / ``polar:`` / ``linear:``
    tokens shared by shape nodes and ``use`` instance directives.

    Every length (``@x,y,z`` per component, ``polar:``'s ``r``,
    ``linear:``'s ``dx``/``dy``/``dz``) requires an explicit length unit;
    ``rot:`` requires an explicit angle unit (``deg``/``rad``)."""
    loc = (0.0, 0.0, 0.0)
    rot = (0.0, 0.0, 0.0)
    pattern: NodePattern | None = None
    for tok in toks:
        if m := _LOC_RE.match(tok):
            loc = (
                _length(m[1], m[2], f"line {lineno}: @x"),
                _length(m[3], m[4], f"line {lineno}: @y"),
                _length(m[5], m[6], f"line {lineno}: @z"),
            )
        elif m := _ROT_RE.match(tok):
            rot = (
                _angle(m[1], m[2], f"line {lineno}: rot x"),
                _angle(m[3], m[4], f"line {lineno}: rot y"),
                _angle(m[5], m[6], f"line {lineno}: rot z"),
            )
        elif m := _POLAR_RE.match(tok):
            pattern = PolarPattern(
                kind="polar",
                n=float(m[1]),
                r=_length(m[2], m[3], f"line {lineno}: polar:r"),
            )
        elif m := _LINEAR_RE.match(tok):
            pattern = LinearPattern(
                kind="linear",
                n=float(m[1]),
                dx=_length(m[2], m[3], f"line {lineno}: linear:dx") if m[2] else 0.0,
                dy=_length(m[4], m[5], f"line {lineno}: linear:dy") if m[4] else 0.0,
                dz=_length(m[6], m[7], f"line {lineno}: linear:dz") if m[6] else 0.0,
            )
        else:
            raise SceneError(f"line {lineno}: unrecognised token {tok!r}")
    return loc, rot, pattern


def parse_source(text: str) -> SceneSpec:
    """Parse the line-based design language into a :class:`SceneSpec`."""
    spec = SceneSpec()
    current = "part"
    seen_components: list[str] = []
    seen_names: set[str] = set()
    components_with_nodes: set[str] = set()
    instance_names: set[str] = set()
    ports: list[PortSpec] = []
    payloads: list[tuple[str, PayloadSpec, int]] = []  # (at-port, spec, lineno)
    materials: dict[str, str] = {}  # component -> material slug
    dims: dict[str, list[float | None]] = {}  # name -> [lo, hi] (None=open)
    constraints: list[tuple[str, str]] = []  # equality pairs
    pending_dim_configs: list[tuple[int, str, str]] = []  # (lineno, name, config)
    mates: list[MateSpec] = []
    cjoints: list[ComponentJointSpec] = []
    couples: list[CoupleSpec] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("desc:") or low.startswith("use:"):
            # free-text design intent — folded into the one search card so
            # designs are findable by purpose, not just geometry (the CAD analytic IR
            # Amendment 1). `desc:` = what it is; `use:` = what it's for.
            key = "description" if low.startswith("desc:") else "use"
            val = line.split(":", 1)[1].strip()
            if val:
                prev = spec.meta.get(key)
                spec.meta[key] = f"{prev} {val}".strip() if prev else val
            continue
        toks = line.split()
        if toks[0] == "component":
            if len(toks) != 2:
                raise SceneError(f"line {lineno}: 'component' needs exactly a name")
            current = toks[1]
            if current in instance_names:
                raise SceneError(
                    f"line {lineno}: component {current!r} collides with the "
                    "instance of the same name"
                )
            if current not in seen_components:
                seen_components.append(current)
            continue
        if toks[0] == "use":
            # `use <slug> as <name> [pose] [pattern]` — a sub-assembly. It is
            # a peer of `component`, so it neither joins nor closes the
            # enclosing component block (`current` is deliberately untouched).
            if len(toks) < 4 or toks[2] != "as":
                raise SceneError(
                    f"line {lineno}: expected "
                    "'use <design> as <name> [@x,y,z] [rot:...] [pattern]'"
                )
            sub_slug, inst = toks[1], toks[3]
            if not _SLUG_RE.match(sub_slug):
                raise SceneError(f"line {lineno}: bad design slug {sub_slug!r}")
            if not _IDENT_RE.match(inst):
                raise SceneError(
                    f"line {lineno}: bad instance name {inst!r} — letters, "
                    f"digits, '_' and '-' only (no {NAMESPACE_SEP!r}, which "
                    "separates an instance from the parts it brings in)"
                )
            if inst in seen_names or inst in seen_components:
                raise SceneError(f"line {lineno}: duplicate name {inst!r}")
            loc, rot, pattern = _parse_placement(toks[4:], lineno)
            seen_names.add(inst)
            seen_components.append(inst)
            components_with_nodes.add(inst)
            instance_names.add(inst)
            spec.nodes.append(
                NodeSpec(
                    name=inst,
                    op="add",
                    config=f"{INSTANCE_PREFIX}{sub_slug}",
                    component=inst,
                    loc=loc,
                    rot=rot,
                    pattern=pattern,
                )
            )
            continue
        if toks[0] == "part":
            # `part <name> <family>:<code> [pose] [pattern]` — a catalog
            # atom (precis.cad.catalog). Top-level like `use` (owns its own
            # component namespace, leaves `current` alone); the code is
            # validated *here* so a typo refuses at put with a line number,
            # never at expansion.
            if len(toks) < 3 or ":" not in toks[2]:
                raise SceneError(
                    f"line {lineno}: expected 'part <name> <family>:<code> "
                    "[@x,y,z] [rot:...] [pattern]' (e.g. 'part b1 bearing:6202')"
                )
            part_name, raw_code = toks[1], toks[2].lower()
            if not _IDENT_RE.match(part_name):
                raise SceneError(
                    f"line {lineno}: bad part name {part_name!r} — letters, "
                    f"digits, '_' and '-' only (no {NAMESPACE_SEP!r}, which "
                    "separates a part from the bodies it brings in)"
                )
            if part_name in seen_names or part_name in seen_components:
                raise SceneError(f"line {lineno}: duplicate name {part_name!r}")
            try:
                catalog.resolve_part(raw_code)
            except ValueError as exc:
                raise SceneError(f"line {lineno}: {exc}") from None
            loc, rot, pattern = _parse_placement(toks[3:], lineno)
            seen_names.add(part_name)
            seen_components.append(part_name)
            components_with_nodes.add(part_name)
            instance_names.add(part_name)
            spec.nodes.append(
                NodeSpec(
                    name=part_name,
                    op="add",
                    config=f"{PART_PREFIX}{raw_code}",
                    component=part_name,
                    loc=loc,
                    rot=rot,
                    pattern=pattern,
                )
            )
            continue
        if toks[0] == "port":
            # `port <name> [@x,y,z] [rot:...] [type:<t>] [of:<component>]` —
            # a named frame on this design. Top-level like `component`/`use`:
            # `current` is untouched (scope is the explicit `of:`, never the
            # enclosing block, so slice-2 sources keep their meaning).
            if len(toks) < 2:
                raise SceneError(
                    f"line {lineno}: expected 'port <name> [@x,y,z] [rot:...] "
                    "[type:<t>] [of:<component>]'"
                )
            pname = toks[1]
            if not _IDENT_RE.match(pname):
                raise SceneError(f"line {lineno}: bad port name {pname!r}")
            if any(pt.name == pname for pt in ports):
                raise SceneError(f"line {lineno}: duplicate port {pname!r}")
            ptype = pcomp = ""
            rest: list[str] = []
            for tok in toks[2:]:
                if m := _TYPE_RE.match(tok):
                    ptype = m[1]
                elif m := _OF_RE.match(tok):
                    pcomp = m[1]
                else:
                    rest.append(tok)
            ploc, prot, ppat = _parse_placement(rest, lineno)
            if ppat is not None:
                raise SceneError(
                    f"line {lineno}: port {pname!r} cannot carry a pattern — "
                    "a port is a single frame; declare one port per interface"
                )
            ports.append(
                PortSpec(name=pname, loc=ploc, rot=prot, type=ptype, component=pcomp)
            )
            continue
        if toks[0] == "payload":
            # `payload <name> <op> <config> at:<port> [@x,y,z] [rot:...]` —
            # geometry the port splices into whatever it mates to (slice 5).
            # Top-level like `port`; placement is relative to the port frame.
            if len(toks) < 5:
                raise SceneError(
                    f"line {lineno}: expected 'payload <name> <op> <config> "
                    "at:<port> [@x,y,z] [rot:...]'"
                )
            plname, plop, plconfig = toks[1], toks[2], toks[3]
            if not _IDENT_RE.match(plname):
                raise SceneError(f"line {lineno}: bad payload name {plname!r}")
            if plname in seen_names:
                raise SceneError(f"line {lineno}: duplicate node name {plname!r}")
            seen_names.add(plname)
            if plop not in _PAYLOAD_OPS:
                raise SceneError(
                    f"line {lineno}: payload op {plop!r} not one of "
                    f"{_PAYLOAD_OPS} — an 'intersect' payload would replace "
                    "its whole host with the overlap, not feature it"
                )
            if "{" in plconfig:
                raise SceneError(
                    f"line {lineno}: payload {plname!r} config cannot "
                    "reference dims yet — a payload splices into another "
                    "design, whose dim namespace is not this one; use "
                    "unit-suffixed literal numbers, e.g. 'cyl:r3mmh12mm'"
                )
            pl_spec = parse(plconfig, require_units=True)
            build(pl_spec)
            plconfig = format_spec(pl_spec)  # canonicalise for storage
            if pl_spec.alias == "chamfer" and plop == "add":
                raise SceneError(
                    f"line {lineno}: chamfer payload {plname!r} cannot use op "
                    "'add' — an added half-space is an unbounded infinite solid"
                )
            at_port = ""
            rest = []
            for tok in toks[4:]:
                if m := _AT_RE.match(tok):
                    at_port = m[1]
                else:
                    rest.append(tok)
            if not at_port:
                raise SceneError(
                    f"line {lineno}: payload {plname!r} needs at:<port> — the "
                    "port whose mate splices it into the other body"
                )
            plloc, plrot, plpat = _parse_placement(rest, lineno)
            if plpat is not None:
                raise SceneError(
                    f"line {lineno}: payload {plname!r} cannot carry a pattern "
                    "— a payload splices once per mate; declare one per feature"
                )
            payloads.append(
                (
                    at_port,
                    PayloadSpec(
                        name=plname, op=plop, config=plconfig, loc=plloc, rot=plrot
                    ),
                    lineno,
                )
            )
            continue
        if toks[0] == "dim":
            # `dim <name> =|>=|<= <mm>` — a named dimension bound. Bounds on
            # the same name accumulate by intersection; an empty interval is
            # refused immediately (an impossible declaration must not parse).
            m = _DIM_RE.match(line)
            if m is None:
                raise SceneError(
                    f"line {lineno}: expected 'dim <name> =|>=|<= <value+unit>'"
                )
            dname, dop = m[1], m[2]
            dval = _length(m[3], m[4], f"line {lineno}: dim {dname}")
            lo, hi = dims.get(dname, [None, None])
            if dop in ("=", ">="):
                lo = dval if lo is None else max(lo, dval)
            if dop in ("=", "<="):
                hi = dval if hi is None else min(hi, dval)
            if lo is not None and hi is not None and lo > hi:
                raise SceneError(
                    f"line {lineno}: dim {dname!r} is impossible — bounds "
                    f"reduce to an empty range ({_fmt_interval([lo, hi])})"
                )
            dims[dname] = [lo, hi]
            continue
        if toks[0] == "constrain":
            # `constrain <a> = <b>` — two dims must be equal. Satisfiability
            # is checked at end of parse (declarations are order-free).
            m = _CONSTRAIN_RE.match(line)
            if m is None:
                raise SceneError(
                    f"line {lineno}: expected 'constrain <dim> = <dim>' "
                    "(v1 supports equality between dims; numeric bounds go "
                    "on 'dim' lines)"
                )
            if m[1] == m[2]:
                raise SceneError(
                    f"line {lineno}: constrain {m[1]} = {m[1]} is a tautology"
                )
            constraints.append((m[1], m[2]))
            continue
        if toks[0] == "material":
            # `material <component> <slug>` — assign a material kind slug to
            # a component (view='mass' joins its sourced density). Top-level
            # and order-free like `port`; validated against components at
            # the end of the parse.
            if len(toks) != 3:
                raise SceneError(
                    f"line {lineno}: expected 'material <component> <slug>'"
                )
            mcomp, mslug = toks[1], toks[2]
            if not _IDENT_RE.match(mcomp):
                raise SceneError(f"line {lineno}: bad component name {mcomp!r}")
            if not _SLUG_RE.match(mslug):
                raise SceneError(f"line {lineno}: bad material slug {mslug!r}")
            if mcomp in materials:
                raise SceneError(
                    f"line {lineno}: component {mcomp!r} already has material "
                    f"{materials[mcomp]!r}"
                )
            materials[mcomp] = mslug
            continue
        if toks[0] == "mate" or (
            toks[0] == "joint" and len(toks) >= 3 and toks[2] == "to"
        ):
            # `mate <inst>.<port> to <anchor> [flip] [spin:<deg>]`, or the
            # articulated `joint <inst>.<port> to <anchor> <kind> [opts]`.
            is_joint = toks[0] == "joint"
            if len(toks) < (5 if is_joint else 4) or toks[2] != "to":
                raise SceneError(
                    f"line {lineno}: expected '{toks[0]} "
                    f"<instance>{NAMESPACE_SEP}<port> to <anchor>"
                    + (" <kind>" if is_joint else "")
                    + " [options]'"
                )
            kind = toks[4] if is_joint else "fixed"
            flip = False
            spin = 0.0
            limits: tuple[float, float] | None = None
            pitch = 0.0
            for tok in toks[5 if is_joint else 4 :]:
                if tok == "flip":
                    flip = True
                elif m := _SPIN_RE.match(tok):
                    spin = _angle(m[1], m[2], f"line {lineno}: spin")
                elif is_joint and (m := _LIMITS_RE.match(tok)):
                    limits = _parse_limits(m, kind, f"line {lineno}")
                elif is_joint and (m := _PITCH_RE.match(tok)):
                    pitch = _parse_pitch(m, f"line {lineno}")
                else:
                    raise SceneError(f"line {lineno}: unrecognised token {tok!r}")
            mates.append(
                MateSpec.build(
                    subject=toks[1],
                    anchor=toks[3],
                    flip=flip,
                    spin=spin,
                    kind=kind,
                    limits=limits,
                    pitch=pitch,
                    where=f"line {lineno}",
                )
            )
            continue
        if toks[0] == "joint":
            # `joint <component> <kind> at:<port> [limits:lo..hi] [pitch:<mm>]`
            # — articulate a whole component of this design.
            if len(toks) < 4:
                raise SceneError(
                    f"line {lineno}: expected 'joint <component> <kind> "
                    "at:<port> [limits:lo..hi] [pitch:<mm>]' (or the instance "
                    f"form 'joint <inst>{NAMESPACE_SEP}<port> to <anchor> <kind>')"
                )
            jcomp, jkind = toks[1], toks[2]
            if not _IDENT_RE.match(jcomp):
                raise SceneError(f"line {lineno}: bad component name {jcomp!r}")
            jport = ""
            limits = None
            pitch = 0.0
            for tok in toks[3:]:
                if m := _AT_RE.match(tok):
                    jport = m[1]
                elif m := _LIMITS_RE.match(tok):
                    limits = _parse_limits(m, jkind, f"line {lineno}")
                elif m := _PITCH_RE.match(tok):
                    pitch = _parse_pitch(m, f"line {lineno}")
                else:
                    raise SceneError(f"line {lineno}: unrecognised token {tok!r}")
            if not jport:
                raise SceneError(
                    f"line {lineno}: component joint needs at:<port> — the "
                    "pivot frame, a port declared of: the jointed component"
                )
            _check_joint_options(jkind, limits, pitch, where=f"line {lineno}")
            if jkind == "fixed":
                raise SceneError(
                    f"line {lineno}: a fixed component joint is a no-op — "
                    "components are already rigid in the design frame"
                )
            if any(j.component == jcomp for j in cjoints):
                raise SceneError(f"line {lineno}: component {jcomp!r} is jointed twice")
            cjoints.append(
                ComponentJointSpec(
                    component=jcomp,
                    kind=jkind,
                    port=jport,
                    limits=limits,
                    pitch=pitch,
                )
            )
            continue
        if toks[0] in ("gear", "belt"):
            # `gear <drive> to <driven> ratio:<r>` — couple two joint states.
            m = _RATIO_RE.match(toks[4]) if len(toks) == 5 else None
            if len(toks) != 5 or toks[2] != "to" or m is None:
                raise SceneError(
                    f"line {lineno}: expected "
                    f"'{toks[0]} <drive-joint> to <driven-joint> ratio:<r>'"
                )
            ratio = float(m[1])
            if ratio == 0.0:
                raise SceneError(f"line {lineno}: ratio must be non-zero")
            for nm in (toks[1], toks[3]):
                if not _IDENT_RE.match(nm):
                    raise SceneError(f"line {lineno}: bad joint name {nm!r}")
            if toks[1] == toks[3]:
                raise SceneError(f"line {lineno}: a joint cannot be coupled to itself")
            couples.append(
                CoupleSpec(via=toks[0], drive=toks[1], driven=toks[3], ratio=ratio)
            )
            continue
        if len(toks) < 3:
            raise SceneError(
                f"line {lineno}: expected '<name> <op> <config> [@x,y,z] [...]'"
            )
        name, op, config = toks[0], toks[1], toks[2]
        if op not in _OPS:
            raise SceneError(f"line {lineno}: op {op!r} not one of {_OPS}")
        if name in seen_names:
            raise SceneError(f"line {lineno}: duplicate node name {name!r}")
        seen_names.add(name)
        if "{" in config:
            # A parametrized config (`box:w{a}d{b}h10`) — dims are
            # order-free declarations, so shape validation defers to the
            # post-pass once every `dim` line is in (the alias is still
            # checkable now: it's the literal prefix).
            pending_dim_configs.append((lineno, name, config))
            alias = config.split(":", 1)[0].strip()
        else:
            # boundary-mode parse (raises on bad DSL / missing units), then
            # re-canonicalise to bare SI metres for storage — the stored
            # `config` is always canonical/storage-mode text, never the
            # author's unit-suffixed source (which the canonical/storage
            # parse mode used on every reload could not re-parse).
            node_spec = parse(config, require_units=True)
            build(node_spec)
            alias = node_spec.alias
            config = format_spec(node_spec)
        if alias == "chamfer":
            if op == "add":
                raise SceneError(
                    f"line {lineno}: chamfer node {name!r} cannot use op 'add' "
                    "— an added half-space is an unbounded infinite solid; "
                    "use 'cut' or 'intersect'"
                )
            if current not in components_with_nodes:
                raise SceneError(
                    f"line {lineno}: chamfer node {name!r} cannot be the first "
                    f"node of component {current!r} — the base must be a "
                    "finite solid; add a bounded base node before chamfering it"
                )

        loc, rot, pattern = _parse_placement(toks[3:], lineno)

        if current not in seen_components:
            seen_components.append(current)
        components_with_nodes.add(current)
        spec.nodes.append(
            NodeSpec(
                name=name,
                op=op,
                config=config,
                component=current,
                loc=loc,
                rot=rot,
                pattern=pattern,
            )
        )

    # Attach payloads to their ports (order-free: a payload line may precede
    # its port's declaration).
    if payloads:
        idx = {pt.name: i for i, pt in enumerate(ports)}
        for at_port, pl, pl_lineno in payloads:
            i = idx.get(at_port)
            if i is None:
                known = ", ".join(pt.name for pt in ports) or "none"
                raise SceneError(
                    f"line {pl_lineno}: payload {pl.name!r}: at:{at_port} is "
                    f"not a declared port — declared ports: {known}"
                )
            ports[i] = replace(ports[i], payloads=(*ports[i].payloads, pl))

    eff_dims = _effective_dims(dims, constraints)
    for cfg_lineno, cfg_name, cfg in pending_dim_configs:
        # dims are all in now — resolve the parametrized configs and
        # validate the resulting shape, so a bad ref or a bad shape still
        # refuses at put with the offending line named.
        resolved = _subst_dims(cfg, eff_dims, f"line {cfg_lineno}: node {cfg_name!r}")
        build(parse(resolved))

    for mcomp in materials:
        if mcomp not in seen_components or mcomp in instance_names:
            raise SceneError(
                f"material for {mcomp!r}: must name a component of this "
                "design (an instance's materials belong to its own design)"
            )

    _validate_interfaces(
        ports=ports,
        mates=mates,
        cjoints=cjoints,
        couples=couples,
        seen_components=seen_components,
        instance_names=instance_names,
    )
    if dims:
        spec.meta["dims"] = {k: list(v) for k, v in dims.items()}
    if constraints:
        spec.meta["constraints"] = [list(c) for c in sorted(set(constraints))]
    if materials:
        spec.meta["materials"] = dict(materials)
    if ports:
        spec.meta["ports"] = [pt.to_meta() for pt in ports]
    if mates:
        spec.meta["mates"] = [mt.to_meta() for mt in mates]
    if cjoints:
        spec.meta["joints"] = [j.to_meta() for j in cjoints]
    if couples:
        spec.meta["couples"] = [c.to_meta() for c in couples]
    spec.components = seen_components or ["part"]
    return spec


def _validate_interfaces(
    *,
    ports: list[PortSpec],
    mates: list[MateSpec],
    cjoints: list[ComponentJointSpec],
    couples: list[CoupleSpec],
    seen_components: list[str],
    instance_names: set[str],
) -> None:
    """Cross-line consistency for ports / joints / couples, run once at the
    end of the parse (declarations are order-free, so per-line checks can't
    see forward references)."""
    by_port = {pt.name: pt for pt in ports}
    for pt in ports:
        if pt.component and (
            pt.component not in seen_components or pt.component in instance_names
        ):
            raise SceneError(
                f"port {pt.name!r}: of:{pt.component} must name a component "
                "of this design (not an instance — an instance's ports are "
                "declared in its own design)"
            )
    for j in cjoints:
        if j.component in instance_names:
            raise SceneError(
                f"joint {j.component!r}: that's an instance — articulate it "
                f"with 'joint {j.component}{NAMESPACE_SEP}<port> to <anchor> "
                f"{j.kind}'"
            )
        if j.component not in seen_components:
            raise SceneError(f"joint {j.component!r}: no such component in this design")
        pivot = by_port.get(j.port)
        if pivot is None:
            known = ", ".join(sorted(by_port)) or "none"
            raise SceneError(
                f"joint {j.component!r}: at:{j.port} is not a declared port "
                f"— declared ports: {known}"
            )
        if pivot.component != j.component:
            raise SceneError(
                f"joint {j.component!r}: pivot port {j.port!r} must be "
                f"scoped 'of:{j.component}' — the port names which body the "
                f"joint frame rides on (it is "
                + (f"of:{pivot.component}" if pivot.component else "unscoped")
                + ")"
            )
    # Couples reference articulated joints by name (subject instance /
    # component). Fixed mates have no state to couple.
    joint_names = {m.instance for m in mates if m.kind != "fixed"} | {
        j.component for j in cjoints
    }
    two_dof = {m.instance for m in mates if m.kind == "cylindrical"} | {
        j.component for j in cjoints if j.kind == "cylindrical"
    }
    driven_by: dict[str, str] = {}
    for c in couples:
        for nm in (c.drive, c.driven):
            if nm not in joint_names:
                known = ", ".join(sorted(joint_names)) or "none"
                raise SceneError(
                    f"{c.via} {c.drive} to {c.driven}: {nm!r} is not an "
                    f"articulated joint — joints: {known}"
                )
            if nm in two_dof:
                raise SceneError(
                    f"{c.via} {c.drive} to {c.driven}: {nm!r} is cylindrical "
                    "(two DOF) — couple only single-DOF joints"
                )
        if c.driven in driven_by:
            raise SceneError(
                f"joint {c.driven!r} is driven by two couplings — over-constrained"
            )
        driven_by[c.driven] = c.drive
    for start in driven_by:
        seen: set[str] = set()
        cur: str | None = start
        while cur is not None:
            if cur in seen:
                raise SceneError(
                    f"coupling cycle through joint {start!r} — a driven "
                    "chain must end at a free joint"
                )
            seen.add(cur)
            cur = driven_by.get(cur)


def _fmt_interval(iv: list[float | None]) -> str:
    """Render a dim's bound(s) with an explicit unit (:func:`_fmt_len` — every
    dim is a length) so an author who wrote ``dim a = 200mm`` doesn't get a
    bare ``0.2`` echoed back that reads as their own mm figure."""
    lo, hi = iv
    if lo is not None and hi is not None and lo == hi:
        return f"= {_fmt_len(lo)}"
    parts = []
    if lo is not None:
        parts.append(f">= {_fmt_len(lo)}")
    if hi is not None:
        parts.append(f"<= {_fmt_len(hi)}")
    return " and ".join(parts) if parts else "unbounded"


def _effective_dims(
    dims: dict[str, list[float | None]], constraints: list[tuple[str, str]]
) -> dict[str, list[float | None]]:
    """Refuse the impossible before geometry exists — and hand back what a
    ``{name}`` config reference resolves against.

    Union-find over the equality constraints, one interval per class
    (intersection of every member's bounds). An empty class interval means
    the declarations contradict each other — e.g. ``a = 200``, ``b = 150``,
    ``constrain a = b`` — and the design must not parse. Returns each dim's
    **effective** interval — its class's intersection, so ``dim a = 200``
    + ``constrain a = b`` pins ``b`` too."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for a, b in constraints:
        for nm in (a, b):
            if nm not in dims:
                known = ", ".join(sorted(dims)) or "none"
                raise SceneError(
                    f"constrain {a} = {b}: {nm!r} is not a declared dim — "
                    f"declared: {known}"
                )
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    classes: dict[str, list[str]] = {}
    for nm in dims:
        classes.setdefault(find(nm), []).append(nm)
    effective: dict[str, list[float | None]] = {}
    for members in classes.values():
        lo: float | None = None
        hi: float | None = None
        for nm in members:
            dlo, dhi = dims[nm]
            if dlo is not None:
                lo = dlo if lo is None else max(lo, dlo)
            if dhi is not None:
                hi = dhi if hi is None else min(hi, dhi)
        if len(members) > 1 and lo is not None and hi is not None and lo > hi:
            detail = "; ".join(
                f"{nm} {_fmt_interval(dims[nm])}" for nm in sorted(members)
            )
            raise SceneError(
                "impossible constraints: "
                + " = ".join(sorted(members))
                + f" has an empty combined range ({detail})"
            )
        for nm in members:
            effective[nm] = [lo, hi]
    return effective


_DIM_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}")


def _subst_dims(
    config: str, effective: dict[str, list[float | None]], where: str
) -> str:
    """Resolve every ``{name}`` in a config against the effective dims.

    A reference must resolve to a **number**: the dim (or its equality
    class) must be pinned to an exact value. A one-sided or range dim is a
    spec still narrowing — geometry cannot be built from it, and picking a
    representative silently would launder the openness the author declared.
    """

    def rep(m: re.Match[str]) -> str:
        nm = m[1]
        iv = effective.get(nm)
        if iv is None:
            known = ", ".join(sorted(effective)) or "none"
            raise SceneError(
                f"{where}: config references unknown dim {nm!r} — "
                f"declared dims: {known}"
            )
        lo, hi = iv
        if lo is None or hi is None or lo != hi:
            raise SceneError(
                f"{where}: dim {nm!r} is not pinned to an exact value "
                f"({_fmt_interval(iv)}) — a config needs a number; add "
                f"'dim {nm} = 20mm' (any unit-suffixed value; or pin it via "
                "a constrained dim)"
            )
        return _fmt_num(lo)

    return _DIM_REF_RE.sub(rep, config)


def _resolve_node_dims(spec: SceneSpec) -> SceneSpec:
    """Substitute ``{name}`` config references from the spec's own dims.

    The stored spec keeps the parametric string (edit the ``dim`` line and
    geometry follows on the next build); *this* is what geometry consumers
    see, applied per design inside expansion — a sub-design's configs
    resolve against its own dim namespace, never the parent's. Identity
    fast path when no config carries a reference."""
    if not any("{" in n.config for n in spec.nodes):
        return spec
    raw = {
        str(k): [None if b is None else float(b) for b in v]
        for k, v in (spec.meta.get("dims") or {}).items()
    }
    cons = [(str(a), str(b)) for a, b in (spec.meta.get("constraints") or [])]
    effective = _effective_dims(raw, cons)
    nodes = [
        replace(n, config=_subst_dims(n.config, effective, f"node {n.name!r}"))
        if "{" in n.config
        else n
        for n in spec.nodes
    ]
    return SceneSpec(nodes=nodes, components=list(spec.components), meta=spec.meta)


def _fmt_num(x: float) -> str:
    """Round-trip-safe number formatting for the source language.

    Integers render without a decimal point (``18``); everything else uses
    ``repr`` (which Python guarantees round-trips a float). Both forms match
    ``_LOC_RE`` / ``_ROT_RE`` / the pattern regexes, so
    ``parse_source(spec_to_source(x)) == x`` for any spec that came from
    ``parse_source`` (i.e. authored decimals)."""
    return str(int(x)) if x == int(x) else repr(x)


def _fmt_len(x: float) -> str:
    """Round-trip-safe *length* formatting — a number plus its SI unit
    (``m``), so re-serialised source carries the explicit unit the strict
    boundary parse now requires. The stored value is already SI metres;
    this only attaches the suffix, never scales."""
    return f"{_fmt_num(x)}m"


def _fmt_ang(x: float) -> str:
    """Round-trip-safe *angle* formatting — a number plus its SI unit
    (``rad``), the angle sibling of :func:`_fmt_len`. The stored value is
    already SI radians; this only attaches the suffix, never scales."""
    return f"{_fmt_num(x)}rad"


def _fmt_loc(loc: tuple[float, float, float]) -> str:
    return "@" + ",".join(_fmt_len(v) for v in loc)


def _fmt_rot(rot: tuple[float, float, float]) -> str:
    return "rot:" + ",".join(_fmt_ang(v) for v in rot)


def _pattern_token(pat: NodePattern) -> str:
    """The ``polar:``/``linear:`` source token for a node pattern."""
    if pat["kind"] == "polar":
        return f"polar:n{int(pat['n'])}r{_fmt_len(pat['r'])}"
    if pat["kind"] == "linear":
        tok = f"linear:n{int(pat['n'])}"
        for axis, v in (("dx", pat["dx"]), ("dy", pat["dy"]), ("dz", pat["dz"])):
            if v != 0.0:
                tok += f"{axis}{_fmt_len(v)}"
        return tok
    raise SceneError(f"unknown pattern kind {pat['kind']!r}")  # pragma: no cover


def _node_line(node: NodeSpec) -> str:
    """Serialise one node back to a source line (inverse of the parser).

    A shape node's ``config`` is stored canonical/bare (SI metres, no
    units) — re-attach the ``m`` suffix on every length key
    (:func:`~precis.cad.dsl.format_spec`'s ``units=True`` mode) so the
    emitted line re-parses under the strict boundary too. A
    ``{name}``-parametrized config is the one exemption (its literal
    numbers are already canonical/bare per the grammar's own carve-out) —
    emitted verbatim."""
    sub = instance_slug(node.config)
    code = part_code(node.config)
    if sub is not None:
        parts = ["use", sub, "as", node.name]
    elif code is not None:
        parts = ["part", node.name, code]
    elif "{" in node.config:
        parts = [node.name, node.op, node.config]
    else:
        parts = [node.name, node.op, format_spec(parse(node.config), units=True)]
    if node.loc != (0.0, 0.0, 0.0):
        parts.append(_fmt_loc(node.loc))
    if node.rot != (0.0, 0.0, 0.0):
        parts.append(_fmt_rot(node.rot))
    if node.pattern is not None:
        parts.append(_pattern_token(node.pattern))
    return " ".join(parts)


def spec_to_source(spec: SceneSpec) -> str:
    """Render a :class:`SceneSpec` back to the line-based design language.

    The inverse of :func:`parse_source`: ``desc:``/``use:`` meta first, then
    each component's nodes under a ``component <name>`` header (in node order).
    Round-trips — ``parse_source(spec_to_source(s)) == s`` — so the web editor
    can show an editable source and re-parse an LLM's proposed rewrite."""
    lines: list[str] = []
    desc = str(spec.meta.get("description") or "").strip()
    use = str(spec.meta.get("use") or "").strip()
    if desc:
        lines.append(f"desc: {desc}")
    if use:
        lines.append(f"use: {use}")
    for port in ports_of(spec):
        lines.extend(port.source_lines())
    for mcomp, mslug in (spec.meta.get("materials") or {}).items():
        lines.append(f"material {mcomp} {mslug}")
    for dname, iv in (spec.meta.get("dims") or {}).items():
        lo, hi = iv
        if lo is not None and hi is not None and lo == hi:
            lines.append(f"dim {dname} = {_fmt_len(lo)}")
        else:
            if lo is not None:
                lines.append(f"dim {dname} >= {_fmt_len(lo)}")
            if hi is not None:
                lines.append(f"dim {dname} <= {_fmt_len(hi)}")
    for pair in spec.meta.get("constraints") or []:
        lines.append(f"constrain {pair[0]} = {pair[1]}")
    if lines:
        lines.append("")

    current: str | None = None
    for node in spec.nodes:
        if instance_slug(node.config) is not None or part_code(node.config) is not None:
            # A `use`/`part` directive owns its own component namespace but
            # does not open a block — emit it bare and leave `current` alone,
            # mirroring the parser.
            lines.append(_node_line(node))
            continue
        if node.component != current:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"component {node.component}")
            current = node.component
        lines.append(_node_line(node))

    # Mates/joints/couples last: they address instances and components by
    # name, so they read in dependency order after the lines that declare
    # them (parsing is order-free).
    trailing = [
        *(mate.to_source() for mate in mates_of(spec)),
        *(j.to_source() for j in joints_of(spec)),
        *(c.to_source() for c in couples_of(spec)),
    ]
    if trailing:
        lines.append("")
        lines.extend(trailing)
    return "\n".join(lines) + "\n"


def _node_xform(
    loc: tuple[float, float, float], rot: tuple[float, float, float]
) -> Transform:
    """World transform of a leaf: translate(loc) ∘ rotate(rot)."""
    t = translation(*loc)
    if rot == (0.0, 0.0, 0.0):
        return t
    return t.compose(rotation(*rot))


def _pattern_transforms(node: NodeSpec) -> list[Transform]:
    """Expand a node's pattern into per-instance world transforms."""
    assert node.pattern is not None
    pat = node.pattern
    base_rot = rotation(*node.rot) if node.rot != (0.0, 0.0, 0.0) else identity()
    out: list[Transform] = []
    if pat["kind"] == "polar":
        n = int(pat["n"])
        r = pat["r"]
        z = node.loc[2]
        for i in range(n):
            theta = 2.0 * math.pi * i / n
            xf = rotation(0.0, 0.0, theta).compose(translation(r, 0.0, z))
            out.append(xf.compose(base_rot))
    elif pat["kind"] == "linear":
        n = int(pat["n"])
        dx, dy, dz = pat["dx"], pat["dy"], pat["dz"]
        for i in range(n):
            xf = translation(
                node.loc[0] + i * dx, node.loc[1] + i * dy, node.loc[2] + i * dz
            )
            out.append(xf.compose(base_rot))
    else:  # pragma: no cover - parser guards the kind
        raise SceneError(f"unknown pattern kind {pat['kind']!r}")
    return out


#: Resolves a design slug to its stored :class:`SceneSpec`. Injected because
#: this package imports nothing from the DB — see
#: :func:`precis.cad_resolve.design_resolver` for the production one.
Resolver = Callable[[str], SceneSpec]


def has_instances(spec: SceneSpec) -> bool:
    """True when ``spec`` instances another design or places a catalog
    part (either way, it needs expansion)."""
    return any(
        instance_slug(n.config) is not None or part_code(n.config) is not None
        for n in spec.nodes
    )


def _decompose(
    xf: Transform,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """A rigid transform → the ``(loc, rot)`` pair a :class:`NodeSpec` carries.

    Exact inverse of :func:`_node_xform`, which builds ``translate(loc) ∘
    rotate(rot)`` — so ``t`` *is* the location and ``R`` *is* the rotation;
    only the Euler extraction (:func:`~precis.cad.vec.euler_rad_from_matrix`)
    does any work.
    """
    rx, ry, rz = euler_rad_from_matrix(xf.R)
    return (float(xf.t[0]), float(xf.t[1]), float(xf.t[2])), (rx, ry, rz)


def _placements(node: NodeSpec) -> list[tuple[str, Transform]]:
    """The node's ``(name, world transform)`` copies — one per pattern
    instance (``name#1``…), or a single unpatterned placement."""
    if node.pattern is None:
        return [(node.name, _node_xform(node.loc, node.rot))]
    return [
        (f"{node.name}#{i}", xf)
        for i, xf in enumerate(_pattern_transforms(node), start=1)
    ]


def _inline(
    spec: SceneSpec,
    resolve: Resolver | None,
    xf: Transform,
    prefix: str,
    out: list[NodeSpec],
    stack: tuple[str, ...],
    materials: dict[str, str] | None = None,
) -> None:
    """Append ``spec``'s nodes to ``out``, re-placed under ``xf`` and
    namespaced under ``prefix``, recursing through its own instances.
    ``resolve`` may be ``None`` for a parts-only design — only a ``use``
    node needs it (:func:`expand_instances` pre-checks)."""
    for node in spec.nodes:
        sub_slug = instance_slug(node.config)
        code = part_code(node.config)
        if sub_slug is None and code is None:
            if node.pattern is not None and node.op == "intersect":
                # A patterned node folds as `intersect(cur, union(copies))`;
                # flattened to one node per copy it would fold as a *chain* of
                # intersections, which is a different (near-empty) solid. Add /
                # cut are associative that way, intersect is not — so refuse
                # rather than silently change the sub-design's meaning.
                raise SceneError(
                    f"cannot instance a design whose node {node.name!r} is a "
                    "patterned 'intersect' — flattening it under an instance "
                    "pose would change the solid; split it into explicit nodes"
                )
            for name, local in _placements(node):
                loc, rot = _decompose(xf.compose(local))
                out.append(
                    replace(
                        node,
                        name=f"{prefix}{name}",
                        component=f"{prefix}{node.component}",
                        loc=loc,
                        rot=rot,
                        pattern=None,
                    )
                )
            continue

        if code is not None:
            # A catalog part: the "sub-design" comes from the built-in
            # catalog, needs no resolver, and can never nest or cycle.
            sub = part_spec(code)
        else:
            assert sub_slug is not None
            if sub_slug in stack:
                chain = " → ".join((*stack, sub_slug))
                raise SceneError(f"instance cycle: {chain}")
            if len(stack) + 1 > MAX_INSTANCE_DEPTH:
                raise SceneError(
                    f"instance nesting deeper than {MAX_INSTANCE_DEPTH} at {sub_slug!r}"
                )
            if resolve is None:
                raise SceneError(
                    "this design instances another ('use <slug> as <name>') "
                    "but no resolver was supplied to expand it"
                )
            try:
                sub = resolve(sub_slug)
            except SceneError:
                raise
            except Exception as exc:
                raise SceneError(f"cannot resolve design {sub_slug!r}: {exc}") from exc
            # a sub-design's parametrized configs resolve against its OWN
            # dim namespace, before its nodes inline under ours
            sub = _resolve_node_dims(sub)
        if sub.meta.get("mates") or sub.meta.get("joints"):
            # The sub-design's own mates/joints solve at their defaults
            # before its nodes inline — otherwise its mated instances would
            # arrive frozen at the origin. (state= never reaches down here:
            # it addresses only the top design's joints.)
            sub_states = _resolve_states(sub, None)
            sub_cworld = _component_joint_worlds(sub, sub_states)
            sub = _solve_mates(sub, resolve, sub_states, sub_cworld)
            sub = _pose_component_joints(sub, sub_cworld)
        for name, local in _placements(node):
            sub_prefix = f"{prefix}{name}{NAMESPACE_SEP}"
            if materials is not None:
                for mcomp, mslug in (sub.meta.get("materials") or {}).items():
                    materials.setdefault(f"{sub_prefix}{mcomp}", mslug)
            _inline(
                sub,
                resolve,
                xf.compose(local),
                sub_prefix,
                out,
                (*stack, sub_slug) if sub_slug is not None else stack,
                materials,
            )
        if len(out) > MAX_EXPANDED_NODES:
            raise SceneError(
                f"expanded design exceeds {MAX_EXPANDED_NODES} nodes — "
                "reduce instance nesting or pattern counts"
            )


def _coerce_state(
    name: str,
    kind: str,
    value: Any,
    limits: tuple[float, float] | None,
) -> JointState:
    """Validate one explicit state entry. Out-of-limits is an error (the
    acceptance rule: an explicit illegal pose is rejected, never clamped)."""
    if kind == "cylindrical":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise SceneError(
                f"state[{name!r}]: cylindrical takes [angle, slide] as SI "
                "[radians, metres] here — the args.state boundary "
                "(handlers.cad) converts a unit-suffixed ['45deg', '5mm'] "
                "to this before it reaches the parser"
            )
        ang, dist = float(value[0]), float(value[1])
        if limits is not None and not (limits[0] <= ang <= limits[1]):
            raise SceneError(
                f"state[{name!r}]: angle {_fmt_ang(ang)} outside limits "
                f"{_fmt_ang(limits[0])}..{_fmt_ang(limits[1])}"
            )
        return (ang, dist)
    try:
        q = float(value)
    except (TypeError, ValueError):
        raise SceneError(
            f"state[{name!r}] must be a number (SI, already unit-converted "
            "here — the args.state boundary takes a unit-suffixed value "
            "like '45deg'/'5mm', not this parser's input)"
        ) from None
    if limits is not None and not (limits[0] <= q <= limits[1]):
        fmt = _fmt_len if kind == "prismatic" else _fmt_ang
        raise SceneError(
            f"state[{name!r}]={fmt(q)} outside limits "
            f"{fmt(limits[0])}..{fmt(limits[1])}"
        )
    return q


def _default_state(kind: str, limits: tuple[float, float] | None) -> JointState:
    """Neutral pose: 0, clamped into ``limits:`` when 0 lies outside them
    (a 10..80 mm actuator defaults to 10, not an illegal 0)."""
    q = 0.0
    if limits is not None:
        q = min(max(0.0, limits[0]), limits[1])
    return (q, 0.0) if kind == "cylindrical" else q


def _resolve_states(
    spec: SceneSpec, state: dict[str, Any] | None
) -> dict[str, JointState]:
    """Every joint's final state: defaults, overlaid with the caller's
    ``state=``, with gear/belt couplings derived (or consistency-checked
    when the driven joint was also given explicitly)."""
    joints: dict[str, tuple[str, tuple[float, float] | None]] = {}
    for m in mates_of(spec):
        if m.kind != "fixed":
            joints[m.instance] = (m.kind, m.limits)
    for j in joints_of(spec):
        joints[j.component] = (j.kind, j.limits)

    raw = dict(state or {})
    unknown = sorted(set(raw) - set(joints))
    if unknown:
        known = ", ".join(sorted(joints)) or "none"
        raise SceneError(
            f"state names unknown joint(s) {unknown} — this design's joints: {known}"
        )

    out: dict[str, JointState] = {}
    for name, (kind, limits) in joints.items():
        if name in raw:
            out[name] = _coerce_state(name, kind, raw[name], limits)
        else:
            out[name] = _default_state(kind, limits)

    driver = {c.driven: c for c in couples_of(spec)}
    resolved: set[str] = set()

    def chain_q(name: str) -> float:
        cur = out[name]
        assert isinstance(cur, float)  # couplings refuse 2-DOF joints at parse
        c = driver.get(name)
        if c is None or name in resolved:
            return cur
        resolved.add(name)
        want = c.ratio * chain_q(c.drive)
        if name in raw:
            # Self-relative: ``cur``/``want`` are joint state values, which
            # per-joint-kind are lengths or radians — a fixed absolute
            # threshold means nothing for a length-typed joint whose scale
            # is nanometres.
            if abs(cur - want) > 1e-9 * max(abs(cur), abs(want)):
                raise SceneError(
                    f"state[{name!r}]={cur:g} conflicts with its {c.via} "
                    f"coupling (= {c.ratio:g} × {c.drive} = {want:g}) — "
                    "drop one"
                )
            return cur
        _kind, limits = joints[name]
        if limits is not None and not (limits[0] <= want <= limits[1]):
            raise SceneError(
                f"{c.via} coupling drives joint {name!r} to {want:g}, "
                f"outside limits {limits[0]:g}..{limits[1]:g}"
            )
        out[name] = want
        return want

    for name in driver:
        chain_q(name)
    return out


def _joint_xform(kind: str, q: JointState, pitch: float) -> Transform:
    """The state-dependent transform a joint inserts at its interface —
    about/along the joint frame's local z."""
    if kind == "fixed":
        return identity()
    if kind == "cylindrical":
        ang, dist = q  # type: ignore[misc]
        return translation(0.0, 0.0, dist).compose(rotation(0.0, 0.0, ang))
    assert isinstance(q, float)
    if kind == "revolute":
        return rotation(0.0, 0.0, q)
    if kind == "prismatic":
        return translation(0.0, 0.0, q)
    if kind == "screw":
        return translation(0.0, 0.0, q * pitch / (2.0 * math.pi)).compose(
            rotation(0.0, 0.0, q)
        )
    raise SceneError(f"unknown joint kind {kind!r}")  # pragma: no cover


def _is_neutral(q: JointState) -> bool:
    return q == 0.0 or q == (0.0, 0.0)


def _component_joint_worlds(
    spec: SceneSpec, states: dict[str, JointState]
) -> dict[str, Transform]:
    """Per jointed component, the world transform its joint state applies:
    ``F ∘ J(q) ∘ F⁻¹`` with ``F`` the pivot port's frame (conjugation, so
    the motion happens about the port, not the origin). Components at
    neutral state are omitted — identity by construction."""
    out: dict[str, Transform] = {}
    own_ports = {pt.name: pt for pt in ports_of(spec)}
    for j in joints_of(spec):
        q = states[j.component]
        if _is_neutral(q):
            continue
        frame = own_ports[j.port].frame()
        out[j.component] = frame.compose(_joint_xform(j.kind, q, j.pitch)).compose(
            frame.inverse()
        )
    return out


def _pose_component_joints(spec: SceneSpec, cworld: dict[str, Transform]) -> SceneSpec:
    """Bake each jointed component's world transform into its nodes.

    Patterned nodes flatten to explicit per-copy nodes (a joint pose about
    an off-origin port cannot be expressed as another ``polar:``/``linear:``
    token), with the same patterned-``intersect`` refusal as
    :func:`_inline` and for the same reason.
    """
    if not cworld:
        return spec
    out: list[NodeSpec] = []
    for node in spec.nodes:
        w = cworld.get(node.component)
        if w is None:
            out.append(node)
            continue
        if node.pattern is not None and node.op == "intersect":
            raise SceneError(
                f"cannot pose component {node.component!r}: node "
                f"{node.name!r} is a patterned 'intersect' — flattening it "
                "under a joint pose would change the solid; split it into "
                "explicit nodes"
            )
        for name, local in _placements(node):
            loc, rot = _decompose(w.compose(local))
            out.append(replace(node, name=name, loc=loc, rot=rot, pattern=None))
    return SceneSpec(nodes=out, components=list(spec.components), meta=dict(spec.meta))


def _solve_mates(
    spec: SceneSpec,
    resolve: Resolver | None,
    states: dict[str, JointState] | None = None,
    cworld: dict[str, Transform] | None = None,
) -> SceneSpec:
    """Rewrite each mated/jointed instance node's ``loc``/``rot``.

    A mate fully determines one instance's pose from an already-placed one,
    so this is direct substitution over a spanning tree — never an iterative
    constraint solve. With ``P_s`` the subject port's frame inside its own
    sub-design, ``P_a`` the anchor port's frame in *this* design's
    coordinates, and ``J(q)`` the joint transform at state ``q``
    (:func:`_joint_xform`; identity for a plain mate)::

        X = P_a ∘ Rz(spin) ∘ (Rx(180) if flip) ∘ J(q) ∘ inv(P_s)

    The default is frame **coincidence**; ``flip`` is opt-in. An authored
    port reads as "put the other thing's connection point here", and an
    implicit 180° convention is the kind of thing an author (human or model)
    silently gets backwards.

    An anchor port scoped ``of:`` a component with its own joint follows
    that component's pose (``cworld``) — a motor mated onto an articulated
    arm swings with the arm.

    Solved poses are ephemeral — the stored spec keeps the ``mate`` line,
    exactly as it keeps the ``use`` line.
    """
    states = states or {}
    cworld = cworld or {}
    mates = mates_of(spec)
    if not mates:
        return spec

    instances = {
        n.name: n
        for n in spec.nodes
        if instance_slug(n.config) is not None or part_code(n.config) is not None
    }
    own_ports = {pt.name: pt for pt in ports_of(spec)}

    by_inst: dict[str, MateSpec] = {}
    for mate in mates:
        node = instances.get(mate.instance)
        if node is None:
            raise SceneError(
                f"mate subject {mate.subject!r}: {mate.instance!r} is not an "
                f"instance in this design (declare it with "
                f"'use <design> as {mate.instance}' or "
                f"'part {mate.instance} <family>:<code>')"
            )
        if mate.instance in by_inst:
            raise SceneError(
                f"instance {mate.instance!r} is mated twice — over-constrained; "
                "a single mate already fixes all six degrees of freedom"
            )
        if node.pattern is not None:
            raise SceneError(
                f"mate subject {mate.instance!r} is a patterned instance — a "
                "mate places one body; drop the pattern or place the copies "
                "explicitly"
            )
        if node.loc != (0.0, 0.0, 0.0) or node.rot != (0.0, 0.0, 0.0):
            raise SceneError(
                f"instance {mate.instance!r} is both mated and explicitly "
                "placed (@ / rot:) — over-constrained; drop one"
            )
        by_inst[mate.instance] = mate

    sub_ports: dict[str, dict[str, PortSpec]] = {}
    sub_specs: dict[str, SceneSpec] = {}

    def ports_for(inst: str) -> dict[str, PortSpec]:
        """The ports the sub-design behind instance ``inst`` declares.

        A port scoped ``of:`` one of the sub-design's own jointed components
        is conjugated to that joint's **default** pose — the same pose
        :func:`_inline` bakes into the geometry — so mating onto it lands on
        the deflected frame, not the undeflected authored one (matters when
        a joint's ``limits:`` exclude 0 and the default clamps away from it).
        """
        if inst not in sub_ports:
            code = part_code(instances[inst].config)
            if code is not None:
                sub = part_spec(code)
            else:
                slug = instance_slug(instances[inst].config) or ""
                if resolve is None:
                    raise SceneError(
                        "this design uses 'mate' but no resolver was supplied "
                        "to read the mated designs' ports"
                    )
                try:
                    sub = resolve(slug)
                except SceneError:
                    raise
                except Exception as exc:
                    raise SceneError(f"cannot resolve design {slug!r}: {exc}") from exc
            sub_specs[inst] = sub
            found = {pt.name: pt for pt in ports_of(sub)}
            if sub.meta.get("joints"):
                sub_cw = _component_joint_worlds(sub, _resolve_states(sub, None))
                for pname, pt in found.items():
                    w = sub_cw.get(pt.component)
                    if w is not None:
                        ploc, prot = _decompose(w.compose(pt.frame()))
                        found[pname] = replace(pt, loc=ploc, rot=prot)
            sub_ports[inst] = found
        return sub_ports[inst]

    def require_port(inst: str, port: str) -> PortSpec:
        available = ports_for(inst)
        found = available.get(port)
        if found is None:
            known = ", ".join(sorted(available)) or "none"
            cfg = instances[inst].config
            slug = instance_slug(cfg) or part_code(cfg) or "?"
            raise SceneError(
                f"design {slug!r} (instance {inst!r}) has no port {port!r} "
                f"— declared ports: {known}"
            )
        return found

    solved: dict[str, Transform] = {}

    def pose_of(inst: str, stack: tuple[str, ...]) -> Transform:
        if inst in solved:
            return solved[inst]
        if inst in stack:
            raise SceneError("mate cycle: " + " → ".join((*stack, inst)))
        mate = by_inst.get(inst)
        node = instances[inst]
        if mate is None:
            # Not mated: it sits where it was placed (the origin by default).
            # Deliberately not an error — a frame or base instance at the
            # origin is legitimate authoring, and slice-1 designs rely on it.
            xf = _node_xform(node.loc, node.rot)
            solved[inst] = xf
            return xf

        if mate.anchor_instance is None:
            anchor_port = own_ports.get(mate.anchor_port)
            if anchor_port is None:
                known = ", ".join(sorted(own_ports)) or "none"
                raise SceneError(
                    f"mate anchor {mate.anchor!r} is not a port of this design "
                    f"— declared ports: {known} (an anchor is either "
                    f"'<port>' here or '<instance>{NAMESPACE_SEP}<port>')"
                )
            anchor_world = anchor_port.frame()
            # Payload splices into an own component carry the *pre-joint*
            # frame: _pose_component_joints bakes cworld into every node of
            # a jointed component later, payload rows included.
            anchor_host_frame = anchor_world
            w = cworld.get(anchor_port.component)
            if w is not None:
                # The port rides a component with its own joint — follow it.
                anchor_world = w.compose(anchor_world)
        else:
            if mate.anchor_instance not in instances:
                raise SceneError(
                    f"mate anchor {mate.anchor!r}: {mate.anchor_instance!r} is "
                    "not an instance in this design"
                )
            if instances[mate.anchor_instance].pattern is not None:
                # A patterned anchor is N frames, not one — picking the base
                # copy would silently place the subject against an arbitrary
                # member of the array.
                raise SceneError(
                    f"mate anchor {mate.anchor!r}: instance "
                    f"{mate.anchor_instance!r} is patterned, so its port is "
                    "many frames, not one — mate against an unpatterned "
                    "instance or place the subject explicitly"
                )
            anchor_port = require_port(mate.anchor_instance, mate.anchor_port)
            anchor_world = pose_of(mate.anchor_instance, (*stack, inst)).compose(
                anchor_port.frame()
            )
            # An instance's components are never in cworld (only this
            # design's own jointed components are), so the world frame IS
            # the host-rigid frame.
            anchor_host_frame = anchor_world

        subject_port = require_port(inst, mate.port)
        if (
            subject_port.type
            and anchor_port.type
            and subject_port.type != anchor_port.type
        ):
            raise SceneError(
                f"port type mismatch: {mate.subject} is "
                f"type:{subject_port.type} but {mate.anchor} is "
                f"type:{anchor_port.type} — typed ports only mate like with "
                "like"
            )
        xf = anchor_world
        host_iface = anchor_host_frame
        if mate.spin:
            xf = xf.compose(rotation(0.0, 0.0, mate.spin))
            host_iface = host_iface.compose(rotation(0.0, 0.0, mate.spin))
        if mate.flip:
            xf = xf.compose(rotation(math.pi, 0.0, 0.0))
            host_iface = host_iface.compose(rotation(math.pi, 0.0, 0.0))
        if mate.kind != "fixed":
            xf = xf.compose(_joint_xform(mate.kind, states[inst], mate.pitch))
        xf = xf.compose(subject_port.frame().inverse())

        # Straddling modules (slice 5): each side's port payloads splice
        # into the body on the *other* side of the mate, as plain nodes
        # named `<instance>~<payload>` so the host's tree attributes them.
        # A subject payload is rigid in the host — it sits at the interface
        # frame *before* J(q) (a hinge's recess is machined into the host;
        # it does not swing with the hinge). An anchor payload rides the
        # subject, so its frame includes J(q).
        if subject_port.payloads:
            if not anchor_port.component:
                raise SceneError(
                    f"mate {mate.subject} to {mate.anchor}: port "
                    f"{mate.port!r} carries payload geometry, but the anchor "
                    f"port {mate.anchor_port!r} is not scoped of: a component "
                    "— a payload needs a host body to splice into"
                )
            host = (
                anchor_port.component
                if mate.anchor_instance is None
                else f"{mate.anchor_instance}{NAMESPACE_SEP}{anchor_port.component}"
            )
            _require_host_body(
                mate,
                spec
                if mate.anchor_instance is None
                else sub_specs[mate.anchor_instance],
                anchor_port.component,
                host,
            )
            _splice(inst, subject_port.payloads, host, host_iface)
        if anchor_port.payloads:
            if not subject_port.component:
                raise SceneError(
                    f"mate {mate.subject} to {mate.anchor}: port "
                    f"{mate.anchor_port!r} carries payload geometry, but the "
                    f"subject port {mate.port!r} is not scoped of: a "
                    "component — a payload needs a host body to splice into"
                )
            host = f"{inst}{NAMESPACE_SEP}{subject_port.component}"
            _require_host_body(mate, sub_specs[inst], subject_port.component, host)
            _splice(
                inst,
                anchor_port.payloads,
                host,
                xf.compose(subject_port.frame()),
            )

        solved[inst] = xf
        return xf

    spliced: list[NodeSpec] = []

    def _require_host_body(
        mate: MateSpec, host_spec: SceneSpec, comp: str, host: str
    ) -> None:
        """A payload features an *existing* body. Splicing into a component
        with no shape nodes of its own would make the payload that
        component's base — and a base ignores its op, so a ``cut`` payload
        would silently *add* material. Refuse instead."""
        if not any(
            n.component == comp
            and instance_slug(n.config) is None
            and part_code(n.config) is None
            for n in host_spec.nodes
        ):
            raise SceneError(
                f"mate {mate.subject} to {mate.anchor}: payload host "
                f"component {host!r} has no geometry of its own — a payload "
                "modifies an existing body; give it a base node first"
            )

    def _splice(
        inst: str, pls: tuple[PayloadSpec, ...], host: str, frame: Transform
    ) -> None:
        for pl in pls:
            loc, rot = _decompose(frame.compose(_node_xform(pl.loc, pl.rot)))
            spliced.append(
                NodeSpec(
                    name=f"{inst}{PAYLOAD_SEP}{pl.name}",
                    op=pl.op,
                    config=pl.config,
                    component=host,
                    loc=loc,
                    rot=rot,
                )
            )

    out: list[NodeSpec] = []
    for node in spec.nodes:
        if node.name in by_inst:
            loc, rot = _decompose(pose_of(node.name, ()))
            out.append(replace(node, loc=loc, rot=rot))
        else:
            out.append(node)
    # Payload rows land after every authored node, so each host's own
    # geometry folds first and the payload adds/cuts apply to the finished
    # body (build_design folds a component's nodes in list order).
    out.extend(spliced)
    return SceneSpec(nodes=out, components=list(spec.components), meta=dict(spec.meta))


def expand_instances(
    spec: SceneSpec,
    resolve: Resolver | None = None,
    state: dict[str, Any] | None = None,
) -> SceneSpec:
    """Inline every ``use <slug> as <name>`` into a flat, self-contained spec.

    The stored spec keeps the compact instance node; *this* is what the probe,
    relate, export and tessellate layers consume, so a sub-assembly is a real
    multi-body part everywhere without any of them learning about instancing.
    Names and components are namespaced ``<instance>.<name>`` and the
    sub-design's poses are composed under the instance's own.

    Interfaces are solved first: joint states resolve
    (:func:`_resolve_states` — ``state=`` overlays the defaults, couplings
    derive), then mates/joints rewrite each subject's pose in place
    (:func:`_solve_mates`, :func:`_pose_component_joints`) — so from here
    down a mated instance is indistinguishable from a hand-placed one.
    ``state=`` addresses only *this* design's joints; an instanced
    sub-design's own joints pose at their defaults.

    A spec with no instances, mates, or joints is returned **unchanged**
    (identity fast path), so plain designs are byte-identical through here.
    """
    # Parametrized configs resolve first (against THIS design's dims);
    # identity when nothing references a dim, so plain specs stay
    # byte-identical through the fast path below.
    spec = _resolve_node_dims(spec)
    has_iface = bool(spec.meta.get("mates") or spec.meta.get("joints"))
    if not has_instances(spec) and not has_iface:
        if state:
            raise SceneError("args.state was given but this design declares no joints")
        return spec
    states = _resolve_states(spec, state)
    cworld = _component_joint_worlds(spec, states)
    spec = _solve_mates(spec, resolve, states, cworld)
    spec = _pose_component_joints(spec, cworld)

    if has_instances(spec):
        if resolve is None and any(
            instance_slug(n.config) is not None for n in spec.nodes
        ):
            # Only `use` needs the store: a parts-only design expands from
            # the built-in catalog alone.
            raise SceneError(
                "this design instances another ('use <slug> as <name>') but "
                "no resolver was supplied to expand it"
            )
        out: list[NodeSpec] = []
        sub_materials: dict[str, str] = {}
        for node in spec.nodes:
            if instance_slug(node.config) is None and part_code(node.config) is None:
                # Top-level nodes are kept verbatim — pattern included — so an
                # unrelated instance elsewhere cannot perturb them.
                out.append(node)
                continue
            _inline(
                SceneSpec(nodes=[node]),
                resolve,
                identity(),
                "",
                out,
                (),
                sub_materials,
            )
    else:
        out = list(spec.nodes)

    components: list[str] = []
    seen: set[str] = set()
    for node in out:
        if node.component not in seen:
            seen.add(node.component)
            components.append(node.component)
    names = [n.name for n in out]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise SceneError(
            f"instance expansion produced duplicate node names: {dupes} — "
            "rename the colliding instance, part, or payload"
        )
    # Expansion *consumes* the mates / joints / couples: their poses are now
    # baked into the nodes, and the instances/components they addressed may
    # no longer exist under those names. Leaving them on the meta would make
    # a second pass over an already-expanded spec (build_design re-runs
    # this) fail with "not an instance". Ports stay — they still describe
    # this design's interfaces, and the search card reads them off the
    # expanded spec.
    meta = {
        k: v for k, v in spec.meta.items() if k not in ("mates", "joints", "couples")
    }
    if has_instances(spec):
        # Sub-designs' material assignments follow their components in,
        # namespaced. Disjoint from the top design's own keys by
        # construction (namespaced vs bare component names).
        merged = {**sub_materials, **(meta.get("materials") or {})}
        if merged:
            meta["materials"] = merged
    return SceneSpec(nodes=out, components=components, meta=meta)


def build_design(
    spec: SceneSpec,
    *,
    resolve: Resolver | None = None,
    state: dict[str, Any] | None = None,
) -> Design:
    """Build a live :class:`Design` from a :class:`SceneSpec`.

    ``resolve`` is required only when ``spec`` instances another design;
    ``state=`` poses its joints — both are threaded through
    :func:`expand_instances`.
    """
    spec = expand_instances(spec, resolve, state)
    design = Design()
    per_component: dict[str, Expr] = {}

    for node in spec.nodes:
        prim = build_config(node.config)
        if node.pattern is not None:
            node_expr: Expr = design.pattern(node.name, prim, _pattern_transforms(node))
        else:
            node_expr = design.prim(node.name, prim, _node_xform(node.loc, node.rot))

        cur = per_component.get(node.component)
        if cur is None:
            per_component[node.component] = node_expr
        elif node.op == "add":
            per_component[node.component] = design.merge(cur, node_expr)
        elif node.op == "cut":
            per_component[node.component] = design.subtract(cur, node_expr)
        elif node.op == "intersect":
            per_component[node.component] = design.intersect(cur, node_expr)
        else:  # pragma: no cover - parser guards op
            raise SceneError(f"unknown op {node.op!r}")

    for comp in spec.components:
        if comp in per_component:
            design.add_component(comp, per_component[comp])
    return design
