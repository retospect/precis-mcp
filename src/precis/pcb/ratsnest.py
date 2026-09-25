"""The ratsnest + crossing count — the pre-routing
objective the placer minimizes ("minimize crossed wires").

A *ratsnest* is the set of straight pin-to-pin airwires for every net not yet
routed. v1 works at **component granularity**: a pin's position is its
instance's placement (centroid). When real footprint pad offsets land
(Slice 2) the same machinery refines to true pin positions.

Per net we build a **minimum spanning tree** over its placed members (the
standard shortest ratsnest), then count **genuine crossings** between airwires
of *different* nets. **Plane nets (gnd / power) are excluded** — they drop to
the plane through vias, not point-to-point airwires (the §8.1
derivation rule), so counting them as a star of crossings would be noise.

**The MST is side-aware (gripe 449579).** A net whose members span both
board sides pays one via per top/bottom alternation along the tree's edges,
and pure 2D Euclidean distance has no term for that — a member sitting
geometrically *between* two same-side members gets threaded into the chain
even when a direct same-side edge plus one spur would route the identical
copper for half the vias. :func:`_mst_edges` takes an optional ``bottom``
set (refdes → mounted on the back) and adds :data:`MST_VIA_BIAS_MM` to an
edge's weight whenever its two endpoints disagree, so Prim's naturally
prefers staying on one side unless the side-crossing tree really is
enough shorter to be worth it. Callers with no side context (``bottom=
None``) get the exact pre-fix pure-Euclidean MST — see that function's
docstring for the caller-supplied-not-re-derived convention.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.pcb.geom import Point, bbox, bboxes_disjoint, dist, segments_cross

#: Net classes that route to a plane, not as airwires — excluded from the
#: ratsnest + crossing metric (the netlist still models every connection).
PLANE_CLASSES = frozenset({"gnd", "ground", "power", "pwr", "plane"})

#: Heuristic bias :func:`_mst_edges` adds to an edge's weight when its two
#: members sit on opposite board sides — gripe 449579. Deliberately its own
#: constant, NOT :data:`precis.pcb.maze.VIA_COST_MM`: that constant prices a
#: layer change at the maze ROUTER's own per-cell, sub-mm path-search
#: granularity, while this MST works at component-CENTROID granularity,
#: where inter-member distances routinely run 5-50+mm. Sharing one number
#: across those two length scales would make one of them wrong — here it
#: must comfortably dominate a *typical* same-side-vs-cross-side distance
#: delta (a few mm to a couple cm) so a side-alternating chain never reads
#: as cheaper than a same-side tree over a modest length difference, while
#: still losing to a same-side detour large enough to be a genuinely worse
#: trade than the via.
MST_VIA_BIAS_MM = 20.0


@dataclass(frozen=True, slots=True)
class Airwire:
    net: str
    a: str  # refdes
    b: str  # refdes
    p1: Point
    p2: Point

    @property
    def length(self) -> float:
        return dist(self.p1, self.p2)


def _mst_edges(
    members: list[tuple[str, Point]],
    *,
    bottom: frozenset[str] | None = None,
) -> list[tuple[str, str, Point, Point]]:
    """Prim's MST over placed members (small N → O(N²) is fine).

    ``bottom`` is the set of refdes mounted on the back of the board — the
    SAME ``pcb_instances.layer`` convention :func:`precis.pcb.padplace.
    is_bottom_instance` resolves (which is in turn what :func:`precis.pcb.
    ir.from_graph` reads to populate ``PcbIR.inst_bottom``, the same fact
    :func:`precis.pcb.realize._side_layer` the router itself uses ultimately
    keys off). This module never re-derives top/bottom itself — no second
    parse of the layer string — it only accepts the caller's pre-resolved
    set, gripe 449579's "don't invent a second side-resolution path".
    ``bottom=None`` (the default) disables the term entirely and reproduces
    the original pure-Euclidean MST byte-for-byte, so a caller with no side
    context (or a single-side net, where the term is a no-op regardless) is
    unaffected.

    **This weight function is no longer a metric** once ``bottom`` is given
    a non-empty set: ``weight(a, c)`` can exceed ``weight(a, b) +
    weight(b, c)`` (e.g. a same-side pair separated by a far same-side
    member still beats a near cross-side member once the via bias is
    added), so the triangle inequality does NOT hold. That is harmless for
    Prim's itself — it places no such requirement on its edge weights — but
    future code must not assume this MST approximates a Steiner tree or any
    other metric-space bound the way a plain-distance MST would.
    """
    if len(members) < 2:
        return []

    def _weight(i: int, j: int) -> float:
        d = dist(members[i][1], members[j][1])
        if bottom and ((members[i][0] in bottom) != (members[j][0] in bottom)):
            d += MST_VIA_BIAS_MM
        return d

    in_tree = {0}
    edges: list[tuple[str, str, Point, Point]] = []
    while len(in_tree) < len(members):
        best: tuple[float, int, int] | None = None
        for i in in_tree:
            for j in range(len(members)):
                if j in in_tree:
                    continue
                d = _weight(i, j)
                if best is None or d < best[0]:
                    best = (d, i, j)
        assert best is not None
        _d, i, j = best
        edges.append((members[i][0], members[j][0], members[i][1], members[j][1]))
        in_tree.add(j)
    return edges


def build_airwires(
    instances: dict[str, Point],
    nets: list[dict],
    *,
    plane_classes: frozenset[str] = PLANE_CLASSES,
    bottom: frozenset[str] | None = None,
) -> list[Airwire]:
    """Airwires (MST per signal net) over placed instances.

    ``instances`` maps refdes → (x, y) for *placed* parts only. ``nets`` is a
    list of ``{name, net_class, members:[{refdes,...}]}``. Plane-class nets and
    nets with <2 placed members contribute nothing.

    ``bottom`` is the optional refdes set of back-mounted instances, passed
    straight through to :func:`_mst_edges` (see its docstring) — omit it for
    the pre-449579 side-blind MST.
    """
    out: list[Airwire] = []
    for net in nets:
        if (net.get("net_class") or "").strip().lower() in plane_classes:
            continue
        seen: dict[str, Point] = {}
        for m in net.get("members") or []:
            rd = m["refdes"] if isinstance(m, dict) else m
            if rd in instances and rd not in seen:
                seen[rd] = instances[rd]
        members = list(seen.items())
        for a, b, p1, p2 in _mst_edges(members, bottom=bottom):
            out.append(Airwire(net=net["name"], a=a, b=b, p1=p1, p2=p2))
    return out


def crossings(airwires: list[Airwire]) -> list[tuple[Airwire, Airwire]]:
    """Genuine crossings between airwires of *different* nets.

    O(N²) with an AABB pre-filter. Same-net wires never count
    (they form a tree); shared-endpoint touches are excluded by
    :func:`precis.pcb.geom.segments_cross`.
    """
    boxes = [bbox(w.p1, w.p2) for w in airwires]
    out: list[tuple[Airwire, Airwire]] = []
    for i in range(len(airwires)):
        for j in range(i + 1, len(airwires)):
            if airwires[i].net == airwires[j].net:
                continue
            if bboxes_disjoint(boxes[i], boxes[j]):
                continue
            if segments_cross(
                airwires[i].p1, airwires[i].p2, airwires[j].p1, airwires[j].p2
            ):
                out.append((airwires[i], airwires[j]))
    return out


def total_length(airwires: list[Airwire]) -> float:
    return sum(w.length for w in airwires)
