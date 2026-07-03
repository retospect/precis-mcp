"""Design source ↔ scene spec ↔ live :class:`Design` (ADR 0041 §3, §11).

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
- ``<name> <op> <config> [@x,y,z] [rot:rx,ry,rz] [polar:nNrR] [linear:nNdx..dy..dz..]``
  — ``op`` ∈ {``add``, ``cut``, ``intersect``}; ``config`` is the §11
  mini-DSL (:mod:`precis.cad.dsl`). The first node in a part is its base;
  later ``add`` merges, ``cut`` subtracts, ``intersect`` intersects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.vec import Transform, identity, rotation, translation

_OPS = ("add", "cut", "intersect")
_LOC_RE = re.compile(r"^@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")
_ROT_RE = re.compile(r"^rot:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$")
_POLAR_RE = re.compile(r"^polar:n(\d+)r(-?\d+(?:\.\d+)?)$")
_LINEAR_RE = re.compile(
    r"^linear:n(\d+)"
    r"(?:dx(-?\d+(?:\.\d+)?))?"
    r"(?:dy(-?\d+(?:\.\d+)?))?"
    r"(?:dz(-?\d+(?:\.\d+)?))?$"
)


class SceneError(ValueError):
    """A malformed design source line."""


@dataclass(frozen=True)
class NodeSpec:
    """One parsed design node — the serialisable unit the handler stores."""

    name: str
    op: str  # add | cut | intersect
    config: str  # the mini-DSL shape string
    component: str
    loc: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pattern: dict[str, float] | None = None  # {kind:'polar'|'linear', ...}

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
        pat = meta.get("pattern")
        return cls(
            name=name,
            op=str(meta.get("op", "add")),
            config=str(meta.get("config", "")),
            component=str(meta.get("component", "part")),
            loc=(loc[0], loc[1], loc[2]),
            rot=(rot[0], rot[1], rot[2]),
            pattern=dict(pat) if isinstance(pat, dict) else None,
        )


@dataclass
class SceneSpec:
    """A whole design: ordered nodes grouped into named components."""

    nodes: list[NodeSpec] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=lambda: {"units": "mm"})


def parse_source(text: str) -> SceneSpec:
    """Parse the line-based design language into a :class:`SceneSpec`."""
    spec = SceneSpec()
    current = "part"
    seen_components: list[str] = []
    seen_names: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("desc:") or low.startswith("use:"):
            # free-text design intent — folded into the one search card so
            # designs are findable by purpose, not just geometry (ADR 0041
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
            if current not in seen_components:
                seen_components.append(current)
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
        build_config(config)

        loc = (0.0, 0.0, 0.0)
        rot = (0.0, 0.0, 0.0)
        pattern: dict[str, float] | None = None
        for tok in toks[3:]:
            if m := _LOC_RE.match(tok):
                loc = (float(m[1]), float(m[2]), float(m[3]))
            elif m := _ROT_RE.match(tok):
                rot = (float(m[1]), float(m[2]), float(m[3]))
            elif m := _POLAR_RE.match(tok):
                pattern = {"kind": "polar", "n": float(m[1]), "r": float(m[2])}  # type: ignore[dict-item]
            elif m := _LINEAR_RE.match(tok):
                pattern = {
                    "kind": "linear",  # type: ignore[dict-item]
                    "n": float(m[1]),
                    "dx": float(m[2] or 0.0),
                    "dy": float(m[3] or 0.0),
                    "dz": float(m[4] or 0.0),
                }
            else:
                raise SceneError(f"line {lineno}: unrecognised token {tok!r}")

        if current not in seen_components:
            seen_components.append(current)
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


def _pattern_token(pat: dict[str, float]) -> str:
    """The ``polar:``/``linear:`` source token for a node pattern."""
    kind = pat["kind"]
    if kind == "polar":  # type: ignore[comparison-overlap]
        return f"polar:n{int(pat['n'])}r{_fmt_num(float(pat['r']))}"
    if kind == "linear":  # type: ignore[comparison-overlap]
        tok = f"linear:n{int(pat['n'])}"
        for axis in ("dx", "dy", "dz"):
            v = float(pat.get(axis, 0.0))
            if v != 0.0:
                tok += f"{axis}{_fmt_num(v)}"
        return tok
    raise SceneError(f"unknown pattern kind {kind!r}")  # pragma: no cover


def _node_line(node: NodeSpec) -> str:
    """Serialise one node back to a source line (inverse of the parser)."""
    parts = [node.name, node.op, node.config]
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
    if lines:
        lines.append("")

    current: str | None = None
    for node in spec.nodes:
        if node.component != current:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"component {node.component}")
            current = node.component
        lines.append(_node_line(node))
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
    if pat["kind"] == "polar":  # type: ignore[index]
        n = int(pat["n"])
        r = float(pat["r"])
        z = node.loc[2]
        for i in range(n):
            theta = 360.0 * i / n
            xf = rotation(0.0, 0.0, theta).compose(translation(r, 0.0, z))
            out.append(xf.compose(base_rot))
    elif pat["kind"] == "linear":  # type: ignore[index]
        n = int(pat["n"])
        dx, dy, dz = float(pat["dx"]), float(pat["dy"]), float(pat["dz"])
        for i in range(n):
            xf = translation(
                node.loc[0] + i * dx, node.loc[1] + i * dy, node.loc[2] + i * dz
            )
            out.append(xf.compose(base_rot))
    else:  # pragma: no cover - parser guards the kind
        raise SceneError(f"unknown pattern kind {pat['kind']!r}")
    return out


def build_design(spec: SceneSpec) -> Design:
    """Build a live :class:`Design` from a :class:`SceneSpec`."""
    design = Design()
    per_component: dict[str, object] = {}

    for node in spec.nodes:
        prim = build_config(node.config)
        if node.pattern is not None:
            node_expr: object = design.pattern(
                node.name, prim, _pattern_transforms(node)
            )
        else:
            node_expr = design.prim(node.name, prim, _node_xform(node.loc, node.rot))

        cur = per_component.get(node.component)
        if cur is None:
            per_component[node.component] = node_expr
        elif node.op == "add":
            per_component[node.component] = design.merge(cur, node_expr)  # type: ignore[arg-type]
        elif node.op == "cut":
            per_component[node.component] = design.subtract(cur, node_expr)  # type: ignore[arg-type]
        elif node.op == "intersect":
            per_component[node.component] = design.intersect(cur, node_expr)  # type: ignore[arg-type]
        else:  # pragma: no cover - parser guards op
            raise SceneError(f"unknown op {node.op!r}")

    for comp in spec.components:
        if comp in per_component:
            design.add_component(comp, per_component[comp])  # type: ignore[arg-type]
    return design
