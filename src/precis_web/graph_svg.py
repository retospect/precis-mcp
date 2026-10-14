"""Radial-rings SVG projection of a :meth:`Store.neighbourhood` depth-2
result — the picture behind ``GET /graph/<kind>/<id>``
(``docs/backlog/web-graph-navigation.md`` slice 3).

Layout is computed in Python, not a force layout or a JS library: the
focus sits at the centre, its hop-1 neighbours on the first ring evenly
spaced in store order (newest links first — a stable order across
refocuses), its hop-2 neighbours on the second ring clustered inside
their hop-1 anchor's sector. Kinds are node *shapes* (rect for the
evidence kinds, diamond for findings, hexagon for quests, ellipse for
memories, rounded rect for convs, circle for the rest); relations are
edge labels; stored direction is the arrowhead. Neighbourhood rows past
the node cap collapse to count chips on the ring they would have filled
— a hub reads "+34 papers", never a missing tail.

Everything interpolated into the markup is either a formatted number or
:func:`html.escape`d text — the sanitizer precedent
(``routes/mermaid.py`` → ``precis.figure.svg.sanitize_svg``) is for
model-authored diagrams, and it would strip the ``<a href>`` node links
this page exists for. The trust boundary here is the escaping; the route
inlines the result verbatim.

The public surface is one function, :func:`focus_graph_svg`, which takes
the store's neighbourhood dict and returns a complete ``<svg>`` string.
"""

from __future__ import annotations

import html
import math
from typing import Any

from precis.utils import handle_registry

#: Kind → node shape. Anything unlisted is a circle — the generic ref.
KIND_SHAPES: dict[str, str] = {
    # citable evidence docs
    "paper": "rect",
    "patent": "rect",
    "datasheet": "rect",
    "edgar": "rect",
    "cfp": "rect",
    "finding": "diamond",
    "quest": "hex",
    "memory": "ellipse",
    "conv": "roundrect",
    "todo": "square",
    "job": "square",
    "draft": "roundrect",
    "plan": "roundrect",
}

_NODE_R = 13.0
_FOCUS_R = 20.0
#: Radial pixel budget per node — tight enough to keep the canvas small,
#: wide enough that 10px labels rarely collide on the same ring.
_NODE_ARC_PX = 56.0
#: A hop-2 child costs more arc than a ring-1 node — its label opens
#: sideways into the fan, so it needs label-width spacing, not just
#: node-width.
_HOP2_ARC_PX = 78.0
_LABEL_CHARS = 22
_LABEL_OUT_PX = 20.0

#: Colours (slate palette, matching the app's tailwind register).
_INK = "#0f172a"
_NODE_FILL = "#ffffff"
_NODE_STROKE = "#475569"
_HOP2_FILL = "#f1f5f9"
_HOP2_STROKE = "#94a3b8"
_EDGE = "#94a3b8"
_EDGE_LABEL = "#64748b"
_CHIP_FILL = "#e2e8f0"


def _handle(kind: str, ref_id: int) -> str:
    """The neighbourhood's own handle rule (``_links_ops._h``)."""
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _fmt(v: float) -> str:
    return f"{v:.1f}"


def _truncate(label: str) -> str:
    label = " ".join(label.split())
    return label if len(label) <= _LABEL_CHARS else label[: _LABEL_CHARS - 1] + "…"


