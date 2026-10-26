"""The ``TopoDesign`` input shape — one immutable, vectorised snapshot of
everything the topo-place kernel reads, built once from the IR.

Why a separate shape instead of reading :class:`~precis.pcb.ir.PcbIR`
directly: the IR carries every fidelity the optimizer/realizer cascade
needs (dirty masks, segment layers, rotation darts); the placer needs a
small, dense, numpy-only view it can hold next to a ``(N, 4)`` pose
array and re-derive nothing. This module is that view plus
:func:`design_from_ir`, the one sanctioned IR → snapshot bridge.

Pin positions stay FOOTPRINT-LOCAL (``pin_xy``, pre-rotation,
pre-mirror — the same frame :attr:`PcbIR.pin_dx` uses); a consumer
applies the pose itself, mirror-before-rotate like
:func:`precis.pcb.pose.rel_to_world`, so placer and pairmap can never
disagree about where a pad went.

``flex``/``swap_groups`` are the 1b declarations' slots — the shape is
fixed here so 1b only fills semantics, never reshapes the input.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis.pcb import ratsnest
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    NO_NET,
    PcbIR,
    instance_courtyard_polygons,
    layer_name,
)
from precis_geom.pcb import PinConflict

#: ``freedoms`` bitset values (Decision 17's "allowed freedoms").
#: ROTATE — quarter-turn moves are legal; BACK_SIDE — the part may be
#: flipped to the bottom (mirrors its pin x, per the pose convention).
FREEDOM_ROTATE = np.uint8(1)
FREEDOM_BACK_SIDE = np.uint8(2)


@dataclass(frozen=True, slots=True)
class FlexSlot:
    """One ``part.pin(any_of=[...])`` declaration (1b): the solver picks
    ONE pin out of ``pins`` for the slot's signal. ``name`` feeds the
    firmware pin map (Decision 11); ``net`` indexes
    :attr:`TopoDesign.decl_nets` — the net the slot's wire belongs to,
    filled by ``net.tie`` at declaration time. ``site`` is the
    ``file:line`` claim site the wheel records for conflict messages;
    ``requirement_a`` the tie's ``current=Max`` bound in amps (Decision
    15 — ``Min`` is not a pin filter); ``pin_max_a`` is the per-candidate
    drive rating the ``pin_functions`` table carries, NaN = unrated."""

    part: int
    pins: tuple[int, ...]
    net: int
    name: str | None = None
    site: str | None = None
    requirement_a: float | None = None
    pin_max_a: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class SwapGroup:
    """A declared swap group (1b, Decision 13): ``members`` are pin ids
    that may permute among themselves (resistor packs), or — when
    ``tuple_`` — equal-length tuples moved as units (buffer channels:
    ``in_i``/``out_i`` move together)."""

    part: int
    members: tuple[tuple[int, ...], ...]
    tuple_: bool = False


@dataclass(frozen=True, slots=True)
class ObjectiveShares:
    """The named share vector over the proxy's objective terms
    (Decision 18 / pcb-intent-api §Weights): ``crossings`` is the
    straight-airwire crossing count, ``ratsnest`` the total airwire
    length in mm (the term name pcb-intent-api Decision 3 fixes).
    Shares are normalised to sum 1; :class:`~.proxy.ProxyState` applies
    them reference-normalised at its seed pose, so a share means "this
    fraction of J at the seed", not a raw unit weight. Later slices add
    the terms only their rungs can price (``vias``, ``area``, …)."""

    crossings: float = 0.8
    ratsnest: float = 0.2
    #: Via count — priced only from ladder rung 2 onward (slice 2's
    #: ``route_topology``); 0 here keeps the share vector byte-identical
    #: to slice 1 for every existing run.
    vias: float = 0.0
    #: Same-side courtyard overlap area in mm² (Reto ruling 2026-10-25:
    #: a courtyard overlap is a placer violation, priced between rung-2
    #: evals — slice-2a seam item 6). 0 keeps a share vector identical
    #: to slice 1's.
    overlap: float = 0.0
    #: Board-fill repulsion (loop-2 quality ask): part pairs that share
    #: NO net and could collide repel each other, so unconnected clumps
    #: spread over the whole outline while net-connected parts still
    #: cluster under the ratsnest pull. 0 keeps a share vector identical
    #: to slice 2a's.
    spread: float = 0.0

    def __post_init__(self) -> None:
        total = self.crossings + self.ratsnest + self.vias + self.overlap + self.spread
        if total <= 0:
            raise ValueError("objective shares must not all be zero")
        object.__setattr__(self, "crossings", self.crossings / total)
        object.__setattr__(self, "ratsnest", self.ratsnest / total)
        object.__setattr__(self, "vias", self.vias / total)
        object.__setattr__(self, "overlap", self.overlap / total)
        object.__setattr__(self, "spread", self.spread / total)


#: The named share vectors (Decision 18's presets) over the terms slice 1
#: prices. ``low-via`` and ``2-layer-only`` differ from ``compact`` only on
#: terms later rungs price (via count, inner-layer direction), so they
#: collapse onto crossings-heavy shares here — a via exists exactly where
#: a crossing cannot be resolved on one layer, so crossings are the
#: slice-1 proxy for both (spec-call, 1c). Every preset carries the same
#: overlap share: an authored courtyard collision is a violation on any
#: objective, never a trade-off (Reto ruling 2026-10-25). ``compact``
#: — the share vector the ``placer='topo'`` job runs — also carries a
#: modest ``spread`` share: the loop's ask is whole-board usage, and
#: repulsion between unconnected parts is what pays for it.
PRESETS: dict[str, ObjectiveShares] = {
    "compact": ObjectiveShares(crossings=0.55, ratsnest=0.2, overlap=0.2, spread=0.05),
    "low-via": ObjectiveShares(crossings=0.75, ratsnest=0.05, overlap=0.2),
    "2-layer-only": ObjectiveShares(crossings=0.8, ratsnest=0.0, overlap=0.2),
}


@dataclass(frozen=True, slots=True)
class TopoZone:
    """A non-part constrained region the mesh keeps wires out of —
    Decision 17's authored keepouts plus the two board-config bodies the
    router already claims (:attr:`PcbIR.mounting_holes`,
    :attr:`PcbIR.fixed_vias`, ``fixed_copper`` track/pour rows). ``ring``
    is a CLOSED world-frame polygon; ``layers`` the blocked copper-layer
    indices — ``()`` means every layer (a drilled barrel is a hole on all
    of them); ``net`` the fixed copper's net name when known (a same-net
    wire may pass — the wire builder reads it, the mesh does not)."""

    kind: str  # 'keepout' | 'mounting_hole' | 'fixed_via' | 'fixed_copper'
    ring: np.ndarray  # (K, 2) world mm, closed
    layers: tuple[int, ...] = ()
    net: str = ""
    name: str = ""
    #: Index into ``parts`` of the instance this zone belongs to — a
    #: mounting hole cut from a connector's footprint. The owner's
    #: courtyard covers its own hole by design (``courtyard_hole``'s
    #: owner exemption); -1 = a board-level zone nobody owns.
    part: int = -1
    #: True when ``ring`` is in the OWNER's footprint frame and must be
    #: pose-transformed per evaluation (:func:`proxy.zone_world_ring`) —
    #: a part-owned hole moves with its part, matching the DRC, which
    #: recomputes the keep-out from the owner's current pose.
    part_local: bool = False


@dataclass(frozen=True, slots=True)
class TopoDesign:
    """The placer's whole world (spec §1 input shape). Index = part id
    everywhere; ``parts[i]`` is the refdes. ``pose0`` rows are
    ``(x, y, rot_quarter, side)`` — ``rot_quarter`` 0..3 is the authored
    rotation snapped to the quarter turn the mover uses (the board
    frame's clockwise-from-north convention;
    :func:`precis.pcb.pose.rotate_cw`), ``side`` 0 top / 1 bottom.
    ``nan`` x/y means unplaced."""

    parts: tuple[str, ...]  # refdes, index = part id
    pin_xy: np.ndarray  # (P, 2) pin offsets in part frame, mm
    pin_part: np.ndarray  # (P,) int part id
    pin_side: np.ndarray  # (P,) int8: 0 top SMD, 1 bottom SMD, 2 TH
    pin_label: np.ndarray  # (P,) object: footprint pin/pad name per pin (1b)
    pin_net: np.ndarray  # (P,) int32: ir net id per pin, NO_NET unwired (1b)
    #: per routable net: pin ids. Plane-class nets are dropped unless
    #: ``design_from_ir(route_planes=True)`` — the placer's design keeps
    #: them out (a rail's member list would mint airwires the proxy
    #: counts as crossings); the router's design takes them in (Reto:
    #: GND and power route as normal nets, widened post-route).
    net_pins: tuple[np.ndarray, ...]
    flex: tuple[FlexSlot, ...]  # any_of slots (1b)
    swap_groups: tuple[SwapGroup, ...]  # plain / tuple groups (1b)
    fixed: np.ndarray  # (N,) bool: xy locked
    freedoms: np.ndarray  # (N,) uint8 bitset: ROTATE | BACK_SIDE
    outline: np.ndarray  # (K, 2) board outline, mm
    pose0: np.ndarray  # (N, 4) x, y, rot_quarter (0..3), side (0/1)
    #: The board's ``placement_sides`` (``meta.placement_sides`` — 'both'
    #: default): 'top'/'bottom' forbid the flip move that would land a
    #: part on the other side — the placer checks it per proposal, so a
    #: part already on the disallowed side may still flip OFF it
    #: (gr520370: a one-sided JLC assembly cannot take bottom parts).
    sides: str = "both"
    claims: tuple[tuple[int, str], ...] = ()  # (pin id, name) named fixed claims
    #: The nets 1b declarations reference — slot ties plus swap-group
    #: members' wired nets. Indexed by :attr:`FlexSlot.net`. ``decl_nets``
    #: is the ir-level pin membership (every ir net included, whatever
    #: ``net_pins`` holds); ``decl_net_ir`` the ir net id (-1 when the
    #: declared name matches no ir net); ``decl_net_route`` the
    #: ``net_pins`` index or -1; ``decl_net_plane`` marks plane-class
    #: nets (a pin bound there ties to the plane — the pin-assignment
    #: proxy mints no airwire for it, whether or not
    #: ``route_planes`` routed the net).
    decl_nets: tuple[np.ndarray, ...] = ()
    decl_net_ir: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    decl_net_route: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    decl_net_plane: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    decl_net_name: tuple[str, ...] = ()
    #: The 1b ``pin_functions`` tables as pin-id-keyed function sets,
    #: indexed by part — ``pin_functions[part][pin_id] = ("GPIO","INT1")``.
    #: The summary's widen-a-slot ranked edit needs them (1c spec-call).
    pin_functions: tuple[Mapping[int, tuple[str, ...]], ...] = ()
    #: Slice 2 (mesh): each part's courtyard polygon as a CLOSED ring
    #: ``(K, 2)`` in the part's own footprint frame (first point repeated
    #: last), the same shape the placer reserves and placement DRC checks
    #: (:func:`precis.pcb.ir.instance_courtyard_polygons`, pad hull +
    #: ``COURTYARD_CLEARANCE_MM``). The mesh constrains its edges so no
    #: wire threads a part body. ``()``/empty = no outline (synthetic
    #: designs, pinless parts without a fallback).
    part_outline: tuple[np.ndarray, ...] = ()
    #: Slice 2 (list-2-colouring): per ``net_pins`` entry the forced
    #: outer layer from the net's class ``layers`` rule, projected onto
    #: the two outer layers: ``-1`` free (both outers allowed or no
    #: rule), ``0``/``n_layers - 1`` forced top/bottom, ``-2`` = the lock
    #: admits NO outer layer (inner-only — uncolourable on the F/B pair,
    #: a certificate).
    net_layer_force: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int8))
    #: Slice 2 (list-2-colouring): per ``net_pins`` entry the outer-layer
    #: force implied by pad sides alone — every pin top SMD → 0, every
    #: pin bottom SMD → ``n_layers - 1``, anything else (mixed sides, or
    #: a TH pin, which exists on every layer) → -1 free. The colouring
    #: intersects this with ``net_layer_force``; an empty intersection
    #: is a ForcedClash certificate, not a refused board.
    net_pin_force: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int8))
    #: Slice 2 (mesh): non-part constrained regions — authored keepouts,
    #: mounting holes, fixed copper — in build order. The mesh records a
    #: zone-owned constrained edge under owner ``n_parts + zone_index``.
    zones: tuple[TopoZone, ...] = ()
    #: Slice 2 (certificates): the routable net's name per ``net_pins``
    #: entry — odd-cycle certificates name nets, not indices.
    net_name: tuple[str, ...] = ()
    #: Slice 4 (corridor capacity): each pin's pad ``(w, h)`` in the part
    #: frame — an edge's wire capacity is its length minus the pad
    #: extents it touches. ``ir.pin_w``/``pin_h`` verbatim.
    pin_wh: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), np.float64))


def _pin_sides(ir: PcbIR, footprints: Mapping[str, Mapping[str, Any]]) -> np.ndarray:
    """0 top-SMD / 1 bottom-SMD / 2 TH per pin. A pad carrying ``drill``
    is through-hole regardless of side (Decision 6's endpoint rule
    keys off this); no footprint row → SMD on the instance's side."""
    drilled: dict[str, set[str]] = {}
    for refdes, fp in footprints.items():
        nums = {
            str(p.get("number") or "").strip()
            for p in (fp.get("pads") or [])
            if p.get("drill") is not None
        }
        if nums:
            drilled[refdes] = nums
    sides = np.where(ir.inst_bottom, np.int8(1), np.int8(0))
    out = sides[ir.pin_instance].astype(np.int8)
    for pin_id in range(ir.n_pins):
        refdes = str(ir.instance_refdes[int(ir.pin_instance[pin_id])])
        if str(ir.pin_label[pin_id]) in drilled.get(refdes, ()):
            out[pin_id] = 2
    return out


