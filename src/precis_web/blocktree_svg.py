"""SVG projection over a :mod:`precis.blocktree` design — the shared
render core behind the ``se``/``nm`` web readers (docs backing gripe
gr335242, items 1-3: envelope-union projection, part isolation,
stepped abstraction levels).

Kind-agnostic on purpose: both ``se`` and ``nm`` blocks are
:class:`~precis.blocktree.types.BlockNode` subclasses carrying the same
L1 triple (``envelope`` — the ``cad`` mini-DSL config string, ``pose``,
``rot``, world-frame per each kind's own ``validate.py`` v1 convention),
so one projector serves both; a caller supplies the domain's own
``effective_envelope`` (instance/array/catalog fallback already lives
there — this module never reaches for the DB or re-derives it).

**Projection.** Each block's envelope is built via the same
``cad.dsl``/``cad.tessellate`` pipeline the glTF viewer and STL/3MF
exporter use (:mod:`precis_web.routes.cad`'s own precedent — "the mesh
the browser sees comes from the *same* IR→tessellate pipeline ... so the
view can never drift from the geometry the probes reason about"): the
tessellated local-frame mesh is placed at the block's world pose
(``cad.vec.pose``), and every vertex is projected onto the chosen view
plane and reduced to its 2-D convex hull. Overlapping per-block polygons
read as a union once painted with one flat fill colour — no explicit 2-D
boolean union is computed (round-1 scope; an exact union is a nice-to-have,
not needed for legibility at design-review resolution). Concave envelopes
(``torus``) render as their hull, a known, honest simplification.

**Abstraction levels** (comment 4 on the gripe — a stepped, *discrete*
selector, never a continuous depth slider): each level fixes a tree-depth
cutoff measured from the render root(s); a block with children at or past
the cutoff collapses to one box — the union bounding rectangle of every
hidden descendant's own polygon, so the box never claims less extent than
what it hides. A genuine leaf (no children) always renders its true
envelope shape regardless of level — there is nothing to hide.

**Part isolation** (item 2) narrows the render roots to one named
subtree and recentres the view on it; the level cutoff then applies
*within* that subtree. Combining isolation with an independent level per
OTHER subtree (comment 4's "hub shown implemented while the rest stays at
envelope level") is not built this round — one level applies uniformly
to whatever is rendered.

**Force overlay.** ``axial`` members (se's tension-rung 1 mechanism
class) are drawn as coloured lines between their two endpoint blocks'
own posed points — se's own :mod:`precis_se.stability` already treats an
array node as one representative point at its own pose (validate.py:
"array members are not expanded"), so this module follows the same
convention rather than inventing per-member spoke geometry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from precis.blocktree.types import BlockNode, Connect, Tree
from precis.cad.tessellate import apply_rigid, mesh_config
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose

Axis = Literal["x", "y", "z"]
AXES: tuple[Axis, ...] = ("x", "y", "z")

#: The named abstraction ladder (comment 4 — semantic steps, not a raw
#: depth number). Order matters: index is the "how much more to reveal"
#: rank the UI stepper walks.
LEVELS: tuple[str, ...] = ("envelope", "interfaces", "refined", "realized")

#: Selectable fill-colour channels (comment 3).
COLOUR_CHANNELS: tuple[str, ...] = ("part", "force", "realization")

Point2 = tuple[float, float]

#: A block's own effective envelope, per the domain (``se``/``nm`` each
#: layer instance/array/catalog fallback on top of the shared
#: ``precis.blocktree.ops`` base — see the module docstring).
EffectiveEnvelopeFn = Callable[[Tree, BlockNode], "str | None"]


def _level_index(level: str) -> int:
    try:
        return LEVELS.index(level)
    except ValueError as exc:
        raise ValueError(
            f"unknown abstraction level {level!r} — one of {LEVELS}"
        ) from exc


def collapse_depth(level: str) -> int | None:
    """Tree-depth cutoff (from the render root) at which a block WITH
    children collapses to its envelope box. ``None`` = never collapse
    (the two deepest rungs, 'refined'/'realized', differ only in whether
    a leaf's realization state gets a distinct outline — both show every
    leaf)."""
    idx = _level_index(level)
    if idx == 0:  # envelope-only
        return 0
    if idx == 1:  # interfaces
        return 1
    return None


def project_point(p: Sequence[float], axis: Axis) -> Point2:
    """World ``(x, y, z)`` → the 2-D view-plane coordinate for ``axis``
    (the axis looked *along*): ``z`` → ``(x, y)`` (top), ``y`` → ``(x,
    z)`` (front), ``x`` → ``(y, z)`` (side). Kept as one function so
    every caller (block polygons, member-line endpoints, port markers)
    agrees on the same basis."""
    x, y, z = float(p[0]), float(p[1]), float(p[2])
    if axis == "z":
        return (x, y)
    if axis == "y":
        return (x, z)
    return (y, z)


def _cross(o: Point2, a: Point2, b: Point2) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points: Iterable[Point2]) -> list[Point2]:
    """Andrew's monotone chain — the minimal extreme-vertex hull (mirrors
    ``precis.pcb.escape._convex_hull``'s algorithm; this module keeps its
    own copy rather than reaching into a sibling domain's private
    helper)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def build(seq: list[Point2]) -> list[Point2]:
        hull: list[Point2] = []
        for p in seq:
            while len(hull) >= 2 and _cross(hull[-2], hull[-1], p) <= 0.0:
                hull.pop()
            hull.append(p)
        return hull

    lower = build(pts)
    upper = build(list(reversed(pts)))
    return lower[:-1] + upper[:-1]