def _node_shape(kind: str, r: float, fill: str, stroke: str, live: bool) -> str:
    dash = ' stroke-dasharray="3 3"' if not live else ""
    shape = KIND_SHAPES.get(kind, "circle")
    if shape == "rect":
        return (
            f'<rect x="{_fmt(-r)}" y="{_fmt(-r * 0.72)}" width="{_fmt(2 * r)}" '
            f'height="{_fmt(1.44 * r)}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="1.4"{dash}/>'
        )
    if shape == "square":
        s = r * 0.82
        return (
            f'<rect x="{_fmt(-s)}" y="{_fmt(-s)}" width="{_fmt(2 * s)}" '
            f'height="{_fmt(2 * s)}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="1.4"{dash}/>'
        )
    if shape == "diamond":
        pts = f"0,{_fmt(-r)} {_fmt(r)},{_fmt(0)} 0,{_fmt(r)} {_fmt(-r)},{_fmt(0)}"
        return (
            f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="1.4"{dash}/>'
        )
    if shape == "hex":
        pts = " ".join(
            f"{_fmt(r * math.cos(a))},{_fmt(r * math.sin(a))}"
            for a in (math.radians(60 * i - 90) for i in range(6))
        )
        return (
            f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="1.4"{dash}/>'
        )
    if shape == "ellipse":
        return (
            f'<ellipse rx="{_fmt(r * 1.2)}" ry="{_fmt(r * 0.8)}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.4"{dash}/>'
        )
    if shape == "roundrect":
        return (
            f'<rect x="{_fmt(-r)}" y="{_fmt(-r * 0.72)}" width="{_fmt(2 * r)}" '
            f'height="{_fmt(1.44 * r)}" rx="{_fmt(r * 0.4)}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.4"{dash}/>'
        )
    return (
        f'<circle r="{_fmt(r)}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="1.4"{dash}/>'
    )


def _chip(x: float, y: float, text: str) -> str:
    """One count chip — a grey pill standing in for nodes past the cap."""
    w = 7.0 * len(text) + 14.0
    return (
        f'<g transform="translate({_fmt(x)},{_fmt(y)})">'
        f'<rect x="{_fmt(-w / 2)}" y="-9" width="{_fmt(w)}" height="18" rx="9" '
        f'fill="{_CHIP_FILL}" stroke="{_HOP2_STROKE}" stroke-width="1"/>'
        f'<text y="3.2" text-anchor="middle" font-size="9.5" '
        f'font-family="ui-monospace, monospace" fill="{_EDGE_LABEL}">'
        f"{_esc(text)}</text></g>"
    )


def _edge_geometry(
    src: tuple[float, float],
    dst: tuple[float, float],
    offset: float,
    node_r: float,
) -> tuple[str, tuple[float, float], tuple[float, float]]:
    """Path ``d`` for one edge plus its unit direction and arrow tip.

    A single edge between a pair is a straight spoke; parallel edges fan
    out into quadratic Beziers offset to alternating sides so each keeps
    its own line. The arrow tip sits ``node_r`` short of the destination
    centre so the marker lands on the node outline. The label position is
    the caller's — it wants the edge's *outer* (larger-radius) end, not
    the midpoint, so rel labels don't stack where spokes converge.
    """
    sx, sy = src
    dx, dy = dst
    ux, uy = dx - sx, dy - sy
    dist = math.hypot(ux, uy) or 1.0
    ux, uy = ux / dist, uy / dist
    tip_x, tip_y = dx - ux * node_r, dy - uy * node_r
    if offset == 0.0:
        d = f"M {_fmt(sx)},{_fmt(sy)} L {_fmt(tip_x)},{_fmt(tip_y)}"
    else:
        mx, my = (sx + dx) / 2.0, (sy + dy) / 2.0
        cx, cy = mx - uy * offset * 2.0, my + ux * offset * 2.0
        d = f"M {_fmt(sx)},{_fmt(sy)} Q {_fmt(cx)},{_fmt(cy)} {_fmt(tip_x)},{_fmt(tip_y)}"
    return d, (ux, uy), (tip_x, tip_y)


