"""The ratsnest + crossing count (ADR 0042 §8.1, §9) — the pre-routing
objective the placer minimizes ("minimize crossed wires").

A *ratsnest* is the set of straight pin-to-pin airwires for every net not yet
routed. v1 works at **component granularity**: a pin's position is its
instance's placement (centroid). When real footprint pad offsets land
(Slice 2) the same machinery refines to true pin positions.

Per net we build a **minimum spanning tree** over its placed members (the
standard shortest ratsnest), then count **genuine crossings** between airwires
of *different* nets. **Plane nets (gnd / power) are excluded** — they drop to
the plane through vias, not point-to-point airwires (ADR 0042 §10, the §8.1
derivation rule), so counting them as a star of crossings would be noise.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.pcb.geom import Point, bbox, bboxes_disjoint, dist, segments_cross

#: Net classes that route to a plane, not as airwires — excluded from the
#: ratsnest + crossing metric (the netlist still models every connection).
PLANE_CLASSES = frozenset({"gnd", "ground", "power", "pwr", "plane"})


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


def _mst_edges(members: list[tuple[str, Point]]) -> list[tuple[str, str, Point, Point]]:
    """Prim's MST over placed members (small N → O(N²) is fine)."""
    if len(members) < 2:
        return []
    in_tree = {0}
    edges: list[tuple[str, str, Point, Point]] = []
    while len(in_tree) < len(members):
        best: tuple[float, int, int] | None = None
        for i in in_tree:
            for j in range(len(members)):
                if j in in_tree:
                    continue
                d = dist(members[i][1], members[j][1])
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
) -> list[Airwire]:
    """Airwires (MST per signal net) over placed instances.

    ``instances`` maps refdes → (x, y) for *placed* parts only. ``nets`` is a
    list of ``{name, net_class, members:[{refdes,...}]}``. Plane-class nets and
    nets with <2 placed members contribute nothing.
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
        for a, b, p1, p2 in _mst_edges(members):
            out.append(Airwire(net=net["name"], a=a, b=b, p1=p1, p2=p2))
    return out


def crossings(airwires: list[Airwire]) -> list[tuple[Airwire, Airwire]]:
    """Genuine crossings between airwires of *different* nets.

    O(N²) with an AABB pre-filter (ADR 0042 §12). Same-net wires never count
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