def bbox_of(points: Iterable[Point2]) -> tuple[Point2, Point2] | None:
    """``(lo, hi)`` axis-aligned bounds, or ``None`` for an empty input."""
    us = [p[0] for p in points]
    vs = [p[1] for p in points]
    if not us:
        return None
    return (min(us), min(vs)), (max(us), max(vs))


def bbox_polygon(points: Iterable[Point2]) -> list[Point2] | None:
    """The 4-corner axis-aligned rectangle enclosing ``points`` — a
    collapsed subtree's "envelope box" (module docstring)."""
    box = bbox_of(points)
    if box is None:
        return None
    (lo_u, lo_v), (hi_u, hi_v) = box
    return [(lo_u, lo_v), (hi_u, lo_v), (hi_u, hi_v), (lo_u, hi_v)]


def envelope_polygon(
    envelope: str, pose: Sequence[float], rot: Sequence[float], axis: Axis
) -> list[Point2] | None:
    """The block's own posed envelope, projected + hulled — ``None`` on
    any failure to parse/tessellate (a chamfer-only or malformed stored
    envelope; honest absence, never a raised error, matching se/nm's own
    read-path convention of reporting absence rather than failing)."""
    try:
        verts, _tris = mesh_config(envelope)
    except ValueError:
        # covers cad.dsl.DslError (malformed config) and
        # cad.tessellate.TessellationError (chamfer — unbounded, no
        # finite mesh) — both subclass ValueError.
        return None
    xf = cad_pose(cad_as_vec3(list(pose)), cad_as_vec3(list(rot)))
    world = apply_rigid(xf, verts)
    pts = [project_point(p, axis) for p in world]
    hull = convex_hull(pts)
    return hull if len(hull) >= 3 else None


# ── tree traversal ──────────────────────────────────────────────────────