def focus_graph_svg(data: dict[str, Any]) -> str:
    """The 2-hop neighbourhood as a radial-rings ``<svg>`` string.

    ``data`` is a :meth:`Store.neighbourhood` result at ``depth=2``:
    ``focus`` at the centre, hop-1 nodes (no ``hop`` key) on ring 1,
    ``hop: 2`` nodes on ring 2 clustered under their first anchor's
    sector, every edge a labelled arrow in stored direction. Nodes link
    to their own ``/graph/<kind>/<id>`` focus page. ``truncated`` (hop-1
    over the cap) and a counts-only ``counts2`` (hop-2 over the cap)
    render as count chips on the ring they would have filled.
    """
    focus = data["focus"]
    focus_h = _handle(focus["kind"], focus["id"])
    hop1 = [n for n in data["nodes"] if not n.get("hop")]
    hop2 = [n for n in data["nodes"] if n.get("hop") == 2]
    edges = data["edges"]

    # Count chips: a hop-1 overflow pill (edges counted but over the cap)
    # and, when hop 2 came back counts-only, one pill per hop-2 kind.
    chips1: list[str] = []
    if data.get("truncated"):
        total1 = sum(n for kinds in data["counts"].values() for n in kinds.values())
        shown1 = sum(1 for e in edges if not e.get("hop"))
        chips1.append(f"+{total1 - shown1} more")
    chips2: list[str] = []
    if not hop2:
        by_kind: dict[str, int] = {}
        for kinds in (data.get("counts2") or {}).values():
            for k, n in kinds.items():
                by_kind[k] = by_kind.get(k, 0) + n
        for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            chips2.append(f"+{n} {k}")

    # Ring radii grow with occupancy so neighbours don't touch; hop-2
    # chips share ring 2's slots, hop-1 chips share ring 1's.
    n1 = len(hop1) + len(chips1)
    n2 = len(hop2) + len(chips2)
    r1 = max(190.0, math.ceil(n1 * _NODE_ARC_PX / (2.0 * math.pi)))
    r2 = max(r1 + 175.0, math.ceil(n2 * _NODE_ARC_PX / (2.0 * math.pi)))
    margin = 130.0
    side = 2.0 * (r2 + margin)
    cx = cy = side / 2.0

    pos: dict[str, tuple[float, float]] = {focus_h: (cx, cy)}
    ring_of: dict[str, float] = {focus_h: 0.0}
    angle: dict[str, float] = {}
    #: Per-node label stagger (sibling index within a ring slot) so
    #: adjacent labels alternate heights instead of printing on top of
    #: each other; ``slot`` is the raw sibling index, used to ladder
    #: hop-2 edge labels across a fan.
    stagger: dict[str, int] = {}
    slot: dict[str, int] = {}
    for i, n in enumerate(hop1):
        theta = -math.pi / 2 + i * 2.0 * math.pi / max(n1, 1)
        h = _handle(n["kind"], n["id"])
        angle[h] = theta
        ring_of[h] = r1
        stagger[h] = i % 2
        slot[h] = i
        pos[h] = (cx + r1 * math.cos(theta), cy + r1 * math.sin(theta))

    # Hop 2: each node slots inside its anchor's ring-1 sector (the first
    # anchor its edges name; multi-anchor nodes still draw every edge).
    # The sector widens with the child count — several children crammed
    # into one ring-1 slot's arc overlap nodes and labels (checked by
    # eye); neighbours' sectors may then collide, which the edges still
    # disambiguate.
    anchor_of: dict[str, str] = {}
    kids: dict[str, list[str]] = {}
    for e in edges:
        if e.get("hop") != 2:
            continue
        anchor = e["src"] if e["dir"] == "out" else e["dst"]
        far = e["dst"] if e["dir"] == "out" else e["src"]
        if far not in anchor_of:
            anchor_of[far] = anchor
            kids.setdefault(anchor, []).append(far)
    for h, anchor in anchor_of.items():
        sibs = kids.get(anchor, [])
        try:
            j = sibs.index(h)
        except ValueError:
            j = 0
        sector = max(
            2.0 * math.pi / max(n1, 1) * 0.86,
            len(sibs) * _HOP2_ARC_PX / r2,
        )
        theta = angle.get(anchor, -math.pi / 2) + sector * (
            (j + 0.5) / max(len(sibs), 1) - 0.5
        )
        angle[h] = theta
        ring_of[h] = r2
        stagger[h] = j % 3
        slot[h] = j
        pos[h] = (cx + r2 * math.cos(theta), cy + r2 * math.sin(theta))

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_fmt(side)} {_fmt(side)}" '
        'role="img" aria-label="link neighbourhood" '
        'font-family="ui-sans-serif, system-ui, sans-serif">',
        '<defs><marker id="gah" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 1 L 9 5 L 0 9 z" fill="{_EDGE}"/></marker></defs>',
        # faint ring guides so "two hops" reads at a glance
        f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r1)}" fill="none" '
        f'stroke="#e2e8f0" stroke-width="1" stroke-dasharray="2 5"/>',
        f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r2)}" fill="none" '
        f'stroke="#e2e8f0" stroke-width="1" stroke-dasharray="2 5"/>',
    ]

    # Edges (below nodes): straight spoke for a lone edge between a pair,
    # fanned Beziers for parallel ones. Rel labels sit ~30% in from the
    # edge's OUTER end — at the spoke midpoints they stack on the focus,
    # and in a hop-2 fan they overlap where the children converge.
    pair_count: dict[tuple[str, str], int] = {}
    for e in edges:
        key = (min(e["src"], e["dst"]), max(e["src"], e["dst"]))
        pair_count[key] = pair_count.get(key, 0) + 1
    pair_seen: dict[tuple[str, str], int] = {}
    for e in edges:
        src, dst = pos.get(e["src"]), pos.get(e["dst"])
        if src is None or dst is None:
            continue
        key = (min(e["src"], e["dst"]), max(e["src"], e["dst"]))
        idx = pair_seen.get(key, 0)
        pair_seen[key] = idx + 1
        count = pair_count[key]
        offset = 0.0 if count == 1 else (idx - (count - 1) / 2.0) * 14.0
        dst_r = _FOCUS_R if e["dst"] == focus_h else _NODE_R
        d, (ux, uy), _tip = _edge_geometry(src, dst, offset, dst_r + 5.0)
        out.append(
            f'<path d="{d}" fill="none" stroke="{_EDGE}" stroke-width="1.1" '
            'marker-end="url(#gah)"/>'
        )
        outer_h = (
            e["dst"]
            if ring_of.get(e["dst"], 0.0) >= ring_of.get(e["src"], 0.0)
            else e["src"]
        )
        outer, inner = (dst, src) if outer_h == e["dst"] else (src, dst)
        px, py = -uy, ux
        if e.get("hop") == 2:
            # a fan's edge labels alternate sides and ladder out — same
            # side at a fixed offset stacks them on the anchor's spoke
            j = slot.get(outer_h, 0)
            perp = (10.0 + (j // 2) * 12.0) * (-1.0 if j % 2 else 1.0)
        else:
            perp = 10.0 + idx * 9.0
        lx = outer[0] + (inner[0] - outer[0]) * 0.30 + px * perp
        ly = outer[1] + (inner[1] - outer[1]) * 0.30 + py * perp
        out.append(
            f'<text x="{_fmt(lx)}" y="{_fmt(ly)}" text-anchor="middle" '
            f'font-size="8.5" fill="{_EDGE_LABEL}" '
            f'paint-order="stroke" stroke="#ffffff" stroke-width="3">'
            f"{_esc(e['rel'])}</text>"
        )

    # Nodes: shape by kind, click re-focuses on that node's /graph page.
    def emit_node(
        n: dict[str, Any],
        h: str,
        r: float,
        fill: str,
        stroke: str,
        anchor_delta: float | None = None,
    ) -> None:
        x, y = pos[h]
        theta = angle.get(h, -math.pi / 2)
        lx = x + (r + _LABEL_OUT_PX * 0.6) * math.cos(theta)
        ly = y + (r + _LABEL_OUT_PX * 0.6) * math.sin(theta) + stagger.get(h, 0) * 13.0
        if anchor_delta is not None and abs(anchor_delta) > 0.05:
            # hop-2 child: the label opens AWAY from the anchor, not
            # middle-anchored on the sibling beside it; the innermost
            # children (|delta| ~ 0) keep the middle anchor — the fan is
            # wide enough there.
            anchor = "start" if anchor_delta > 0 else "end"
        else:
            anchor = (
                "middle"
                if abs(math.cos(theta)) < 0.25
                else ("start" if math.cos(theta) > 0 else "end")
            )
        live = (n.get("state") or "live") == "live"
        label = _truncate(n.get("label") or h)
        url = f"/graph/{n['kind']}/{n['id']}"
        out.append(
            f'<a href="{_esc(url)}"><g transform="translate({_fmt(x)},{_fmt(y)})">'
            f"<title>{_esc(n.get('label') or h)} — {_esc(n['kind'])}</title>"
            + _node_shape(n["kind"], r, fill, stroke, live)
            + "</g></a>"
        )
        out.append(
            f'<a href="{_esc(url)}"><text x="{_fmt(lx)}" y="{_fmt(ly + 3)}" '
            f'text-anchor="{anchor}" font-size="10" fill="{_INK}">'
            f"{_esc(label)}</text></a>"
        )
        out.append(
            f'<text x="{_fmt(lx)}" y="{_fmt(ly + 13)}" text-anchor="{anchor}" '
            f'font-size="8" font-family="ui-monospace, monospace" '
            f'fill="{_EDGE_LABEL}">{_esc(n["kind"])} · {_esc(h)}</text>'
        )

    for n in hop1:
        emit_node(n, _handle(n["kind"], n["id"]), _NODE_R, _NODE_FILL, _NODE_STROKE)
    for n in hop2:
        h = _handle(n["kind"], n["id"])
        emit_node(
            n,
            h,
            _NODE_R,
            _HOP2_FILL,
            _HOP2_STROKE,
            anchor_delta=angle[h] - angle.get(anchor_of.get(h, ""), angle[h]),
        )

    # Count chips take the trailing ring slots the hidden nodes would have.
    for i, text in enumerate(chips1):
        theta = -math.pi / 2 + (len(hop1) + i) * 2.0 * math.pi / max(n1, 1)
        out.append(_chip(cx + r1 * math.cos(theta), cy + r1 * math.sin(theta), text))
    for i, text in enumerate(chips2):
        theta = -math.pi / 2 + (len(hop2) + i) * 2.0 * math.pi / max(n2, 1)
        out.append(_chip(cx + r2 * math.cos(theta), cy + r2 * math.sin(theta), text))

    # Focus, on top: a solid disc with its label under it.
    out.append(
        f'<g transform="translate({_fmt(cx)},{_fmt(cy)})">'
        f"<title>{_esc(focus.get('label') or focus_h)} — focus</title>"
        + _node_shape(focus["kind"], _FOCUS_R, _INK, _INK, True)
        + "</g>"
    )
    out.append(
        f'<text x="{_fmt(cx)}" y="{_fmt(cy + _FOCUS_R + 14)}" text-anchor="middle" '
        f'font-size="11" font-weight="600" fill="{_INK}">'
        f"{_esc(_truncate(focus.get('label') or focus_h))}</text>"
    )
    out.append(
        f'<text x="{_fmt(cx)}" y="{_fmt(cy + _FOCUS_R + 25)}" text-anchor="middle" '
        f'font-size="8.5" font-family="ui-monospace, monospace" fill="{_EDGE_LABEL}">'
        f"{_esc(focus['kind'])} · {_esc(focus_h)}</text>"
    )
    out.append("</svg>")
    return "".join(out)