#: Layer ``layers`` rules may name the stackup's own layer names or the
#: standard names — this sentinel marks a lock that admits no OUTER
#: layer (``net_layer_force``; list-2-colouring can't satisfy it).
LAYER_FORCE_INNER = np.int8(-2)
#: ``net_layer_force`` value for a net with no outer-layer restriction.
LAYER_FORCE_FREE = np.int8(-1)


def _layer_name_map(ir: PcbIR) -> dict[str, int]:
    """Every acceptable layer NAME → index: the stackup's own names plus
    the standard ``layer_name`` set, so a preset-named stackup still
    reads. Shared by the class-rule projection and the keepout zone
    filter."""
    name_to_idx = {
        str(layer.get("name") or ""): i
        for i, layer in enumerate(ir.stackup)
        if layer.get("name")
    }
    name_to_idx.update({layer_name(i, ir.n_layers): i for i in range(ir.n_layers)})
    return name_to_idx


def _net_layer_force(
    ir: PcbIR,
    route_of: dict[int, int],
    net_classes: Mapping[str, Mapping[str, Any]] | None,
    name_to_idx: Mapping[str, int],
) -> np.ndarray:
    """Per ``net_pins`` entry the outer-layer force implied by the net's
    class ``layers`` rule (``net_classes`` is the graph's
    ``{class_name: rules}`` map — ``pcb_net_classes.rules`` rows). A name
    that resolves to no stackup layer refuses — reading it as
    "inner-only" would fabricate a layer lock the author never wrote
    (slice-2a seam item 8). Projection: both outers allowed →
    ``LAYER_FORCE_FREE``; one → that index; none → ``LAYER_FORCE_INNER``."""
    n_routed = max(route_of.values(), default=-1) + 1
    force = np.full(n_routed, LAYER_FORCE_FREE, dtype=np.int8)
    if not net_classes:
        return force
    for ir_net, k in route_of.items():
        cls = str(ir.net_class[ir_net] or "")
        if not cls:
            continue
        rules = net_classes.get(cls)
        allowed_names = (rules or {}).get("layers")
        if not allowed_names:
            continue
        unknown = [str(nm) for nm in allowed_names if str(nm) not in name_to_idx]
        if unknown:
            raise BadInput(
                f"net class {cls!r} rule 'layers' names {unknown} — not "
                f"in the {ir.n_layers}-layer stackup",
                next=f"fix net class {cls!r} to name a stackup layer",
                options=sorted(name_to_idx),
            )
        allowed = {name_to_idx[str(nm)] for nm in allowed_names}
        outer = allowed & {0, ir.n_layers - 1}
        if len(outer) == 2:
            continue
        force[k] = np.int8(outer.pop()) if outer else LAYER_FORCE_INNER
    return force


