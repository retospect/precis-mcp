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
    plate     add  cyl:r25h8
    hub_bore  cut  cyl:r8h10    @0,0,-1
    bolts     cut  cyl:r2.5h10  @18,0,-1  polar:n6r18
    rim       add  box:w4d4h4   @20,0,0   rot:0,0,45

- ``desc: <text>`` / ``use: <text>`` (optional, anywhere) record what the
  design *is* and what it's *for*; folded into the searchable card.
- ``component <name>`` opens a part; nodes until the next ``component``
  belong to it (default part name ``part`` if none is declared).
- ``use <slug> as <name> [@x,y,z] [rot:...] [polar:/linear:]`` instances
  *another design* as a sub-assembly (:func:`expand_instances`). It is a
  top-level directive like ``component`` — it does not join, or close, the
  enclosing component block.
- ``port <name> [@x,y,z] [rot:...]`` names a frame on *this* design — the
  interface another design mates to.
- ``mate <inst>.<port> to <anchor> [flip] [spin:<deg>]`` places instance
  ``<inst>`` by making its port coincide with ``<anchor>`` (this design's own
  ``<port>``, or another instance's ``<inst>.<port>``) — see
  :func:`_solve_mates`. Also top-level.
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
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypedDict

from precis.cad.dsl import build, build_config, parse
from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.vec import (
    Transform,
    as_float3,
    euler_deg_from_matrix,
    identity,
    rotation,
    translation,
)

log = logging.getLogger(__name__)

_OPS = ("add", "cut", "intersect")

#: A node whose ``config`` starts with this instances another design rather
#: than building a primitive — see :func:`expand_instances`.
INSTANCE_PREFIX = "use:"

#: Separator between an instance's name and the sub-design's own node /
#: component names once inlined (``f1.plate``).
NAMESPACE_SEP = "."

#: How deep ``use`` may nest before we call it a runaway.
MAX_INSTANCE_DEPTH = 8

#: Ceiling on the expanded node count — a 6-deep tree of 6-way polar
#: instances is 46656 nodes, which would wedge a worker rather than fail.
MAX_EXPANDED_NODES = 20_000

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LOC_RE = re.compile(r"^@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")
_ROT_RE = re.compile(r"^rot:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")
_POLAR_RE = re.compile(r"^polar:n(\d+)r(-?\d+(?:\.\d+)?)$")
_SPIN_RE = re.compile(r"^spin:(-?\d+(?:\.\d+)?)$")
_LINEAR_RE = re.compile(
    r"^linear:n(\d+)"
    r"(?:dx(-?\d+(?:\.\d+)?))?"
    r"(?:dy(-?\d+(?:\.\d+)?))?"
    r"(?:dz(-?\d+(?:\.\d+)?))?$"
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

    def to_meta(self) -> dict[str, Any]:
        return {"name": self.name, "loc": list(self.loc), "rot": list(self.rot)}

    @classmethod
    def from_meta(cls, raw: Any) -> PortSpec:
        if not isinstance(raw, dict) or not str(raw.get("name") or ""):
            raise SceneError(f"malformed stored port {raw!r}")
        return cls(
            name=str(raw["name"]),
            loc=as_float3(raw.get("loc")),
            rot=as_float3(raw.get("rot")),
        )

    def to_source(self) -> str:
        """The ``port …`` source line (also what the node tree shows)."""
        parts = ["port", self.name]
        if self.loc != (0.0, 0.0, 0.0):
            parts.append("@" + ",".join(_fmt_num(v) for v in self.loc))
        if self.rot != (0.0, 0.0, 0.0):
            parts.append("rot:" + ",".join(_fmt_num(v) for v in self.rot))
        return " ".join(parts)

    def frame(self) -> Transform:
        """The port's pose in its own design's coordinates."""
        return _node_xform(self.loc, self.rot)


@dataclass(frozen=True)
class MateSpec:
    """``mate <instance>.<port> to <anchor> [flip] [spin:<deg>]``.

    ``anchor_instance`` is ``None`` when the anchor is one of *this* design's
    own ports (fixed in the design frame); otherwise it names another
    instance whose pose must be solved first.
    """

    instance: str
    port: str
    anchor_instance: str | None
    anchor_port: str
    flip: bool = False
    spin: float = 0.0

    @property
    def subject(self) -> str:
        return f"{self.instance}{NAMESPACE_SEP}{self.port}"

    @property
    def anchor(self) -> str:
        if self.anchor_instance is None:
            return self.anchor_port
        return f"{self.anchor_instance}{NAMESPACE_SEP}{self.anchor_port}"

    def to_source(self) -> str:
        """The ``mate …`` source line (also what the node tree shows)."""
        parts = ["mate", self.subject, "to", self.anchor]
        if self.flip:
            parts.append("flip")
        if self.spin:
            parts.append(f"spin:{_fmt_num(self.spin)}")
        return " ".join(parts)

    def to_meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {"subject": self.subject, "anchor": self.anchor}
        if self.flip:
            m["flip"] = True
        if self.spin:
            m["spin"] = self.spin
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
            where="stored mate",
        )

    @classmethod
    def build(
        cls, *, subject: str, anchor: str, flip: bool, spin: float, where: str
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
        return cls(
            instance=parts[0],
            port=parts[1],
            anchor_instance=a_parts[0] if len(a_parts) == 2 else None,
            anchor_port=a_parts[-1],
            flip=flip,
            spin=spin,
        )


def ports_of(spec: SceneSpec) -> list[PortSpec]:
    """This design's declared ports (empty when it declares none)."""
    return [PortSpec.from_meta(r) for r in spec.meta.get("ports") or ()]


def mates_of(spec: SceneSpec) -> list[MateSpec]:
    """This design's declared mates (empty when it declares none)."""
    return [MateSpec.from_meta(r) for r in spec.meta.get("mates") or ()]


def _parse_placement(
    toks: list[str], lineno: int
) -> tuple[tuple[float, float, float], tuple[float, float, float], NodePattern | None]:
    """Parse the trailing ``@x,y,z`` / ``rot:`` / ``polar:`` / ``linear:``
    tokens shared by shape nodes and ``use`` instance directives."""
    loc = (0.0, 0.0, 0.0)
    rot = (0.0, 0.0, 0.0)
    pattern: NodePattern | None = None
    for tok in toks:
        if m := _LOC_RE.match(tok):
            loc = (float(m[1]), float(m[2]), float(m[3]))
        elif m := _ROT_RE.match(tok):
            rot = (float(m[1]), float(m[2]), float(m[3]))
        elif m := _POLAR_RE.match(tok):
            pattern = PolarPattern(kind="polar", n=float(m[1]), r=float(m[2]))
        elif m := _LINEAR_RE.match(tok):
            pattern = LinearPattern(
                kind="linear",
                n=float(m[1]),
                dx=float(m[2] or 0.0),
                dy=float(m[3] or 0.0),
                dz=float(m[4] or 0.0),
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
    mates: list[MateSpec] = []

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
        if toks[0] == "port":
            # `port <name> [@x,y,z] [rot:...]` — a named frame on this design.
            # Top-level like `component`/`use`: `current` is untouched.
            if len(toks) < 2:
                raise SceneError(
                    f"line {lineno}: expected 'port <name> [@x,y,z] [rot:...]'"
                )
            pname = toks[1]
            if not _IDENT_RE.match(pname):
                raise SceneError(f"line {lineno}: bad port name {pname!r}")
            if any(pt.name == pname for pt in ports):
                raise SceneError(f"line {lineno}: duplicate port {pname!r}")
            ploc, prot, ppat = _parse_placement(toks[2:], lineno)
            if ppat is not None:
                raise SceneError(
                    f"line {lineno}: port {pname!r} cannot carry a pattern — "
                    "a port is a single frame; declare one port per interface"
                )
            ports.append(PortSpec(name=pname, loc=ploc, rot=prot))
            continue
        if toks[0] == "mate":
            # `mate <inst>.<port> to <anchor> [flip] [spin:<deg>]`.
            if len(toks) < 4 or toks[2] != "to":
                raise SceneError(
                    f"line {lineno}: expected 'mate <instance>{NAMESPACE_SEP}<port> "
                    "to <anchor> [flip] [spin:<deg>]'"
                )
            flip = False
            spin = 0.0
            for tok in toks[4:]:
                if tok == "flip":
                    flip = True
                elif m := _SPIN_RE.match(tok):
                    spin = float(m[1])
                else:
                    raise SceneError(f"line {lineno}: unrecognised token {tok!r}")
            mates.append(
                MateSpec.build(
                    subject=toks[1],
                    anchor=toks[3],
                    flip=flip,
                    spin=spin,
                    where=f"line {lineno}",
                )
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
        # validate the shape config eagerly (raises on bad DSL)
        node_spec = parse(config)
        build(node_spec)
        if node_spec.alias == "chamfer":
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

    if ports:
        spec.meta["ports"] = [pt.to_meta() for pt in ports]
    if mates:
        spec.meta["mates"] = [mt.to_meta() for mt in mates]
    spec.components = seen_components or ["part"]
    return spec


def _fmt_num(x: float) -> str:
    """Round-trip-safe number formatting for the source language.

    Integers render without a decimal point (``18``); everything else uses
    ``repr`` (which Python guarantees round-trips a float). Both forms match
    ``_LOC_RE`` / ``_ROT_RE`` / the pattern regexes, so
    ``parse_source(spec_to_source(x)) == x`` for any spec that came from
    ``parse_source`` (i.e. authored decimals)."""
    return str(int(x)) if x == int(x) else repr(x)


def _pattern_token(pat: NodePattern) -> str:
    """The ``polar:``/``linear:`` source token for a node pattern."""
    if pat["kind"] == "polar":
        return f"polar:n{int(pat['n'])}r{_fmt_num(pat['r'])}"
    if pat["kind"] == "linear":
        tok = f"linear:n{int(pat['n'])}"
        for axis, v in (("dx", pat["dx"]), ("dy", pat["dy"]), ("dz", pat["dz"])):
            if v != 0.0:
                tok += f"{axis}{_fmt_num(v)}"
        return tok
    raise SceneError(f"unknown pattern kind {pat['kind']!r}")  # pragma: no cover


def _node_line(node: NodeSpec) -> str:
    """Serialise one node back to a source line (inverse of the parser)."""
    sub = instance_slug(node.config)
    if sub is None:
        parts = [node.name, node.op, node.config]
    else:
        parts = ["use", sub, "as", node.name]
    if node.loc != (0.0, 0.0, 0.0):
        parts.append("@" + ",".join(_fmt_num(v) for v in node.loc))
    if node.rot != (0.0, 0.0, 0.0):
        parts.append("rot:" + ",".join(_fmt_num(v) for v in node.rot))
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
    lines.extend(port.to_source() for port in ports_of(spec))
    if lines:
        lines.append("")

    current: str | None = None
    for node in spec.nodes:
        if instance_slug(node.config) is not None:
            # A `use` directive owns its own component namespace but does not
            # open a block — emit it bare and leave `current` alone, mirroring
            # the parser.
            lines.append(_node_line(node))
            continue
        if node.component != current:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"component {node.component}")
            current = node.component
        lines.append(_node_line(node))

    # Mates last: they address instances by name, so they read in dependency
    # order after the `use` lines that declare them (parsing is order-free).
    mates = mates_of(spec)
    if mates:
        lines.append("")
    lines.extend(mate.to_source() for mate in mates)
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
            theta = 360.0 * i / n
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
    """True when ``spec`` instances another design (needs expansion)."""
    return any(instance_slug(n.config) is not None for n in spec.nodes)


def _decompose(
    xf: Transform,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """A rigid transform → the ``(loc, rot)`` pair a :class:`NodeSpec` carries.

    Exact inverse of :func:`_node_xform`, which builds ``translate(loc) ∘
    rotate(rot)`` — so ``t`` *is* the location and ``R`` *is* the rotation;
    only the Euler extraction (:func:`~precis.cad.vec.euler_deg_from_matrix`)
    does any work.
    """
    rx, ry, rz = euler_deg_from_matrix(xf.R)
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
    resolve: Resolver,
    xf: Transform,
    prefix: str,
    out: list[NodeSpec],
    stack: tuple[str, ...],
) -> None:
    """Append ``spec``'s nodes to ``out``, re-placed under ``xf`` and
    namespaced under ``prefix``, recursing through its own instances."""
    for node in spec.nodes:
        sub_slug = instance_slug(node.config)
        if sub_slug is None:
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

        if sub_slug in stack:
            chain = " → ".join((*stack, sub_slug))
            raise SceneError(f"instance cycle: {chain}")
        if len(stack) + 1 > MAX_INSTANCE_DEPTH:
            raise SceneError(
                f"instance nesting deeper than {MAX_INSTANCE_DEPTH} at {sub_slug!r}"
            )
        try:
            sub = resolve(sub_slug)
        except SceneError:
            raise
        except Exception as exc:
            raise SceneError(f"cannot resolve design {sub_slug!r}: {exc}") from exc
        for name, local in _placements(node):
            _inline(
                sub,
                resolve,
                xf.compose(local),
                f"{prefix}{name}{NAMESPACE_SEP}",
                out,
                (*stack, sub_slug),
            )
        if len(out) > MAX_EXPANDED_NODES:
            raise SceneError(
                f"expanded design exceeds {MAX_EXPANDED_NODES} nodes — "
                "reduce instance nesting or pattern counts"
            )


def _solve_mates(spec: SceneSpec, resolve: Resolver | None) -> SceneSpec:
    """Rewrite each mated instance node's ``loc``/``rot`` from its mate.

    A mate fully determines one instance's pose from an already-placed one,
    so this is direct substitution over a spanning tree — never an iterative
    constraint solve. With ``P_s`` the subject port's frame inside its own
    sub-design and ``P_a`` the anchor port's frame in *this* design's
    coordinates::

        X = P_a ∘ Rz(spin) ∘ (Rx(180) if flip) ∘ inv(P_s)

    The default is frame **coincidence**; ``flip`` is opt-in. An authored
    port reads as "put the other thing's connection point here", and an
    implicit 180° convention is the kind of thing an author (human or model)
    silently gets backwards.

    Solved poses are ephemeral — the stored spec keeps the ``mate`` line,
    exactly as it keeps the ``use`` line.
    """
    mates = mates_of(spec)
    if not mates:
        return spec

    instances = {n.name: n for n in spec.nodes if instance_slug(n.config) is not None}
    own_ports = {pt.name: pt for pt in ports_of(spec)}

    by_inst: dict[str, MateSpec] = {}
    for mate in mates:
        node = instances.get(mate.instance)
        if node is None:
            raise SceneError(
                f"mate subject {mate.subject!r}: {mate.instance!r} is not an "
                f"instance in this design (declare it with "
                f"'use <design> as {mate.instance}')"
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

    def ports_for(inst: str) -> dict[str, PortSpec]:
        """The ports the sub-design behind instance ``inst`` declares."""
        if inst not in sub_ports:
            slug = instance_slug(instances[inst].config) or ""
            if resolve is None:
                raise SceneError(
                    "this design uses 'mate' but no resolver was supplied to "
                    "read the mated designs' ports"
                )
            try:
                sub = resolve(slug)
            except SceneError:
                raise
            except Exception as exc:
                raise SceneError(f"cannot resolve design {slug!r}: {exc}") from exc
            sub_ports[inst] = {pt.name: pt for pt in ports_of(sub)}
        return sub_ports[inst]

    def require_port(inst: str, port: str) -> PortSpec:
        available = ports_for(inst)
        found = available.get(port)
        if found is None:
            known = ", ".join(sorted(available)) or "none"
            slug = instance_slug(instances[inst].config) or "?"
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

        xf = anchor_world
        if mate.spin:
            xf = xf.compose(rotation(0.0, 0.0, mate.spin))
        if mate.flip:
            xf = xf.compose(rotation(180.0, 0.0, 0.0))
        xf = xf.compose(require_port(inst, mate.port).frame().inverse())
        solved[inst] = xf
        return xf

    out: list[NodeSpec] = []
    for node in spec.nodes:
        if node.name in by_inst:
            loc, rot = _decompose(pose_of(node.name, ()))
            out.append(replace(node, loc=loc, rot=rot))
        else:
            out.append(node)
    return SceneSpec(nodes=out, components=list(spec.components), meta=dict(spec.meta))


def expand_instances(spec: SceneSpec, resolve: Resolver | None = None) -> SceneSpec:
    """Inline every ``use <slug> as <name>`` into a flat, self-contained spec.

    The stored spec keeps the compact instance node; *this* is what the probe,
    relate, export and tessellate layers consume, so a sub-assembly is a real
    multi-body part everywhere without any of them learning about instancing.
    Names and components are namespaced ``<instance>.<name>`` and the
    sub-design's poses are composed under the instance's own.

    Mates are solved first (:func:`_solve_mates`), which rewrites each mated
    instance node's pose in place — so from here down a mated instance is
    indistinguishable from a hand-placed one.

    A spec with no instances and no mates is returned **unchanged** (identity
    fast path), so non-instanced designs are byte-identical through this code.
    """
    if not has_instances(spec) and not spec.meta.get("mates"):
        return spec
    spec = _solve_mates(spec, resolve)
    if resolve is None:
        raise SceneError(
            "this design instances another ('use <slug> as <name>') but no "
            "resolver was supplied to expand it"
        )
    out: list[NodeSpec] = []
    for node in spec.nodes:
        if instance_slug(node.config) is None:
            # Top-level nodes are kept verbatim — pattern included — so an
            # unrelated instance elsewhere in the design cannot perturb them.
            out.append(node)
            continue
        _inline(SceneSpec(nodes=[node]), resolve, identity(), "", out, ())

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
            "rename the colliding instance or part"
        )
    # Expansion *consumes* the mates: their poses are now baked into the
    # inlined nodes, and the instances they addressed no longer exist. Leaving
    # them on the meta would make a second pass over an already-expanded spec
    # (build_design re-runs this) fail with "not an instance". Ports stay —
    # they still describe this design's interfaces, and the search card reads
    # them off the expanded spec.
    meta = {k: v for k, v in spec.meta.items() if k != "mates"}
    return SceneSpec(nodes=out, components=components, meta=meta)


def build_design(spec: SceneSpec, *, resolve: Resolver | None = None) -> Design:
    """Build a live :class:`Design` from a :class:`SceneSpec`.

    ``resolve`` is required only when ``spec`` instances another design; it is
    threaded through :func:`expand_instances`.
    """
    spec = expand_instances(spec, resolve)
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