def children_map(tree: Tree[BlockNode, Connect]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, node in tree.blocks.items():
        if node.parent is not None:
            out.setdefault(node.parent, []).append(name)
    return out


def root_names(tree: Tree[BlockNode, Connect]) -> list[str]:
    return sorted(n for n, b in tree.blocks.items() if b.parent is None)


@dataclass
class VisiblePlan:
    """One render pass's traversal result over a (possibly isolated)
    subtree: which blocks show their true shape, which collapse to a
    box, and the render roots the traversal actually started from (for
    'part' colour grouping)."""

    shown: dict[str, Literal["shape", "box"]] = field(default_factory=dict)
    render_roots: list[str] = field(default_factory=list)


def plan_visibility(
    tree: Tree[BlockNode, Connect],
    kids: dict[str, list[str]],
    *,
    level: str,
    isolate: str | None,
) -> VisiblePlan:
    """Decide, for every reachable block, whether it draws its own shape
    or stands in as a collapsed box (module docstring's level ladder)."""
    if isolate is not None and isolate not in tree.blocks:
        raise KeyError(isolate)
    cutoff = collapse_depth(level)
    roots = [isolate] if isolate is not None else root_names(tree)
    shown: dict[str, Literal["shape", "box"]] = {}
    #: The render path runs BEFORE ``adapter.validate`` (which is where a
    #: stored parent-cycle would normally surface as a finding), so a
    #: cycle must not hang this walk — a block can only ever have one
    #: parent, so a non-cyclic tree visits each name exactly once; seeing
    #: a name twice means a cycle, and the second visit is dropped (mirrors
    #: :func:`group_of`'s own ``seen`` guard).
    visited: set[str] = set()

    def walk(name: str, depth: int) -> None:
        if name in visited:
            return
        visited.add(name)
        node_kids = sorted(kids.get(name, []))
        if not node_kids:
            shown[name] = "shape"
            return
        if cutoff is not None and depth >= cutoff:
            shown[name] = "box"
            return
        shown[name] = "shape"
        for k in node_kids:
            walk(k, depth + 1)

    for r in roots:
        walk(r, 0)
    return VisiblePlan(shown=shown, render_roots=roots)


def _descendants(name: str, kids: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = {name}
    stack = list(kids.get(name, []))
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        stack.extend(kids.get(n, []))
    return out


def group_of(tree: Tree[BlockNode, Connect], name: str, boundary: set[str]) -> str:
    """Walk ``name``'s ancestor chain up to (and including) the nearest
    member of ``boundary`` (a render pass's ``render_roots``) — the 'part'
    colour channel's grouping key, so isolating a subtree still shows the
    isolated subtree's OWN children as distinct colours rather than one
    flat blob."""
    cur = name
    seen = {cur}
    while cur not in boundary:
        parent = tree.blocks[cur].parent
        if parent is None or parent not in tree.blocks or parent in seen:
            break
        cur = parent
        seen.add(cur)
    return cur


# ── geometry assembly ────────────────────────────────────────────────────


@dataclass
class BlockDraw:
    name: str
    polygon: list[Point2]
    kind: Literal["shape", "box"]
    group: str
    is_realized: bool | None = None


def build_block_draws(
    tree: Tree[BlockNode, Connect],
    effective_envelope: EffectiveEnvelopeFn,
    kids: dict[str, list[str]],
    plan: VisiblePlan,
    axis: Axis,
    *,
    is_realized: Callable[[BlockNode], bool] | None = None,
) -> list[BlockDraw]:
    """One :class:`BlockDraw` per visible block — polygon in world-unit
    view-plane coordinates (not yet flipped/scaled for SVG)."""
    boundary = set(plan.render_roots)
    polygons: dict[str, list[Point2] | None] = {}

    def own_polygon(name: str) -> list[Point2] | None:
        if name in polygons:
            return polygons[name]
        node = tree.blocks[name]
        env = effective_envelope(tree, node)
        poly = envelope_polygon(env, node.pose, node.rot, axis) if env else None
        polygons[name] = poly
        return poly

    draws: list[BlockDraw] = []
    for name, kind in plan.shown.items():
        node = tree.blocks[name]
        if kind == "shape":
            poly = own_polygon(name)
            if poly is None:
                continue
            realized = is_realized(node) if is_realized is not None else None
            draws.append(
                BlockDraw(
                    name=name,
                    polygon=poly,
                    kind="shape",
                    group=group_of(tree, name, boundary),
                    is_realized=realized,
                )
            )
            continue
        # collapsed subtree: union of this block's own polygon (if any)
        # with every hidden descendant's own polygon, honestly covering
        # whatever extent is being hidden rather than only what the
        # collapsed node itself declares.
        pts: list[Point2] = list(own_polygon(name) or [])
        for desc in _descendants(name, kids):
            pts.extend(own_polygon(desc) or [])
        box = bbox_polygon(pts)
        if box is None:
            continue
        draws.append(
            BlockDraw(
                name=name, polygon=box, kind="box", group=group_of(tree, name, boundary)
            )
        )
    return draws


# ── colour ────────────────────────────────────────────────────────────────

#: Deterministic distinct swatch (mirrors ``precis.cad.gltf``'s palette
#: intent — a colour per identity, not a heatmap).
_PALETTE: tuple[str, ...] = (
    "2563eb",
    "16a34a",
    "d97706",
    "9333ea",
    "dc2626",
    "0891b2",
    "65a30d",
    "db2777",
    "4f46e5",
    "ea580c",
)


def part_colours(groups: Iterable[str]) -> dict[str, str]:
    names = sorted(set(groups))
    return {g: "#" + _PALETTE[i % len(_PALETTE)] for i, g in enumerate(names)}


#: Tension-positive role → fill/stroke colour (the 'force' channel and
#: the always-on axial-member overlay share this vocabulary).
_ROLE_COLOUR = {
    "tie": "#16a34a",  # tension — green
    "strut": "#dc2626",  # compression — red
}
_NEUTRAL_COLOUR = "#94a3b8"  # undeclared / no computed sign — slate


def force_colour(role: str, self_stress: float | None) -> str:
    if role in _ROLE_COLOUR:
        return _ROLE_COLOUR[role]
    if role == "rod" and self_stress is not None:
        return _ROLE_COLOUR["tie"] if self_stress >= 0.0 else _ROLE_COLOUR["strut"]
    return _NEUTRAL_COLOUR


_REALIZED_COLOUR = "#0f766e"
_ABSTRACT_COLOUR = "#94a3b8"


def realization_colour(is_realized: bool) -> str:
    return _REALIZED_COLOUR if is_realized else _ABSTRACT_COLOUR


def block_fill_colour(
    draw: BlockDraw, channel: str, part_colour_map: dict[str, str]
) -> str:
    """The per-block fill for the selected colour channel (comment 3).
    ``force`` mode leaves blocks a neutral tint — that channel's signal is
    the always-on axial-member overlay, not the envelope fill; ``part``
    and ``realization`` colour the envelope itself."""
    if channel == "part":
        return part_colour_map.get(draw.group, _NEUTRAL_COLOUR)
    if channel == "realization":
        if draw.is_realized is None:
            return _NEUTRAL_COLOUR
        return realization_colour(draw.is_realized)
    return _NEUTRAL_COLOUR


# ── honesty header ────────────────────────────────────────────────────────

Tier = Literal["ok", "warn", "error"]


def fill_fraction_line(
    tree: Tree[BlockNode, Connect], effective_envelope: EffectiveEnvelopeFn
) -> str:
    """The filled-fraction honesty line (mirrors se/nm handler's own
    ``_fill_fraction_line`` wording — kept as a small local copy rather
    than importing a handler-private helper across the package boundary;
    see the module docstring)."""
    ordinary = [n for n in tree.blocks.values() if n.template is None]
    if not ordinary:
        return "0/0 block(s) filled — no blocks declared yet (unfilled)"
    filled = [n for n in ordinary if effective_envelope(tree, n)]
    line = f"{len(filled)}/{len(ordinary)} block(s) have envelopes (L1 filled)"
    if not filled:
        line += " — UNFILLED scaffold: nothing renders yet"
    return line


def validator_summary(findings: Sequence[object]) -> tuple[str, Tier]:
    """``(line, tier)`` from a list of ``ValidationIssue``-shaped findings
    (duck-typed on ``.severity`` — se's and nm's ``validate()`` both
    return the same rule/subject/detail/severity shape independently)."""
    if not findings:
        return "no validator findings", "ok"
    n_error = sum(1 for f in findings if getattr(f, "severity", None) == "error")
    n_warn = sum(1 for f in findings if getattr(f, "severity", None) == "warn")
    tier: Tier = "error" if n_error else ("warn" if n_warn else "ok")
    return f"{n_error} error(s), {n_warn} warning(s)", tier


# ── member overlay (se's axial subgraph) ────────────────────────────────


@dataclass
class MemberLine:
    a: Point2
    b: Point2
    colour: str
    subject: str


# ── SVG emission ─────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MARGIN = 24.0
_HEADER_LINE_H = 18.0
_HEADER_PAD = 10.0
_TIER_BG = {"ok": "#ecfdf5", "warn": "#fffbeb", "error": "#fef2f2"}
_TIER_FG = {"ok": "#065f46", "warn": "#92400e", "error": "#991b1b"}


def render_svg(
    draws: Sequence[BlockDraw],
    members: Sequence[MemberLine],
    *,
    channel: str,
    header_lines: Sequence[str],
    tier: Tier,
    size: float = 720.0,
) -> str:
    """Assemble the SVG: a coloured honesty/verdict banner across the top
    (module docstring — "verdict + honesty header on top"), then every
    visible block's polygon, then the axial-member overlay on top of the
    blocks so a member line is never hidden under a fill."""
    part_map = part_colours(d.group for d in draws)
    all_pts: list[Point2] = [p for d in draws for p in d.polygon]
    all_pts += [m.a for m in members]
    all_pts += [m.b for m in members]
    box = bbox_of(all_pts)
    header_h = _HEADER_PAD * 2 + _HEADER_LINE_H * max(1, len(header_lines))
    if box is None:
        canvas_w = canvas_h = size
        scale = 1.0
        off_u = off_v = 0.0
    else:
        (lo_u, lo_v), (hi_u, hi_v) = box
        span_u = max(hi_u - lo_u, 1e-9)
        span_v = max(hi_v - lo_v, 1e-9)
        drawable = size - 2 * _MARGIN
        scale = drawable / max(span_u, span_v)
        canvas_w = span_u * scale + 2 * _MARGIN
        canvas_h = span_v * scale + 2 * _MARGIN
        off_u, off_v = lo_u, lo_v

    def to_svg(p: Point2) -> Point2:
        # world (u, v, v-up) -> SVG (x, y, y-down), offset by the header band.
        x = (p[0] - off_u) * scale + _MARGIN
        y = header_h + (canvas_h - _MARGIN) - (p[1] - off_v) * scale
        return (x, y)

    total_h = header_h + canvas_h
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas_w:.1f} '
        f'{total_h:.1f}" font-family="ui-monospace, monospace">',
        f"<title>{_esc(' | '.join(header_lines))}</title>",
        f'<rect x="0" y="0" width="{canvas_w:.1f}" height="{header_h:.1f}" '
        f'fill="{_TIER_BG[tier]}"/>',
    ]
    for i, line in enumerate(header_lines):
        y = _HEADER_PAD + (i + 1) * _HEADER_LINE_H - 4
        parts.append(
            f'<text x="{_HEADER_PAD:.1f}" y="{y:.1f}" font-size="12" '
            f'fill="{_TIER_FG[tier]}">{_esc(line)}</text>'
        )
    parts.append(
        f'<rect x="0" y="{header_h:.1f}" width="{canvas_w:.1f}" '
        f'height="{canvas_h:.1f}" fill="#f8fafc"/>'
    )
    for d in draws:
        fill = block_fill_colour(d, channel, part_map)
        pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in (to_svg(p) for p in d.polygon))
        dash = ' stroke-dasharray="6,4"' if d.kind == "box" else ""
        parts.append(
            f'<polygon points="{pts}" fill="{fill}" fill-opacity="0.55" '
            f'stroke="{fill}" stroke-width="1.5"{dash}>'
            f"<title>{_esc(d.name)}</title></polygon>"
        )
    for m in members:
        ax, ay = to_svg(m.a)
        bx, by = to_svg(m.b)
        parts.append(
            f'<line x1="{ax:.2f}" y1="{ay:.2f}" x2="{bx:.2f}" y2="{by:.2f}" '
            f'stroke="{m.colour}" stroke-width="3">'
            f"<title>{_esc(m.subject)}</title></line>"
        )
    parts.append("</svg>")
    return "\n".join(parts)