def _net_pin_force(
    net_pins: tuple[np.ndarray, ...], pin_side: np.ndarray, n_layers: int
) -> np.ndarray:
    """Per ``net_pins`` entry the outer-layer force implied by pad sides:
    every pin top SMD → 0, every pin bottom SMD → ``n_layers - 1``,
    anything else → ``LAYER_FORCE_FREE`` (a TH pad exists on every
    layer, so it forces nothing — Decision 6)."""
    force = np.full(len(net_pins), LAYER_FORCE_FREE, dtype=np.int8)
    for k, pins in enumerate(net_pins):
        sides = pin_side[pins]
        if (sides == 0).all():
            force[k] = np.int8(0)
        elif (sides == 1).all():
            force[k] = np.int8(n_layers - 1)
    return force


def _circle_ring(x: float, y: float, r: float, n: int = 12) -> np.ndarray:
    """A closed n-gon standing in for a drilled disc — the mesh works in
    rings, and 12 sides costs little against a millimetre object."""
    a = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    ring = np.column_stack([x + r * np.cos(a), y + r * np.sin(a)])
    return np.vstack([ring, ring[:1]])


def _world_to_local(x: float, y: float, row: np.ndarray) -> tuple[float, float]:
    """A world point read back into the part frame under pose ``row`` —
    the exact inverse of ``proxy.local_to_world`` (untranslate,
    un-rotate, un-mirror). Lives here, not in proxy: ``_zones`` runs at
    design-build time and proxy imports this module."""
    px, py, rot_q, side = (float(v) for v in row[:4])
    rx, ry = x - px, y - py
    r = round(rot_q) % 4
    if r == 0:
        mx, ly = rx, ry
    elif r == 1:  # forward (a, b) -> (b, -a)
        mx, ly = -ry, rx
    elif r == 2:
        mx, ly = -rx, -ry
    else:  # r == 3, forward (a, b) -> (-b, a)
        mx, ly = ry, -rx
    return (-mx if side >= 0.5 else mx), ly


