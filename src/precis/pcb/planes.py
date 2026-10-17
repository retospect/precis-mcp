"""Copper pours for plane-assigned layers — the module the realizer has
been deferring to since it was written.

**Why this stopped being cosmetic.** ``realize_segment`` gives a
plane-promoted net a dog-bone STUB off each pad and stops there, on the
stated grounds that "the via-to-plane connection is ``planes.py``'s job, a
later module". Nothing then poured the plane, and nothing dropped a via
into it. So a promoted net's realized copper was: some pads, some
millimetre-long stubs, and no plane — a net that is not connected to
itself at all.

That is not a rendering complaint. It was invisible for as long as it was,
because every check the engine had asks about *proximity* (clearance,
width, annular ring, board edge) and a net with no copper passes all of
them comfortably. :func:`precis.pcb.connectivity.net_islands` found it on
its first run: on seed 2 the ``SDA`` net, promoted to ``In1.Cu``, came back
in five pieces.

**What a pour is here.** The board outline, inset by the edge clearance,
minus every *other* net's copper on that layer dilated by clearance —
including an antipad around each foreign via barrel that passes through.
The pour's own net's vias are deliberately not subtracted: that is the
connection. Thermal reliefs are not modelled (a solid connection is
electrically correct and thermally pessimistic for hand soldering, which
is the safe direction for a machine-assembled board).

The result can be disconnected or holed, and both are reported rather than
smoothed away: a pour that a foreign via array has cut in half is a real
electrical fact about the board, and merging the halves in the model would
be the same class of lie as a track that ends *near* its pad.

**The same-net rule, stated once, because getting it backwards is
plausible-looking either way.** Two nets sharing a layer split into
exactly two treatments and this module must never blur them:

- **same net as the pour → MERGE.** A GND trace/via on a GND-poured layer
  is not an obstacle the pour routes around — it is the SAME conductor,
  so it is excluded from ``blockers`` entirely (the ``continue`` on a
  matching ``net`` above) and the pour polygon simply covers it. Getting
  this backwards (subtracting your own net's copper) leaves the pour and
  its own traces/vias as disconnected islands that LOOK like a normal
  render — every proximity/clearance/width DRC check passes, because
  nothing about "this net has no copper touching itself" is a clearance
  violation. This was the exact defect the module docstring above
  describes finding on seed 2's SDA net, before any pour geometry existed
  at all.
- **foreign net → ANTIPAD.** Every OTHER net's copper is dilated by
  ``clearance_mm`` and subtracted, so a foreign via barrel through the
  plane gets a clean keep-out ring rather than shorting the plane to
  whatever that via carries. Getting THIS backwards (not subtracting
  foreign copper) also renders as a plausible-looking solid fill — it is
  a short, not a hole, so nothing about the pour's own shape looks wrong
  either.

Neither mistake is visible by inspecting the pour polygon alone — both
render as "a solid-looking fill". The check that can actually tell the
two apart is :func:`precis.pcb.connectivity.net_islands`: run against a
board with a routed net sharing a filled layer, it must report the
poured net as ONE connected island (proving the merge happened) while
DRC (:mod:`precis.pcb.drc`) independently proves no clearance violation
exists between the pour and the foreign net's copper (proving the
antipad happened) — see ``tests/test_pcb_planes.py``'s
``test_plane_pour_merges_same_net_copper_and_islands_foreign_net``
for exactly this pairing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# shapely ships no py.typed marker here — same suppression drc.py carries,
# same reason. Boolean geometry on real polygons is what this module is,
# and hand-rolling it would be a second, worse implementation of a
# dependency the project already takes.
from shapely import concave_hull
from shapely.geometry import MultiPoint, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from precis.pcb.drc import _copper_item_polygon, _via_layer_names

#: Shapely buffer resolution for round caps/joins. Matches drc.py's own
#: choice so a pour's boundary and the clearance check's idea of that
#: boundary are the same polygon, not two approximations of one.
_BUFFER_QUAD_SEGS = 16

#: A pour fragment smaller than this is dropped. It is an artifact of
#: buffering arithmetic (slivers along a cut), not copper a fab would
#: image, and keeping it would report a plane as "in 40 pieces" because of
#: 39 specks. Deliberately far below any real feature: a 0.05mm^2 island is
#: two orders of magnitude under the smallest pad on this board.
MIN_FRAGMENT_MM2 = 0.05


def _as_polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []


def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """``geom`` with any non-areal members of a collection dropped (an
    intersection of two polygons can come back as a collection carrying a
    touching edge or point)."""
    if isinstance(geom, Polygon | MultiPolygon):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return unary_union(parts) if parts else Polygon()


def _ring(coords: Any) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in coords]


def _pour_item(frag: Polygon, layer_name: str, net: str) -> dict[str, Any]:
    """One ``ctype='pour'`` dict for a single polygon fragment — the
    exterior ring plus any interior rings ``>= MIN_FRAGMENT_MM2``, exactly
    the shape :func:`plane_pours` has always emitted. Both :func:`plane_pours`
    (a fresh pour, cut against every foreign net's copper) and
    :func:`cut_antipads` (an already-emitted pour, cut a second time against
    a blocker) build their output dicts through this ONE function, so a
    caller two ``ctype='pour'`` items apart in the pipeline can never
    silently disagree on what ``polygon``/``holes`` mean."""
    item: dict[str, Any] = {
        "ctype": "pour",
        "layer": layer_name,
        "net": net,
        "polygon": _ring(frag.exterior.coords),
    }
    holes = [
        _ring(ring.coords)
        for ring in frag.interiors
        if Polygon(ring).area >= MIN_FRAGMENT_MM2
    ]
    if holes:
        item["holes"] = holes
    return item


#: Pour priority of a stackup ``fill`` (pcb-stackup-regions slice 3/4):
#: below every plane (default 0) and every derived region (priority = pin
#: count, so >= 2), so a fill takes only the area nothing else claims —
#: Reto's "any copper area with no assigned net region pours GND".
FILL_PRIORITY = -1


@dataclass(frozen=True, slots=True)
class PourSpec:
    """One net to pour on one layer (pcb-stackup-regions slice 4).

    ``region`` is the fill region (:func:`region_polygon` reads it), ``None``
    for the whole board. Higher ``priority`` pours first; a lower one is
    cleared around everything poured before it on the same layer, by
    ``clearance_mm`` (KiCad/TopoR zone semantics). Several nets share a layer
    when their regions leave each other room."""

    layer: int
    net: str
    region: Any = None
    priority: int = 0


def region_polygon(region: Any) -> Polygon | None:
    """A stored fill region as a shapely polygon, or ``None`` for the whole
    board. Accepts a bare ring (``[[x, y], ...]``) or ``{"points": ring,
    "holes": [ring, ...]}`` — the shapes ``pcb_planes.region`` carries."""
    if region is None:
        return None
    if isinstance(region, dict):
        ring = region.get("points") or []
        holes = region.get("holes") or []
    else:
        ring, holes = region, []
    if len(ring) < 3:
        return None
    poly = Polygon(
        [(float(p[0]), float(p[1])) for p in ring],
        [[(float(p[0]), float(p[1])) for p in h] for h in holes if len(h) >= 3],
    )
    if not poly.is_valid:
        fixed = poly.buffer(0)
        parts = _as_polygons(fixed)
        if not parts:
            return None
        poly = max(parts, key=lambda p: p.area)
    return poly


def region_json(poly: Polygon) -> dict[str, Any]:
    """The ``pcb_planes.region`` shape of a polygon (exterior + holes)."""
    out: dict[str, Any] = {"points": _ring(poly.exterior.coords)}
    holes = [_ring(r.coords) for r in poly.interiors]
    if holes:
        out["holes"] = holes
    return out


def plane_pours(
    *,
    outline: list[list[float]],
    layers: list[str],
    plane_nets: dict[int, str] | None = None,
    copper: list[dict[str, Any]],
    clearance_mm: float,
    edge_clearance_mm: float,
    specs: list[PourSpec] | None = None,
) -> list[dict[str, Any]]:
    """One ``ctype='pour'`` item per connected fragment of each plane.

    ``plane_nets`` maps a stackup layer INDEX to the net name poured on it
    — the caller resolves that from ``ir.net_plane_layers`` (a per-net
    bitmask now: one net may be the value for SEVERAL layer keys at once,
    since a net can be poured on more than one layer), because this
    module deliberately knows nothing about the IR (same boundary
    :mod:`precis.pcb.session` keeps). ``copper`` is the already-realized
    tracks and vias in :mod:`precis.pcb.gerber` model shape.

    ``specs`` (pcb-stackup-regions slice 4) is the general form: several
    nets per layer, each with a region and a priority (:class:`PourSpec`).
    Per layer, highest priority first (ties keep the order given): pour =
    region ∩ outline-inset − foreign copper − everything already poured on
    the layer, grown by ``clearance_mm``. Once a WHOLE-BOARD spec has poured
    on a layer, later specs there are skipped: it left them nothing but the
    rings around its own antipads, and a net that the board says is not
    poured must not come out as via-sized slivers. ``plane_nets`` is the
    one-whole-board-net-per-layer special case and pours byte-identically.

    Emitted items carry ``polygon`` (the exterior ring) and, when the pour
    has any, ``holes`` — a foreign via through a plane leaves a real hole,
    and an exterior-only pour would claim copper where an antipad is.
    """
    if specs is None:
        specs = [
            PourSpec(layer_idx, net)
            for layer_idx, net in sorted((plane_nets or {}).items())
        ]
    if not outline or len(outline) < 3 or not specs:
        return []
    board = Polygon([(float(p[0]), float(p[1])) for p in outline])
    if not board.is_valid:
        board = board.buffer(0)
    region0 = board.buffer(
        -abs(edge_clearance_mm), quad_segs=_BUFFER_QUAD_SEGS, join_style=2
    )
    if region0.is_empty:
        return []

    by_layer: dict[int, list[PourSpec]] = {}
    for spec in specs:
        if 0 <= spec.layer < len(layers):
            by_layer.setdefault(spec.layer, []).append(spec)

    out: list[dict[str, Any]] = []
    for layer_idx in sorted(by_layer):
        layer_name = layers[layer_idx]
        claimed: list[BaseGeometry] = []
        whole_board_done = False
        for spec in sorted(by_layer[layer_idx], key=lambda s: -s.priority):
            if whole_board_done:
                continue
            net = spec.net
            blockers: list[BaseGeometry] = []
            for item in copper:
                if str(item.get("net", "")) == net:
                    continue  # own-net copper is the connection, not an obstacle
                if item.get("ctype") == "via":
                    # A barrel is an obstacle on every layer it passes
                    # through, which is exactly the case a "layer" key would
                    # miss — the prior regression RealizedVia's docstring
                    # names.
                    if layer_name not in _via_layer_names(item, layers):
                        continue
                elif str(item.get("layer", "")) != layer_name:
                    continue
                poly = _copper_item_polygon(item)
                if poly is not None and not poly.is_empty:
                    blockers.append(
                        poly.buffer(clearance_mm, quad_segs=_BUFFER_QUAD_SEGS)
                    )
            # Mitred, not round: a round offset's facets undershoot the
            # clearance at every convex corner by the chord's sagitta, which
            # the clearance check measures as a shortfall between two pours.
            blockers += [c.buffer(clearance_mm, join_style=2) for c in claimed]
            region = region0
            area = region_polygon(spec.region)
            if area is not None:
                region = _polygonal(region.intersection(area))
            if blockers:
                region = region.difference(unary_union(blockers))
            kept = [f for f in _as_polygons(region) if f.area >= MIN_FRAGMENT_MM2]
            for frag in kept:
                out.append(_pour_item(frag, layer_name, net))
            claimed.extend(kept)
            if spec.region is None and kept:
                whole_board_done = True
    return out


def _blocker_layers(item: dict[str, Any]) -> list[str]:
    """The layer name(s) one ``cut_antipads`` blocker occupies. A
    ``ctype='via'`` blocker (the only shape :func:`precis.pcb.silk.
    build_fiducials` ever mints into ``FiducialResult.plane_blockers``,
    see that dataclass's own docstring) already carries a ``layers`` list
    of real stackup NAMES, not indices — unlike the copper vias
    :func:`plane_pours` reads off ``ir.stackup``, this list needs no
    resolution through :func:`precis.pcb.drc._via_layer_names`, because
    the caller that built it already knew which single layer the
    fiducial's flash sits on. A track/pad-shaped blocker (not used today,
    but ``_copper_item_polygon`` accepts the shape) falls back to its
    singular ``layer`` key, so this stays a general "what layer(s) does
    this obstacle occupy" reader rather than a via-only one."""
    layers = item.get("layers")
    if layers:
        return [str(layer_name) for layer_name in layers]
    layer = item.get("layer")
    return [str(layer)] if layer else []


def cut_antipads(
    pours: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    *,
    clearance_mm: float,
) -> list[dict[str, Any]]:
    """Punch a no-pour ring around each of ``blockers`` into ``pours`` —
    the antipad :func:`plane_pours` itself cannot cut for a board-level
    obstacle that does not exist yet when a plane is realized and
    persisted (a fiducial, see :class:`precis.pcb.silk.FiducialResult`'s
    own docstring for exactly why: fiducials are synthesised at RENDER
    time, in :meth:`precis.handlers.pcb.PcbHandler._board_furniture`,
    which runs after ``plane_pours`` has already run and its output has
    already been written to ``pcb_copper`` at realize time). This is the
    one place that knows how to subtract a keep-out from an already-built
    pour dict, so a caller with this exact problem reaches for this
    function rather than inlining shapely geometry a second time next to
    :func:`plane_pours`'s own boolean-geometry code.

    ``pours`` is ``plane_pours``'s own output shape (``ctype='pour'``,
    ``polygon``/optional ``holes``) — this does NOT mutate the list or
    any dict in it; it returns a NEW list, one entry per surviving
    fragment, in the same emit shape (:func:`_pour_item`, shared with
    ``plane_pours`` so the two never drift on what a pour dict means).
    ``blockers`` is :class:`precis.pcb.silk.FiducialResult`'s own
    ``plane_blockers`` shape today (a ``ctype='via'`` disc per fiducial,
    already sized to ``mask_dia_mm`` — the swelled MASK opening, not the
    bare copper disc, so the ring this cuts clears the whole opening a
    pick-and-place camera looks through, not just the copper under it) —
    ``clearance_mm`` is added on top of that disc exactly the way
    ``plane_pours`` buffers every OTHER foreign-net obstacle by its own
    ``clearance_mm`` before subtracting it, so a fiducial's antipad is
    sized by the SAME rule as every other keep-out this module cuts, not
    a second constant invented here (the caller resolves ``clearance_mm``
    from :mod:`precis.pcb.capabilities`, same as ``plane_pours``'s own
    callers do — see :func:`precis.pcb.rules.resolve_net_rules`).

    Only a blocker on the SAME layer as a pour is ever cut into it — a
    ``layers``/``layer`` mismatch (:func:`_blocker_layers`) leaves that
    pour byte-for-byte untouched, returned as the identical dict object
    rather than round-tripped through shapely, so a blocker fully outside
    every pour changes nothing at all, not even float noise. A pour with
    no matching blocker is likewise passed through unchanged. Existing
    ``holes`` on an affected pour survive: the polygon this reads off a
    pour dict already carries them as interior rings
    (:func:`precis.pcb.drc._copper_item_polygon`'s own ``ctype='pour'``
    handling), so subtracting a blocker's ring is one more difference
    against a shape that already has holes in it, not a fresh one that
    forgets them."""
    if not pours or not blockers:
        return list(pours)
    rings_by_layer: dict[str, list[BaseGeometry]] = {}
    for item in blockers:
        poly = _copper_item_polygon(item)
        if poly is None or poly.is_empty:
            continue
        ring = poly.buffer(abs(clearance_mm), quad_segs=_BUFFER_QUAD_SEGS)
        for layer_name in _blocker_layers(item):
            rings_by_layer.setdefault(layer_name, []).append(ring)

    out: list[dict[str, Any]] = []
    for pour in pours:
        rings = rings_by_layer.get(str(pour.get("layer", "")))
        if not rings:
            out.append(pour)
            continue
        pour_poly = _copper_item_polygon(pour)
        if pour_poly is None or pour_poly.is_empty:
            out.append(pour)
            continue
        cut = unary_union(rings)
        if not pour_poly.intersects(cut):
            out.append(pour)
            continue
        layer_name = str(pour.get("layer", ""))
        net = str(pour.get("net", ""))
        for frag in _as_polygons(pour_poly.difference(cut)):
            if frag.area < MIN_FRAGMENT_MM2:
                continue
            out.append(_pour_item(frag, layer_name, net))
    return out


#: The widest neck :func:`pour_quality` looks for, in mm. A pour whose
#: narrowest waist is wider than this is reported as ``None`` (no neck):
#: past a few millimetres the return path is not the problem a neck figure
#: is there to show, and every extra probe is a whole-polygon buffer.
NECK_PROBE_MAX_MM = 2.0
#: Coarse step of the neck scan, then bisected to :data:`NECK_REFINE_STEPS`
#: halvings. Splitting under erosion is not monotone (a piece that split
#: off can erode away again), so a pure bisection over the whole range can
#: miss a waist; a coarse scan finds the first split and the bisection
#: refines it to about 0.003 mm.
NECK_PROBE_STEP_MM = 0.1
NECK_REFINE_STEPS = 5


@dataclass(frozen=True, slots=True)
class PourQuality:
    """How whole one net's fill is on one layer (pcb-stackup-regions slice 1).

    ``fragments`` counts the separate pieces of copper on this layer alone
    (stitching vias and other layers can still join them into one net —
    :func:`precis.pcb.connectivity.net_islands` answers that).
    ``largest_share`` is the largest piece's area over the layer's total
    poured area, 1.0 for a single piece. ``min_neck_mm`` is the narrowest
    waist found in any piece: the erosion diameter at which a piece first
    splits in two, ``None`` when no piece splits below
    :data:`NECK_PROBE_MAX_MM`."""

    net: str
    layer: str
    fragments: int
    area_mm2: float
    largest_share: float
    min_neck_mm: float | None

    def phrase(self) -> str:
        """One clause for a note or a view line."""
        neck = (
            f"neck >{NECK_PROBE_MAX_MM:g}mm"
            if self.min_neck_mm is None
            else f"neck {self.min_neck_mm:.2f}mm"
        )
        return (
            f"{self.layer} {self.fragments} piece(s), "
            f"largest {self.largest_share:.0%}, {neck}"
        )


def _splits(poly: Polygon, r: float) -> bool:
    eroded = poly.buffer(-r, quad_segs=_BUFFER_QUAD_SEGS)
    return sum(1 for p in _as_polygons(eroded) if p.area >= MIN_FRAGMENT_MM2) > 1


def _neck_mm(poly: Polygon) -> float | None:
    """Erosion DIAMETER at which ``poly`` first splits, or ``None``."""
    lo = 0.0
    r = NECK_PROBE_STEP_MM
    while r <= NECK_PROBE_MAX_MM / 2.0 + 1e-9:
        if _splits(poly, r):
            hi = r
            for _ in range(NECK_REFINE_STEPS):
                mid = (lo + hi) / 2.0
                if _splits(poly, mid):
                    hi = mid
                else:
                    lo = mid
            return 2.0 * hi
        lo = r
        r += NECK_PROBE_STEP_MM
    return None


def pour_quality(pours: Iterable[dict[str, Any]]) -> list[PourQuality]:
    """One :class:`PourQuality` per (net, layer) in ``pours`` (the
    ``ctype='pour'`` items :func:`plane_pours` emits, one per fragment),
    sorted by net then layer."""
    by_key: dict[tuple[str, str], list[Polygon]] = {}
    for pour in pours:
        poly = _copper_item_polygon({**pour, "ctype": "pour"})
        if poly is None or poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        frags = [p for p in _as_polygons(poly) if p.area >= MIN_FRAGMENT_MM2]
        if frags:
            key = (str(pour.get("net", "")), str(pour.get("layer", "")))
            by_key.setdefault(key, []).extend(frags)
    out: list[PourQuality] = []
    for (net, layer), frags in sorted(by_key.items()):
        areas = [f.area for f in frags]
        total = sum(areas)
        necks = [n for n in (_neck_mm(f) for f in frags) if n is not None]
        out.append(
            PourQuality(
                net=net,
                layer=layer,
                fragments=len(frags),
                area_mm2=total,
                largest_share=max(areas) / total if total > 0 else 0.0,
                min_neck_mm=min(necks) if necks else None,
            )
        )
    return out


def pour_notes(quality: Iterable[PourQuality]) -> dict[str, str]:
    """Net name -> one ``pcb_routes.note`` clause covering every layer the
    net pours on, e.g. ``"pour: In1.Cu 1 piece(s), largest 100%, neck
    0.42mm"``."""
    notes: dict[str, str] = {}
    for q in quality:
        earlier = notes.get(q.net)
        notes[q.net] = f"{earlier}, {q.phrase()}" if earlier else f"pour: {q.phrase()}"
    return notes


#: Stackup keys added by pcb-stackup-regions slice 3; a stackup carrying
#: any of them is read with the slice-3 semantics
#: (:func:`resolve_assignments`).
SLICE3_STACKUP_KEYS = frozenset(
    {"fill", "power_regions", "direction", "route_cost", "reference"}
)


@dataclass(frozen=True, slots=True)
class PlaneAssignment:
    """One effective (layer, net) pour the router will make — a
    ``pcb_planes`` row (``source`` ``authored``/``derived``), or a stackup
    declaration with no row: a layer's ``plane_net`` (``stackup``) or its
    ``fill`` (``fill``, at :data:`FILL_PRIORITY`)."""

    layer: str
    net: str
    source: str
    region: Any = None
    priority: int = 0

    @property
    def locked(self) -> bool:
        """An instruction the anneal must not demote: everything except the
        anneal's own derived write-back."""
        return self.source != "derived"


def resolve_assignments(
    stackup: list[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
    net_names: Iterable[str],
) -> tuple[list[PlaneAssignment], list[str]]:
    """The board's effective plane assignments and a note per stackup
    declaration that pours nothing.

    ``rows`` are :meth:`precis.store.Store.pcb_planes_list` rows, kept as
    they are. On a stackup that speaks the slice-3 keys (a preset, or any
    layer carrying ``fill``/``power_regions``/``direction``/``route_cost``/
    ``reference``), a ``plane_net`` with no row for that net on its layer
    becomes a whole-board ``stackup`` assignment (gr495287: the default
    stackup's In1.Cu GND was a declaration the router never read, so a
    fresh board routed GND as traces and left In1 empty). A LEGACY stackup
    keeps its old meaning — ``op='stackup'`` mirrors the key into a row,
    and without one the router pours nothing, with a note: honouring it
    there changes every board born on the old default, which the nano
    measurement showed trips its one-way floors, so that switch is Reto's
    (pcb-stackup-regions slice 3 table). A ``fill`` becomes
    a whole-board assignment at :data:`FILL_PRIORITY`, cleared around every
    plane and region on its layer (gr495395: every copper layer pours, GND
    where nothing else claims it). A declaration is skipped, with a note,
    when its net does not exist or another net already has a whole-board
    row on that layer (one sheet; the row is the explicit instruction)."""
    known = set(net_names)
    out = [
        PlaneAssignment(
            layer=str(r["layer"]),
            net=str(r["net"]),
            source=str(r.get("source") or "authored"),
            region=r.get("region"),
            priority=int(r.get("priority") or 0),
        )
        for r in rows
    ]
    notes: list[str] = []
    honour_planes = any(key in e for e in stackup for key in SLICE3_STACKUP_KEYS)
    for entry in stackup:
        name = str(entry.get("name"))
        for key, source, priority in (
            ("plane_net", "stackup", 0),
            ("fill", "fill", FILL_PRIORITY),
        ):
            net = entry.get(key)
            if not net:
                continue
            net = str(net)
            on_layer = [a for a in out if a.layer == name]
            if key == "plane_net" and not honour_planes:
                if not any(a.net == net for a in on_layer):
                    notes.append(
                        f"{name}={net} (plane_net) declared in this legacy "
                        "stackup with no assignment row — the router will not "
                        "pour it; op='plane_net' or a stackup preset does"
                    )
                continue
            if net not in known:
                notes.append(
                    f"{name}={net} ({key}): no such net — the router will not pour it"
                )
                continue
            if any(a.net == net for a in on_layer):
                continue
            blocking = [
                a
                for a in on_layer
                if a.region is None and a.net != net and a.priority >= priority
            ]
            if blocking:
                notes.append(
                    f"{name}={net} ({key}): {blocking[0].net} ({blocking[0].source}) "
                    "already pours the whole layer — the router will not pour it"
                )
                continue
            out.append(PlaneAssignment(name, net, source, None, priority))
    return out, notes


#: Net classes :func:`derive_regions`' caller treats as power nets.
POWER_NET_CLASSES = frozenset({"power", "pwr"})


@dataclass(frozen=True, slots=True)
class DerivedRegion:
    """One power net's derived fill region (:func:`derive_regions`)."""

    net: str
    region: dict[str, Any]
    priority: int
    area_mm2: float


#: :func:`derive_regions`' concave-hull ratio (shapely: 0 most concave, 1
#: the convex hull). A power net's pins cluster around its loads; a hull
#: that hugs them leaves the rest of the layer to the other regions and
#: the fill.
REGION_HULL_RATIO = 0.3


def derive_regions(
    net_pins: dict[str, list[tuple[float, float]]],
    *,
    outline: list[list[float]],
    edge_clearance_mm: float,
    clearance_mm: float,
    reach_mm: float | dict[str, float],
) -> tuple[list[DerivedRegion], list[str]]:
    """Fill regions for power nets on one layer (pcb-stackup-regions slice
    4; Reto: regions are derived, never drawn).

    Each net's region is the concave hull of its pins dilated by
    ``reach_mm`` (how far from its pad a drop via may land; one figure, or
    one per net), trimmed to the
    outline inset by ``edge_clearance_mm`` and to the regions already taken,
    grown by ``clearance_mm``. Nets go in pin-count order (most first, name
    on a tie), and the pin count is the priority. A net is given a region
    only when every pin's whole drop-via reach (inside the board) lies in
    it, as ONE polygon: a pin outside its region would drop a via into
    another net's copper, and a region in two pieces would pour a split
    plane. Such a net gets no region and routes as traces; the second return
    names it and why. A net with fewer than two pins needs no plane."""
    notes: list[str] = []
    if not outline or len(outline) < 3:
        return [], notes
    board = Polygon([(float(p[0]), float(p[1])) for p in outline])
    if not board.is_valid:
        board = board.buffer(0)
    inset = board.buffer(
        -abs(edge_clearance_mm), quad_segs=_BUFFER_QUAD_SEGS, join_style=2
    )
    if inset.is_empty:
        return [], notes
    order = sorted(
        (n for n, pts in net_pins.items() if len(pts) >= 2),
        key=lambda n: (-len(net_pins[n]), n),
    )
    taken: list[BaseGeometry] = []
    out: list[DerivedRegion] = []
    for net in order:
        reach = float(reach_mm[net] if isinstance(reach_mm, dict) else reach_mm)
        pts = MultiPoint([(float(x), float(y)) for x, y in net_pins[net]])
        hull = concave_hull(pts, ratio=REGION_HULL_RATIO)
        area = _polygonal(
            hull.buffer(reach, quad_segs=_BUFFER_QUAD_SEGS).intersection(inset)
        )
        if taken:
            area = area.difference(
                unary_union(taken).buffer(clearance_mm, join_style=2)
            )
        parts = [p for p in _as_polygons(area) if p.area >= MIN_FRAGMENT_MM2]
        if len(parts) != 1:
            notes.append(
                f"{net}: no region — "
                + (
                    "nothing left after the regions above"
                    if not parts
                    else f"its region would be {len(parts)} pieces"
                )
            )
            continue
        poly = parts[0]
        # A pin's reach disc, clipped to the board, must sit inside the
        # region. Both circles are faceted (16 segments a quarter), at
        # different angles, so the slack is the facets' sagitta.
        grown = poly.buffer(reach * (1.0 - math.cos(math.pi / 64.0)) * 2.0 + 1e-3)
        outside = [
            (x, y)
            for x, y in net_pins[net]
            if not grown.contains(
                Point(float(x), float(y))
                .buffer(reach, quad_segs=_BUFFER_QUAD_SEGS)
                .intersection(inset)
            )
        ]
        if outside:
            x, y = outside[0]
            notes.append(
                f"{net}: no region — {len(outside)} pin(s) (first at "
                f"({x:.1f}, {y:.1f})) reach outside what the regions above left"
            )
            continue
        taken.append(poly)
        out.append(DerivedRegion(net, region_json(poly), len(net_pins[net]), poly.area))
    return out, notes


def point_in_pour(pour: dict[str, Any], x: float, y: float) -> bool:
    """Is ``(x, y)`` on this pour's copper? Exterior ring minus holes.

    Ray casting, no shapely: this is called from
    :mod:`precis.pcb.connectivity`, which exists to catch the geometry
    engine being wrong and so must not be built on it. Boundary cases go
    to "inside" — a via centred exactly on a pour edge is connected, and
    over-reporting connection here would need a real gap elsewhere to
    matter, whereas under-reporting it invents an island.
    """
    rings: list[list[list[float]]] = [pour.get("polygon") or []]
    if not _in_ring(rings[0], x, y):
        return False
    return not any(_in_ring(hole, x, y) for hole in pour.get("holes") or [])


def _in_ring(ring: list[list[float]], x: float, y: float) -> bool:
    if len(ring) < 3:
        return False
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        if math.isclose(y1, y2):
            continue
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > x:
                inside = not inside
    return inside


__all__ = [
    "FILL_PRIORITY",
    "MIN_FRAGMENT_MM2",
    "POWER_NET_CLASSES",
    "DerivedRegion",
    "PlaneAssignment",
    "PourQuality",
    "PourSpec",
    "cut_antipads",
    "derive_regions",
    "plane_pours",
    "point_in_pour",
    "pour_quality",
    "region_json",
    "region_polygon",
    "resolve_assignments",
]
