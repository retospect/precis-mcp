"""Is each net's copper actually one piece?

**The question nothing else asks.** Clearance DRC asks whether two nets'
copper is too close. Trace-width and annular-ring DRC ask whether one
feature is big enough. `RealizeResult.unrouted` asks whether the search
returned a path. None of them asks whether the copper a net ended up with
is *connected* — and a board can satisfy every one of them while a net sits
in two electrically separate halves.

That is not hypothetical. Two defects of exactly this shape shipped in this
engine and were invisible to every check above:

* **Track ends short of a pad.** The maze search works in cells, so its
  endpoints were cell CENTRES, up to half a cell diagonal from the pad.
  120 of 162 ends stopped 0.04-0.06mm short. Inside a 0.4mm pad that
  happens to connect — by luck, not construction.
* **Branch ends short of a trunk.** The same error against a 0.1mm trace
  instead of a 0.4mm pad, where the luck runs out: 32 endpoints on the two
  highest-fanout nets lay on neither pad, via, nor any same-net copper.
  DRC read zero errors and ``unrouted`` was empty.

Both were found by rendering the board and measuring it. This module is the
version of that measurement that runs every time.

**What counts as connected.** Copper that overlaps. Each track segment is a
capsule (segment + half-width), each via a disk on every layer it spans,
each pad a disk on its own layer; two primitives on the same layer are
joined when their gap is ``<= 0``, and all primitives of one via are joined
to each other because a plated barrel *is* the connection between layers.
Union-find over that relation; a net is healthy when its primitives form
exactly one component.

The primitive alphabet and the gap arithmetic are deliberately the same
ones :mod:`precis.pcb.drc`'s O(n^2) reference oracle uses — connectivity
and clearance are the same geometry asked in opposite directions, and two
different notions of "touching" between them would be a defect generator of
its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.pcb.drc import _arc_points, _capsule_capsule_gap, _Prim, _via_layer_names
from precis.pcb.planes import point_in_pour

#: Copper closer than this is treated as touching. Pure float-noise slack:
#: the realizer emits coordinates that are meant to be identical (a via at
#: the point a trace ends on), and they arrive through resampling and
#: collinear-merging arithmetic. It is NOT a tolerance for "nearly
#: connected" — 1 nanometre cannot rescue a real gap, which is what makes
#: it safe to have at all.
TOUCH_EPS_MM = 1e-6


@dataclass(frozen=True, slots=True)
class NetIslands:
    """One net whose copper is in more than one piece."""

    net: str
    components: int
    #: One representative coordinate per component, so a human (or a
    #: renderer) can go look at the break rather than re-deriving it.
    witnesses: tuple[tuple[float, float, str], ...]


def _pad_primitives(model: dict[str, Any], start_group: int) -> list[_Prim]:
    """Pads as disks. Each pad is its OWN group: two pads of one part are
    not electrically joined just because they belong to the same footprint.
    A pad with no net is skipped — a mechanical land has nothing to be
    connected to."""
    prims: list[_Prim] = []
    for i, pad in enumerate(model.get("pads") or []):
        net = str(pad.get("net", ""))
        if not net:
            continue
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        # The inscribed disk, not the circumscribed one: over-stating a
        # pad's reach would report a broken net as healthy, which is the
        # failure this module exists to catch. Understating it can only
        # produce a false alarm, which a human sees.
        r = min(w, h) / 2.0
        prims.append(
            _Prim(
                (float(pad["x"]), float(pad["y"])),
                None,
                r,
                start_group + i,
                net,
                str(pad.get("layer", "")),
            )
        )
    return prims


def _copper_primitives_with_vias(
    model: dict[str, Any],
) -> tuple[list[_Prim], dict[int, list[int]]]:
    """Track/via primitives plus, per via group, the primitive indices it
    spans — a via barrel joins its own layers regardless of geometry, which
    is the one connection in the model that is not an overlap."""
    prims: list[_Prim] = []
    via_groups: dict[int, list[int]] = {}
    for idx, item in enumerate(model.get("copper") or []):
        ctype = item.get("ctype")
        net = str(item.get("net", ""))
        if ctype == "track":
            r = float(item.get("width_mm", 0.0)) / 2.0
            layer = str(item.get("layer", ""))
            for seg in item.get("segments") or []:
                if seg.get("shape") == "arc":
                    pts = _arc_points(seg)
                    prims.extend(
                        _Prim(pts[k], pts[k + 1], r, idx, net, layer)
                        for k in range(len(pts) - 1)
                    )
                else:
                    a = (float(seg["start"][0]), float(seg["start"][1]))
                    b = (float(seg["end"][0]), float(seg["end"][1]))
                    prims.append(_Prim(a, b, r, idx, net, layer))
        elif ctype == "via":
            r = float(item.get("dia_mm", 0.0)) / 2.0
            x, y = float(item["x"]), float(item["y"])
            for layer in _via_layer_names(item, list(model.get("layers") or [])):
                via_groups.setdefault(idx, []).append(len(prims))
                prims.append(_Prim((x, y), None, r, idx, net, layer))
    return prims, via_groups


class _DisjointSet:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, a: int) -> int:
        root = a
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[a] != root:  # path compression
            self._parent[a], a = root, self._parent[a]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def net_islands(model: dict[str, Any]) -> list[NetIslands]:
    """Every net whose copper is in more than one connected component.

    A net with no copper at all is not reported here — that is the
    ``unrouted`` question, and answering it from this side would report the
    same defect twice with two different names.
    """
    prims, via_groups = _copper_primitives_with_vias(model)
    prims += _pad_primitives(model, start_group=len(model.get("copper") or []))
    if not prims:
        return []

    dsu = _DisjointSet(len(prims))
    # A via barrel connects its own layers. Nothing about the geometry says
    # so — two disks at the same (x, y) on different layers do not overlap
    # in any planar sense — so it is asserted from the model.
    for members in via_groups.values():
        for other in members[1:]:
            dsu.union(members[0], other)

    # A pour is a polygon, not a capsule, so it joins the union-find by
    # containment rather than by gap. Only same-net, same-layer primitives
    # are tested: a pour that overlapped a foreign net would be a clearance
    # violation, which is the other module's question.
    pours = [
        item for item in (model.get("copper") or []) if item.get("ctype") == "pour"
    ]
    for pour in pours:
        net, layer = str(pour.get("net", "")), str(pour.get("layer", ""))
        members = [
            i
            for i, p in enumerate(prims)
            if p.net == net and p.layer == layer and point_in_pour(pour, p.a[0], p.a[1])
        ]
        for other in members[1:]:
            dsu.union(members[0], other)

    by_key: dict[tuple[str, str], list[int]] = {}
    for i, p in enumerate(prims):
        by_key.setdefault((p.net, p.layer), []).append(i)
    # O(n^2) within one net+layer bucket, which is small; the whole-board
    # quadratic that would matter is never formed. No spatial index, for
    # the same reason drc.py's oracle has none: this is the check that
    # catches the accelerated path being wrong, so it must not share its
    # machinery.
    for members in by_key.values():
        for a_i in range(len(members)):
            pa = prims[members[a_i]]
            for b_i in range(a_i + 1, len(members)):
                pb = prims[members[b_i]]
                if _capsule_capsule_gap(pa, pb) <= TOUCH_EPS_MM:
                    dsu.union(members[a_i], members[b_i])

    per_net: dict[str, dict[int, tuple[float, float, str]]] = {}
    for i, p in enumerate(prims):
        per_net.setdefault(p.net, {}).setdefault(dsu.find(i), (p.a[0], p.a[1], p.layer))

    out = [
        NetIslands(net, len(roots), tuple(roots.values()))
        for net, roots in sorted(per_net.items())
        if len(roots) > 1
    ]
    return out


def connected_pin_pairs(model: dict[str, Any]) -> set[frozenset[tuple[str, str]]]:
    """Every pair of ``(refdes, pin)`` pads ``model["pads"]`` already joins
    through ``model["copper"]`` — the same union-find :func:`net_islands`
    runs, restated at PIN granularity for a caller asking a different
    question: not "is this net whole" but "are these two SPECIFIC pins
    already one piece of copper, whatever else is going on with the rest
    of their net". :mod:`precis.pcb.realize`'s fixed-copper short-circuit
    (pcb-pre-place-route-blocks Slice 1, "Realize seam") is the intended
    caller: a ratsnest segment whose two pins land in one returned pair
    needs no route, because authored fabric already carries the
    connection.

    Reuses this module's own primitive/gap machinery
    (:func:`_copper_primitives_with_vias`, :func:`_DisjointSet`, the same
    pour-containment and capsule-gap passes :func:`net_islands` runs) — a
    second connectivity reasoning path here would be exactly the
    producer/checker drift this module's docstring warns
    :mod:`precis.pcb.realize` itself away from; this function differs from
    :func:`net_islands` only in what it reads back out of the same
    union-find, never in how the union-find is built.

    A pad with no ``net``, or missing ``refdes``/``pin`` identity (the
    mounting-hole rings :func:`~precis.pcb.realize.pads_for_ir` emits
    carry neither), contributes no pair — same "nothing to check against"
    convention :func:`_pad_primitives` already applies to a netless pad."""
    prims, via_groups = _copper_primitives_with_vias(model)
    pad_offset = len(prims)
    pad_keys: list[tuple[str, str] | None] = []
    for pad in model.get("pads") or []:
        net = str(pad.get("net", ""))
        if not net:
            continue
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        r = min(w, h) / 2.0
        prims.append(
            _Prim(
                (float(pad["x"]), float(pad["y"])),
                None,
                r,
                pad_offset + len(pad_keys),
                net,
                str(pad.get("layer", "")),
            )
        )
        refdes, pin = pad.get("refdes"), pad.get("pin")
        pad_keys.append(
            (str(refdes), str(pin)) if refdes is not None and pin is not None else None
        )
    if not prims:
        return set()

    dsu = _DisjointSet(len(prims))
    for members in via_groups.values():
        for other in members[1:]:
            dsu.union(members[0], other)

    pours = [
        item for item in (model.get("copper") or []) if item.get("ctype") == "pour"
    ]
    for pour in pours:
        net, layer = str(pour.get("net", "")), str(pour.get("layer", ""))
        members = [
            i
            for i, p in enumerate(prims)
            if p.net == net and p.layer == layer and point_in_pour(pour, p.a[0], p.a[1])
        ]
        for other in members[1:]:
            dsu.union(members[0], other)

    by_key: dict[tuple[str, str], list[int]] = {}
    for i, p in enumerate(prims):
        by_key.setdefault((p.net, p.layer), []).append(i)
    for members in by_key.values():
        for a_i in range(len(members)):
            pa = prims[members[a_i]]
            for b_i in range(a_i + 1, len(members)):
                pb = prims[members[b_i]]
                if _capsule_capsule_gap(pa, pb) <= TOUCH_EPS_MM:
                    dsu.union(members[a_i], members[b_i])

    by_root: dict[int, list[tuple[str, str]]] = {}
    for offset, key in enumerate(pad_keys):
        if key is None:
            continue
        by_root.setdefault(dsu.find(pad_offset + offset), []).append(key)

    pairs: set[frozenset[tuple[str, str]]] = set()
    for keys in by_root.values():
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pairs.add(frozenset((keys[i], keys[j])))
    return pairs


#: One terminal a router may start/end a search on: the exact board
#: coordinate, the stackup LAYER NAME it sits on, and what kind of fixed
#: copper it came from (``'via'`` or ``'track'``) — the ``kind`` carries no
#: weight in this module's own union-find, it exists purely so a caller
#: (:mod:`precis.pcb.realize`'s island-terminal seam) can report WHICH
#: piece of authored copper a route actually used, in the same
#: "fail/succeed legibly" spirit :class:`~precis.pcb.realize.UnroutedReason`
#: already applies to the router's failure side.
@dataclass(frozen=True, slots=True)
class FixedTerminal:
    point: tuple[float, float]
    layer: str
    kind: str


def fixed_copper_pin_terminals(
    model: dict[str, Any],
) -> dict[tuple[str, str], tuple[FixedTerminal, ...]]:
    """For every pad pin whose connectivity ISLAND (the same union-find
    :func:`connected_pin_pairs` builds, restricted here to ``model["copper"]``
    itself rather than only what it joins to another PIN) contains at least
    one piece of AUTHORED fixed copper, every terminal point that island's
    fixed copper offers: a via's centre on EACH layer it spans (one
    :class:`FixedTerminal` per layer — :func:`_copper_primitives_with_vias`
    already expands a via into one primitive per spanned layer, so this
    falls out for free), and a track's segment endpoints on its own layer.

    **The router's alternate start/target set, restated per pin.**
    :mod:`precis.pcb.realize`'s maze search normally has exactly one
    start/goal point per connection: the pin's own pad centre. A pin whose
    fixed-copper island reaches somewhere the pad-to-pad grid search cannot
    walk to (a dense field of foreign claims with no corridor, even though
    the AUTHORED copper physically bridges it) needs more candidates than
    that one point — this is where they come from. Purely geometric
    (the same capsule-gap touching test :func:`net_islands`/
    :func:`connected_pin_pairs` already run), so it answers correctly
    whether or not the router's OWN occupancy grid can currently walk the
    same path — the two questions are independent on purpose (this
    module's own docstring).

    A pin with no fixed copper in its island — the overwhelmingly common
    case, every board with no ``fixed_copper`` argument at all included —
    is simply absent from the returned mapping; no empty tuple is stored
    for the majority that gets nothing extra."""
    prims, via_groups = _copper_primitives_with_vias(model)
    n_fixed_prims = len(prims)
    if n_fixed_prims == 0:
        return {}
    pad_offset = n_fixed_prims
    pad_keys: list[tuple[str, str] | None] = []
    for pad in model.get("pads") or []:
        net = str(pad.get("net", ""))
        if not net:
            continue
        w = float(pad.get("w", 0.0))
        h = float(pad.get("h", w))
        r = min(w, h) / 2.0
        prims.append(
            _Prim(
                (float(pad["x"]), float(pad["y"])),
                None,
                r,
                pad_offset + len(pad_keys),
                net,
                str(pad.get("layer", "")),
            )
        )
        refdes, pin = pad.get("refdes"), pad.get("pin")
        pad_keys.append(
            (str(refdes), str(pin)) if refdes is not None and pin is not None else None
        )
    if not prims:
        return {}

    dsu = _DisjointSet(len(prims))
    for members in via_groups.values():
        for other in members[1:]:
            dsu.union(members[0], other)

    by_key: dict[tuple[str, str], list[int]] = {}
    for i, p in enumerate(prims):
        by_key.setdefault((p.net, p.layer), []).append(i)
    for members in by_key.values():
        for a_i in range(len(members)):
            pa = prims[members[a_i]]
            for b_i in range(a_i + 1, len(members)):
                pb = prims[members[b_i]]
                if _capsule_capsule_gap(pa, pb) <= TOUCH_EPS_MM:
                    dsu.union(members[a_i], members[b_i])

    fixed_by_root: dict[int, list[int]] = {}
    for i in range(n_fixed_prims):
        fixed_by_root.setdefault(dsu.find(i), []).append(i)
    kind_by_group = {
        idx: str(item.get("ctype"))
        for idx, item in enumerate(model.get("copper") or [])
    }

    out: dict[tuple[str, str], tuple[FixedTerminal, ...]] = {}
    for offset, key in enumerate(pad_keys):
        if key is None:
            continue
        fixed_members = fixed_by_root.get(dsu.find(pad_offset + offset))
        if not fixed_members:
            continue
        terminals: list[FixedTerminal] = []
        seen: set[tuple[float, float, str]] = set()
        for i in fixed_members:
            p = prims[i]
            kind = kind_by_group.get(p.group, "track")
            points = (p.a,) if p.b is None else (p.a, p.b)
            for point in points:
                sig = (round(point[0], 6), round(point[1], 6), p.layer)
                if sig in seen:
                    continue
                seen.add(sig)
                terminals.append(FixedTerminal(point, p.layer, kind))
        out[key] = tuple(terminals)
    return out


__all__ = [
    "TOUCH_EPS_MM",
    "FixedTerminal",
    "NetIslands",
    "connected_pin_pairs",
    "fixed_copper_pin_terminals",
    "net_islands",
]