def _zones(
    ir: PcbIR,
    features: Iterable[Mapping[str, Any]] | None,
    fixed_copper: Iterable[Mapping[str, Any]] | None,
    name_to_idx: Mapping[str, int],
    pose0: np.ndarray,
) -> tuple[TopoZone, ...]:
    """Every non-part constrained region (Decision 17's keepouts plus the
    board-config bodies the router claims — slice-2a seam item 8):
    mounting holes and authored vias off the IR, ``ftype='keepout'``
    features (layer-scoped; a name that resolves to no stackup layer
    contributes nothing to ``layers`` — an unscoped keepout blocks every
    layer), and ``fixed_copper`` track/pour rows polygonised through the
    DRC's one shape function. ``ctype='via'`` rows are skipped: the same
    rows already arrived as :attr:`PcbIR.fixed_vias`."""
    zones: list[TopoZone] = []
    part_of = {str(r): i for i, r in enumerate(ir.instance_refdes)}
    # The placement-DRC keep-out verbatim (hardware envelope plus
    # courtyard clearance) — a smaller ring lets shove/legalize pass a
    # pose the gate still flags courtyard_hole (gr515107 r4: taz15/
    # heater's residuals sat inside the keep-out but off the
    # drill/annulus ring). The owner part is exempt like the DRC's — a
    # connector's courtyard covers its own standoff holes by design.
    # A hole with footprint-local coords rides its owner's pose like
    # the DRC's keep-out does; a legacy part-owned hole without them
    # gets local coords derived from the owner's pose AT IMPORT, so it
    # rides the owner too — only an unplaced owner (no pose to invert)
    # keeps the authored world position as a static zone.
    from precis.pcb.optimize import (
        _hole_keepout_radius_mm,
        _hole_polygon,
        mounting_hole_keepout_polygon,
    )

    for h in ir.mounting_holes:
        if max(float(h.drill_mm), float(h.ring_dia_mm), float(h.head_dia_mm)) <= 0.0:
            continue
        owner = part_of.get(str(h.part), -1) if h.part else -1
        local: tuple[float, float] | None = None
        if owner >= 0:
            if h.local_x is not None and h.local_y is not None:
                local = (float(h.local_x), float(h.local_y))
            elif np.isfinite(pose0[owner, :2]).all():
                local = _world_to_local(float(h.x), float(h.y), pose0[owner])
        if local is not None:
            ring = np.asarray(
                _hole_polygon(local[0], local[1], _hole_keepout_radius_mm(h)),
                dtype=np.float64,
            )
            part_local = True
        else:
            ring = np.asarray(mounting_hole_keepout_polygon(h), dtype=np.float64)
            part_local = False
        zones.append(
            TopoZone(
                kind="mounting_hole",
                ring=ring,
                part=owner,
                name=f"mounting hole {h.x:g},{h.y:g}",
                part_local=part_local,
            )
        )
    for v in ir.fixed_vias:
        r = float(v.dia_mm) / 2.0
        if r <= 0.0:
            continue
        zones.append(
            TopoZone(
                kind="fixed_via",
                ring=_circle_ring(float(v.x), float(v.y), r),
                net=str(v.net),
                name=f"fixed via {v.x:g},{v.y:g}",
            )
        )
    for f in features or ():
        if str(f.get("ftype") or "") != "keepout":
            continue
        poly = (f.get("geom") or {}).get("poly") or []
        ring = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
        if len(ring) < 3:
            continue
        if not np.allclose(ring[0], ring[-1], atol=1e-9):
            ring = np.vstack([ring, ring[0]])
        # ``layers`` arrives inline on authored features or inside
        # ``geom`` on stored rows (_pcb_insert_feature has no layers
        # column — it persists the list into geom, topo-s5 fix1).
        ko_layers = f.get("layers") or (f.get("geom") or {}).get("layers") or ()
        unknown = [str(nm) for nm in ko_layers if str(nm) not in name_to_idx]
        if unknown:
            raise BadInput(
                f"keepout names layer(s) {unknown} — not in the "
                f"{ir.n_layers}-layer stackup",
                next="fix the keepout's layers to name a stackup layer",
                options=sorted(name_to_idx),
            )
        zones.append(
            TopoZone(
                kind="keepout",
                ring=ring,
                layers=tuple(sorted(name_to_idx[nm] for nm in ko_layers)),
                name=str(
                    f.get("reason") or (f.get("geom") or {}).get("reason") or "keepout"
                ),
            )
        )
    if fixed_copper:
        # Local import: the DRC shape function's module pulls the full
        # rule stack; zones are built only when authored copper exists.
        from precis.pcb.drc import _copper_item_polygon

        for row in fixed_copper:
            ctype = str(row.get("ctype") or "")
            if ctype == "via":
                continue  # already a zone via ir.fixed_vias
            geom = _copper_item_polygon(dict(row))
            if geom is None or geom.is_empty or not hasattr(geom, "exterior"):
                continue
            layer = str(row.get("layer") or "")
            zones.append(
                TopoZone(
                    kind="fixed_copper",
                    ring=np.asarray(geom.exterior.coords, dtype=np.float64),
                    layers=(name_to_idx[layer],) if layer in name_to_idx else (),
                    net=str(row.get("net") or ""),
                    name=f"fixed copper {ctype or '?'}",
                )
            )
    return tuple(zones)


def design_from_ir(
    ir: PcbIR,
    footprints: Mapping[str, Mapping[str, Any]],
    *,
    pin_decls: Mapping[str, Mapping[str, Any]] | None = None,
    net_classes: Mapping[str, Mapping[str, Any]] | None = None,
    features: Iterable[Mapping[str, Any]] | None = None,
    fixed_copper: Iterable[Mapping[str, Any]] | None = None,
    route_planes: bool = False,
    placement_sides: str | None = None,
) -> TopoDesign:
    """Build the one immutable placer input from the IR.

    ``footprints`` is the refdes-keyed pad cache
    (:func:`precis.pcb.session.footprints_by_refdes` shape — ``pads`` /
    ``pin_map`` / ``courtyard`` / ``centroid`` / ``holes``); only
    ``pads[*].number``/``drill`` are read, to tag through-hole pins.

    ``pin_decls`` overrides :attr:`PcbIR.inst_pin_decls` (refdes → the
    ``pin_decls`` component blob) — for callers holding decls that never
    passed through a graph row; ``None`` reads the IR's own.

    ``net_classes`` is the graph's ``{class_name: rules}`` map
    (``pcb_net_classes`` rows, e.g. a ``{"layers": ["B.Cu"]}`` lock);
    ``None`` means no class rules — every net free on both outer layers.
    A ``layers`` name that resolves to no stackup layer raises
    :class:`~precis.errors.BadInput` — silently reading it as inner-only
    would fabricate a lock the author never wrote.

    ``features`` is the board's feature list (``ftype='keepout'`` rows
    become constrained zones); ``fixed_copper`` the authored-copper rows
    (track/pour become zones; vias already ride :attr:`PcbIR.fixed_vias`).
    ``None``/empty contributes no zones — mounting holes and authored
    vias come off the IR itself either way.

    Conventions the spec leaves to the builder, fixed here:
    - ``net_pins`` keeps every non-plane signal net with ≥2 pins
      (``net_class`` against :data:`ratsnest.PLANE_CLASSES`, the same
      drop ``build_airwires`` makes — single-pin nets carry no wire).
      ``route_planes=True`` admits ≥2-pin plane-class nets too — the
      slice-5 ruling routes GND/power as normal nets; it is opt-in so
      the placer's design never pays for rail airwires (a routing
      caller builds its own design for the settled pose).
    - 1b decls ride ``decl_nets`` — NOT ``net_pins``: a plane net's
      member list would mint airwires the proxy would count as
      crossings. A slot's ``net`` index points into ``decl_nets``;
      ``decl_net_route`` maps it back to ``net_pins`` when the net is
      routable.
    - ``freedoms`` derives from the lock bits: ROTATE unless
      ``inst_fixed_rot``, BACK_SIDE unless ``inst_fixed_xy`` (a part
      that may not move may not change sides either) — and never for a
      ``fixed_side`` part (gr520370: the authored side is a lock the
      placer must honour, not a preference). ``placement_sides``
      ('top'/'bottom' off the board meta) lands on
      :attr:`TopoDesign.sides`; the placer rejects the flip proposal
      that would land a part on the disallowed side — a part already
      there may still flip off it.
    - ``pose0`` rotation snaps ``inst_rot`` to the nearest quarter turn
      (mod 4); non-quarter authored rotations are approximated, not
      refused — the mover only ever produces quarter turns.
    - ``outline`` empty when the IR carries none — the placer falls
      back to a part-count-scaled square, like ``autoplace``.
    - a decl pin label that is not a live pin raises :class:`PinConflict`
      naming the part and label — the wheel declares every candidate so
      this only fires on hand-authored ``pin_decls``.
    """
    parts = tuple(str(r) for r in ir.instance_refdes)
    n = len(parts)

    net_pins: list[np.ndarray] = []
    net_names: list[str] = []
    route_of: dict[int, int] = {}  # ir net id -> net_pins index
    for net_id in range(ir.n_nets):
        cls = str(ir.net_class[net_id] or "").strip().lower()
        if cls in ratsnest.PLANE_CLASSES and not route_planes:
            continue
        pins = np.flatnonzero(ir.pin_net == net_id).astype(np.int64)
        if len(pins) >= 2:
            route_of[net_id] = len(net_pins)
            net_pins.append(pins)
            net_names.append(str(ir.net_name[net_id]))

    rot_q = np.nan_to_num(ir.inst_rot, nan=0.0)
    rot_q = np.round(rot_q / 90.0).astype(np.int64) % 4
    pose0 = np.column_stack(
        [
            np.asarray(ir.inst_x, dtype=np.float64),
            np.asarray(ir.inst_y, dtype=np.float64),
            rot_q.astype(np.float64),
            np.asarray(ir.inst_bottom, dtype=np.float64),
        ]
    )

    freedoms = np.zeros(n, dtype=np.uint8)
    freedoms[~ir.inst_fixed_rot] |= FREEDOM_ROTATE
    freedoms[~ir.inst_fixed_xy] |= FREEDOM_BACK_SIDE
    if ir.inst_fixed_side is not None:
        freedoms[np.asarray(ir.inst_fixed_side, dtype=bool)] &= np.uint8(
            ~FREEDOM_BACK_SIDE
        )

    outline = np.empty((0, 2), dtype=np.float64)
    if ir.outline:
        outline = np.asarray(ir.outline, dtype=np.float64)

    decl = _decls(ir, parts, route_of, pin_decls)

    # The courtyard ring every placer/DRC consumer uses, in the part's
    # own frame — the mesh's "footprint outline corners" (slice 2).
    # ``fallback_half_extent_mm`` 1.0 is ``COURTYARD_MIN_SEPARATION_MM/2``
    # (cost.py — not imported: it pulls the optimizer objective stack).
    part_outline = tuple(
        np.asarray(poly, dtype=np.float64) if poly else np.zeros((0, 2))
        for poly in instance_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=1.0,
        )
    )

    names = _layer_name_map(ir)
    pin_side = _pin_sides(ir, footprints)

    return TopoDesign(
        parts=parts,
        pin_xy=np.column_stack(
            [
                np.asarray(ir.pin_dx, dtype=np.float64),
                np.asarray(ir.pin_dy, dtype=np.float64),
            ]
        ),
        pin_part=np.asarray(ir.pin_instance, dtype=np.int64),
        pin_side=pin_side,
        pin_label=np.asarray(ir.pin_label, dtype=object),
        pin_net=np.asarray(ir.pin_net, dtype=np.int32),
        net_pins=tuple(net_pins),
        flex=decl["flex"],
        swap_groups=decl["swap_groups"],
        fixed=np.asarray(ir.inst_fixed_xy, dtype=bool).copy(),
        freedoms=freedoms,
        outline=outline,
        pose0=pose0,
        sides=(
            str(placement_sides).strip().lower()
            if str(placement_sides or "").strip().lower() in ("top", "bottom")
            else "both"
        ),
        claims=decl["claims"],
        decl_nets=decl["nets"],
        decl_net_ir=decl["net_ir"],
        decl_net_route=decl["net_route"],
        decl_net_plane=decl["net_plane"],
        decl_net_name=decl["net_name"],
        pin_functions=decl["pin_functions"],
        part_outline=part_outline,
        net_layer_force=_net_layer_force(ir, route_of, net_classes, names),
        net_pin_force=_net_pin_force(tuple(net_pins), pin_side, ir.n_layers),
        zones=_zones(ir, features, fixed_copper, names, pose0),
        net_name=tuple(net_names),
        pin_wh=np.column_stack(
            [
                np.asarray(ir.pin_w, dtype=np.float64),
                np.asarray(ir.pin_h, dtype=np.float64),
            ]
        ),
    )


def _decls(
    ir: PcbIR,
    parts: tuple[str, ...],
    route_of: dict[int, int],
    pin_decls: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """The 1b ``pin_decls`` blobs as TopoDesign fields (Decisions 11–13):
    named fixed ``claims``, ``any_of`` ``slots`` and ``swap_groups``, with
    the decl-referenced nets collected into ``decl_nets`` so a slot's
    ``net`` index also reaches plane and singleton members ``net_pins``
    drops. Pin LABELS translate through ``ir.pin_label`` — a label that
    is not a live pin raises :class:`PinConflict`."""
    by_inst: dict[int, Mapping[str, Any]] = {}
    if pin_decls is not None:
        ref_to_i = {r: i for i, r in enumerate(parts)}
        for refdes, decl in pin_decls.items():
            if decl is not None and str(refdes) in ref_to_i:
                by_inst[ref_to_i[str(refdes)]] = decl
    elif ir.inst_pin_decls is not None:
        for i, decl in enumerate(ir.inst_pin_decls):
            if decl:
                by_inst[i] = decl

    pin_of: dict[tuple[int, str], int] = {
        (int(ir.pin_instance[pid]), str(ir.pin_label[pid])): pid
        for pid in range(ir.n_pins)
    }
    net_id_of = {str(n): k for k, n in enumerate(ir.net_name)}

    nets: list[np.ndarray] = []
    net_ir: list[int] = []
    net_route: list[int] = []
    net_plane: list[bool] = []
    net_name: list[str] = []
    decl_of_ir: dict[int, int] = {}

    def decl_net(ir_net: int | None, name: str) -> int:
        if ir_net is not None and ir_net in decl_of_ir:
            return decl_of_ir[ir_net]
        j = len(nets)
        if ir_net is None:
            nets.append(np.zeros(0, dtype=np.int64))
            net_ir.append(-1)
            net_route.append(-1)
            net_plane.append(False)
        else:
            nets.append(np.flatnonzero(ir.pin_net == ir_net).astype(np.int64))
            net_ir.append(ir_net)
            net_route.append(route_of.get(ir_net, -1))
            net_plane.append(
                str(ir.net_class[ir_net] or "").strip().lower()
                in ratsnest.PLANE_CLASSES
            )
        net_name.append(name)
        if ir_net is not None:
            decl_of_ir[ir_net] = j
        return j

    def pin_id(inst: int, label: Any) -> int:
        pid = pin_of.get((inst, str(label)))
        if pid is None:
            raise PinConflict(
                f"{parts[inst]}.{label}: pin_decls names a pin that is "
                "not live on the part — declare it or use a footprint "
                "pad name"
            )
        return pid

    flex: list[FlexSlot] = []
    swap_groups: list[SwapGroup] = []
    claims: list[tuple[int, str]] = []
    pin_functions: list[dict[int, tuple[str, ...]]] = [{} for _ in parts]
    for i in sorted(by_inst):
        decl = by_inst[i]
        funcs = decl.get("pin_functions") or {}

        # The table is informational for every pin of the part — labels
        # that never became live pins (undeclared, no pad) resolve to
        # nothing and drop out; a slot's candidates are checked through
        # ``pin_id`` separately, so this cannot mask a decl typo.
        def _funcs_of(rec: Any) -> tuple[str, ...]:
            fl = rec.get("functions") if isinstance(rec, Mapping) else rec
            return tuple(str(f) for f in (fl or ()))

        pin_functions[i] = {
            pid: _funcs_of(funcs.get(label))
            for label in funcs
            if (pid := pin_of.get((i, str(label)))) is not None
        }
        for c in decl.get("claims") or []:
            claims.append((pin_id(i, c["pin"]), str(c["name"])))
        for s in decl.get("slots") or []:
            labels = [str(p) for p in s.get("pins") or []]
            pins = tuple(pin_id(i, p) for p in labels)
            net_name_s = s.get("net")
            j = decl_net(
                net_id_of.get(str(net_name_s)) if net_name_s else None,
                str(net_name_s or ""),
            )
            ratings = tuple(
                float(v)
                if (v := (funcs.get(p) or {}).get("max_current_a")) is not None
                else math.nan
                for p in labels
            )
            flex.append(
                FlexSlot(
                    part=i,
                    pins=pins,
                    net=j,
                    name=s.get("name"),
                    site=s.get("site"),
                    requirement_a=(
                        float(s["requirement_a"])
                        if s.get("requirement_kind") == "max"
                        and s.get("requirement_a") is not None
                        else None
                    ),
                    pin_max_a=ratings,
                )
            )
        for g in decl.get("swap_groups") or []:
            members = tuple(
                tuple(pin_id(i, p) for p in unit) for unit in g.get("members") or []
            )
            for unit in members:
                for p in unit:
                    n = int(ir.pin_net[p])
                    if n != NO_NET:
                        decl_net(n, str(ir.net_name[n]))
            swap_groups.append(
                SwapGroup(part=i, members=members, tuple_=bool(g.get("tuple")))
            )

    return {
        "flex": tuple(flex),
        "swap_groups": tuple(swap_groups),
        "claims": tuple(claims),
        "nets": tuple(nets),
        "net_ir": np.asarray(net_ir, dtype=np.int32),
        "net_route": np.asarray(net_route, dtype=np.int64),
        "net_plane": np.asarray(net_plane, dtype=bool),
        "net_name": tuple(net_name),
        "pin_functions": tuple(pin_functions),
    }


#: Sentinel for "this pin connects nothing" — mirrors ``ir.NO_NET`` so
#: 1b slot code need not import the IR module for the check.
__all__ = [
    "FREEDOM_BACK_SIDE",
    "FREEDOM_ROTATE",
    "LAYER_FORCE_FREE",
    "LAYER_FORCE_INNER",
    "NO_NET",
    "PRESETS",
    "FlexSlot",
    "ObjectiveShares",
    "SwapGroup",
    "TopoDesign",
    "TopoZone",
    "design_from_ir",
]
