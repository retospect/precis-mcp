"""Problem state, torch-free (spec §State).

A :class:`PrProblem` is the structure-of-arrays view of a board the
prototype solves: static obstacles as core+radius shapes with a layer
set, two-pin wires as ratsnest edges (no Steiner points), the movable
:class:`Parts` pose table (spec §State Parts) and per-net
:class:`NetAttrs` (spec §State Net attributes), and the rule context
every ``required(a, b, layer)`` answer is resolved against.
Everything here is numpy; torch tensors are built downstream (the host
venv has no torch).

The :class:`Parts` table is the datamodel slice-2's free-part solve
needs: ``pos``/``rot`` are variables during 4c relaxation, ``side`` and
the two lock bits gate 4d discrete moves, and the whole table is
capacity-padded with a ``live`` mask and a free list so shapes stay
fixed for compiled kernels (spec §State preamble). Slice 1 relaxes
nothing in it — the pads are fixed — but it is built from the same IR so
the method has one state object across slices.

Pads become cores by shape: circle → a degenerate segment (a point),
rect → an oriented box, obround → the end-centres segment, polygon →
its edges (containment not modelled — a wire slipping inside a large
polygon pad is a known prototype gap, gated by the slice-1 fixtures
which have none). A drilled pad's land occupies EVERY layer, matching
``drc._clearance_items``'s own rule; a non-plated hole would too
(drill-to-copper), but slice 1 has none.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.pcb import realize, rules
from precis.pcb.ir import PcbIR, pin_point


@dataclass(frozen=True, slots=True)
class Obstacles:
    """Static copper as core+radius rows. ``seg_*`` rows are segment
    cores (a point is a degenerate segment); ``box_*`` rows are oriented
    rectangles (``cs`` = (cos θ, sin θ) of the box's x axis), needed
    because a rect pad's corners must block a wire, not just its
    skeleton. ``net`` is the IR net id, −1 for unnetted copper;
    ``lmask`` the bitmask over :attr:`PrProblem.layers` the row sits on.
    """

    seg_xy: np.ndarray  # (S, 4) x0, y0, x1, y1
    seg_r: np.ndarray  # (S,)
    seg_net: np.ndarray  # (S,) int64
    seg_lmask: np.ndarray  # (S,) uint8
    box_c: np.ndarray  # (B, 2) centre
    box_hw: np.ndarray  # (B, 2) half extents
    box_cs: np.ndarray  # (B, 2) cos, sin of the box's x axis
    box_net: np.ndarray  # (B,) int64
    box_lmask: np.ndarray  # (B,) uint8


@dataclass(frozen=True, slots=True)
class WireSpec:
    """One two-pin ratsnest edge. ``a_lmask``/``b_lmask`` are the layer
    sets the terminals' copper occupies — an SMD pad pins its span to
    its own side, a through-hole pin admits every layer."""

    seg_id: int
    net_id: int
    a: tuple[float, float]
    b: tuple[float, float]
    a_lmask: int
    b_lmask: int
    width_mm: float


ROLE_GROUND = "ground"
ROLE_POWER = "power"
ROLE_SIGNAL = "signal"
ROLE_SENSITIVE = "sensitive"

#: Default copper appetite α per role (spec §Net attributes, "Copper
#: appetite"): cost per mm² of the net's copper. Negative pays the solver
#: to widen (ground/power want area; the plane emerges where other nets'
#: spacing stops the growth), ~0 holds a signal at its current minimum,
#: positive shrinks sensitive/fast nets toward minimal copper. These are
#: provisional magnitudes — the spec says the weight is bench-tuned so it
#: stays below a signal's detour cost; :func:`build_net_attrs` takes
#: per-net overrides for when the netlist step supplies real figures.
APPETITE_DEFAULTS = {
    ROLE_GROUND: -1.0,
    ROLE_POWER: -1.0,
    ROLE_SIGNAL: 0.0,
    ROLE_SENSITIVE: 1.0,
}
#: A signal at or above this edge frequency is treated as fast and given
#: positive appetite (spec §Net attributes: "sensitive and fast nets
#: positive α"). Provisional, bench-tuned.
FAST_F_MAX_HZ = 50e6
FAST_APPETITE = 1.0

_GROUND_NAMES = frozenset(
    {"GND", "GROUND", "AGND", "DGND", "PGND", "GNDA", "VSS", "VSSA", "EARTH"}
)
_POWER_TOKENS = ("VCC", "VDD", "VBUS", "VBAT", "VIN", "VOUT", "VDDA", "VAA", "VEE")
_SENSITIVE_HINTS = ("sensitive", "analog", "audio", "rf", "xtal", "crystal", "antenna")


def infer_role(name: str, hint: str | None = None) -> str:
    """Best-effort :data:`ROLE_*` from a net's name and free-text
    ``net_function_hint`` — a heuristic standing in until the netlist step
    emits ``role`` directly (spec §Net attributes). Overridable per net in
    :func:`build_net_attrs`."""
    h = (hint or "").lower()
    if any(k in h for k in _SENSITIVE_HINTS):
        return ROLE_SENSITIVE
    u = name.upper().strip()
    if u in _GROUND_NAMES or u.endswith("GND"):
        return ROLE_GROUND
    if any(tok in u for tok in _POWER_TOKENS):
        return ROLE_POWER
    # A rail spelled as a voltage: "+3V3", "3V3", "5V", "-12V".
    core = u.lstrip("+-")
    if core[:1].isdigit() and "V" in core:
        return ROLE_POWER
    return ROLE_SIGNAL


def default_appetite(role: str, f_max: float = 0.0, edge_rate: float = 0.0) -> float:
    """Copper appetite α from a net's role, bumped positive for a fast net
    (``f_max`` over :data:`FAST_F_MAX_HZ`, or any declared edge rate) so it
    stays minimal-copper — but never flipping ground/power off wanting
    area. Spec §Net attributes."""
    base = APPETITE_DEFAULTS.get(role, 0.0)
    fast = f_max >= FAST_F_MAX_HZ or edge_rate > 0.0
    if fast and base >= 0.0:
        return max(base, FAST_APPETITE)
    return base


@dataclass(frozen=True, slots=True)
class NetAttrs:
    """Per-net electrical attributes, index-aligned with the IR's nets
    (spec §State Net attributes). ``role`` drives the default appetite;
    ``appetite`` α is the cost per mm² the copper-area energy term charges
    (spec §Net attributes). ``v_min``/``v_max`` are working-voltage bounds
    (volts) and ``f_max`` the top signal frequency (Hz); both default to 0
    until the netlist step supplies them, carried here so the structure is
    complete across slices."""

    v_min: np.ndarray  # (n_nets,) float64
    v_max: np.ndarray  # (n_nets,) float64
    f_max: np.ndarray  # (n_nets,) float64
    role: tuple[str, ...]  # (n_nets,) one of ROLE_*
    appetite: np.ndarray  # (n_nets,) float64, α

    @property
    def n_nets(self) -> int:
        return len(self.role)


def build_net_attrs(
    ir: PcbIR,
    net_name: tuple[str, ...],
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> NetAttrs:
    """Resolve :class:`NetAttrs` off a built IR. ``role`` is inferred from
    the net name and ``net_function_hint``; ``appetite`` from the role and
    any declared edge rate (``net_edge_rate_v_per_ns``). ``v_min``,
    ``v_max`` and ``f_max`` default to 0 — the IR carries no net
    working-voltage or frequency column today (`net_current_a` and edge
    rate are the electricals that exist). ``overrides`` maps a net name to
    any of ``v_min``/``v_max``/``f_max``/``role``/``appetite``, the hook
    for the netlist step's datasheet-sourced figures."""
    overrides = overrides or {}
    n = len(net_name)
    edge = ir.net_edge_rate_v_per_ns
    has_edge = getattr(edge, "shape", (0,))[0] == n
    hints = ir.net_function_hint
    has_hint = getattr(hints, "shape", (0,))[0] == n
    v_min = np.zeros(n, dtype=np.float64)
    v_max = np.zeros(n, dtype=np.float64)
    f_max = np.zeros(n, dtype=np.float64)
    appetite = np.zeros(n, dtype=np.float64)
    roles: list[str] = []
    for i, name in enumerate(net_name):
        ov = overrides.get(name, {})
        hint = str(hints[i]) if has_hint and hints[i] is not None else None
        role = str(ov["role"]) if "role" in ov else infer_role(name, hint)
        roles.append(role)
        v_min[i] = float(ov.get("v_min", 0.0))
        v_max[i] = float(ov.get("v_max", 0.0))
        f_max[i] = float(ov.get("f_max", 0.0))
        er = float(edge[i]) if has_edge and np.isfinite(edge[i]) else 0.0
        appetite[i] = (
            float(ov["appetite"])
            if "appetite" in ov
            else default_appetite(role, f_max[i], er)
        )
    return NetAttrs(v_min, v_max, f_max, tuple(roles), appetite)


@dataclass(slots=True)
class Parts:
    """Movable parts as a structure-of-arrays (spec §State Parts),
    capacity-padded with a ``live`` mask so shapes stay fixed for compiled
    kernels; freed rows go on :attr:`free` for reuse by a 4d discrete move.
    ``pos`` is the centre (mm, NaN row = unplaced), ``rot`` the (cos θ,
    sin θ) of the authored rotation (mirroring is carried by ``side``, not
    folded into ``rot`` — ``pin_point`` mirrors before rotating), ``side``
    True = bottom, ``cluster`` the hierarchy parent (−1 = board; the block
    tree is slice-2+)."""

    pos: np.ndarray  # (cap, 2) float64
    rot: np.ndarray  # (cap, 2) float64 (cos, sin)
    side: np.ndarray  # (cap,) bool, True = bottom
    lock_xy: np.ndarray  # (cap,) bool  (ir.inst_fixed_xy)
    lock_rot: np.ndarray  # (cap,) bool  (ir.inst_fixed_rot)
    cluster: np.ndarray  # (cap,) int64, -1 = board
    live: np.ndarray  # (cap,) bool
    refdes: np.ndarray  # (cap,) object -> str | None
    free: list[int]  # dead row ids, ready to alloc

    @property
    def capacity(self) -> int:
        return int(self.live.shape[0])

    @property
    def n_live(self) -> int:
        return int(self.live.sum())

    def live_ids(self) -> np.ndarray:
        return np.nonzero(self.live)[0]

    def alloc(
        self,
        *,
        pos: tuple[float, float],
        rot_deg: float = 0.0,
        side: bool = False,
        lock_xy: bool = False,
        lock_rot: bool = False,
        cluster: int = -1,
        refdes: str | None = None,
    ) -> int:
        """Claim a dead row, returning its id. Raises if the table is at
        capacity — kernels run on fixed shapes, so grow ``capacity`` at
        build time rather than here."""
        if not self.free:
            raise IndexError("Parts at capacity; raise capacity at build time")
        i = self.free.pop()
        self.pos[i] = pos
        self.rot[i] = (math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg)))
        self.side[i] = side
        self.lock_xy[i] = lock_xy
        self.lock_rot[i] = lock_rot
        self.cluster[i] = cluster
        self.refdes[i] = refdes
        self.live[i] = True
        return i

    def free_row(self, i: int) -> None:
        """Retire a live row to the free list (a 4d relocate that removes a
        part). Idempotent-unsafe: freeing a dead row is a bug."""
        if not self.live[i]:
            raise IndexError(f"row {i} is already dead")
        self.live[i] = False
        self.refdes[i] = None
        self.pos[i] = np.nan
        self.free.append(i)


def build_parts(ir: PcbIR, *, capacity: int | None = None) -> Parts:
    """The :class:`Parts` table off a built IR: one live row per instance
    in IR order, padded to ``capacity`` dead rows (default: no slack) on
    the free list. ``pos`` copies ``inst_x``/``inst_y`` verbatim, so an
    unplaced instance stays NaN."""
    n = ir.n_instances
    cap = n if capacity is None else max(int(capacity), n)
    pos = np.full((cap, 2), np.nan, dtype=np.float64)
    pos[:n, 0] = np.asarray(ir.inst_x, dtype=np.float64)
    pos[:n, 1] = np.asarray(ir.inst_y, dtype=np.float64)
    theta = np.radians(np.asarray(ir.inst_rot, dtype=np.float64))
    rot = np.zeros((cap, 2), dtype=np.float64)
    rot[:n, 0] = np.cos(theta)
    rot[:n, 1] = np.sin(theta)
    side = np.zeros(cap, dtype=bool)
    side[:n] = np.asarray(ir.inst_bottom, dtype=bool)
    lock_xy = np.zeros(cap, dtype=bool)
    lock_xy[:n] = np.asarray(ir.inst_fixed_xy, dtype=bool)
    lock_rot = np.zeros(cap, dtype=bool)
    lock_rot[:n] = np.asarray(ir.inst_fixed_rot, dtype=bool)
    cluster = np.full(cap, -1, dtype=np.int64)
    live = np.zeros(cap, dtype=bool)
    live[:n] = True
    refdes = np.empty(cap, dtype=object)
    refdes[:n] = [str(r) for r in ir.instance_refdes]
    # Dead rows, lowest-id first out of pop().
    free = list(range(cap - 1, n - 1, -1))
    return Parts(pos, rot, side, lock_xy, lock_rot, cluster, live, refdes, free)


@dataclass(frozen=True, slots=True)
class PrProblem:
    """The whole slice-1 instance. Distances in mm; ``layers`` is the
    stackup's copper layer names in order (index = layer id)."""

    layers: tuple[str, ...]
    outline: np.ndarray  # (V, 2) convex polygon, CCW
    edge_mm: float  # copper-to-board-edge clearance
    clearance_mm: float  # house tier fallback (per-pair use required_mm)
    track_mm: float  # widest track any wire wants (graph pricing basis)
    via_dia_mm: float
    via_drill_mm: float
    wires: tuple[WireSpec, ...]
    obs: Obstacles
    pads: tuple[dict[str, Any], ...]  # gerber pad rows (topo + DRC model)
    ctx: rules.RuleContext
    net_rules: dict[str, rules.NetRules]
    net_name: tuple[str, ...]  # net id -> name, index-aligned with the IR
    parts: Parts  # movable-part pose table (spec §State Parts)
    net_attrs: NetAttrs  # per-net v/f/role/appetite (spec §State Net attributes)

    @property
    def n_layers(self) -> int:
        return len(self.layers)

    def required_mm(self, net_a: int, net_b: int, layer: int) -> float:
        """The warn-tier spacing net ``net_a`` owes net ``net_b`` on
        ``layer`` — ``rules.required`` resolved (net class, pair rules,
        voltage tiers). Same net merges (0)."""
        if net_a >= 0 and net_a == net_b:
            return 0.0
        name_a = self.net_name[net_a] if net_a >= 0 else ""
        name_b = self.net_name[net_b] if net_b >= 0 else ""
        req = rules.required(
            rules.Owner(name_a), rules.Owner(name_b), self.layers[layer], self.ctx
        )
        return float(req.warn_mm if req.warn_mm is not None else req.error_mm)


def _pad_lmask(pad: dict[str, Any], layer_index: dict[str, int]) -> int:
    """Bitmask of the copper layers one pad row occupies: drilled (plated)
    pads span every layer, an SMD pad only its side's outer layer."""
    if pad.get("drill"):
        return (1 << len(layer_index)) - 1
    bit = layer_index.get(str(pad.get("layer")))
    return 0 if bit is None else (1 << bit)


def _obstacle_rows(
    pad: dict[str, Any],
    net_id: int,
    lmask: int,
) -> tuple[list[tuple[float, float, float, float, float]], list[tuple[float, ...]]]:
    """One gerber pad row as ``(seg cores, box cores)`` — the slice-1
    core+radius table. Seg rows carry ``(x0, y0, x1, y1, r)``; a box row
    is ``(cx, cy, hw, hh, theta)``."""
    segs: list[tuple[float, float, float, float, float]] = []
    boxes: list[tuple[float, ...]] = []
    shape = str(pad.get("shape", "circle"))
    x, y = float(pad["x"]), float(pad["y"])
    w = float(pad.get("w", 0.0))
    h = float(pad.get("h", w))
    if w <= 0 or h <= 0:
        return segs, boxes
    if shape == "polygon":
        poly = pad.get("poly") or []
        for i, p in enumerate(poly):
            q = poly[(i + 1) % len(poly)]
            segs.append((float(p[0]), float(p[1]), float(q[0]), float(q[1]), 0.0))
        return segs, boxes
    if shape == "circle":
        segs.append((x, y, x, y, w / 2.0))
    elif shape == "rect":
        boxes.append((x, y, w / 2.0, h / 2.0, 0.0))
    elif shape == "obround":
        if w == h:
            segs.append((x, y, x, y, w / 2.0))
        elif w > h:
            half = (w - h) / 2.0
            segs.append((x - half, y, x + half, y, h / 2.0))
        else:
            half = (h - w) / 2.0
            segs.append((x, y - half, x, y + half, w / 2.0))
    else:
        raise ValueError(f"unknown pad shape {shape!r}")
    # A slotted hole's barrel: the slot's own stadium, on every layer.
    if pad.get("slot"):
        (ax, ay), (bx, by) = pad["slot"][:2]
        segs.append(
            (float(ax), float(ay), float(bx), float(by), float(pad["drill"]) / 2.0)
        )
    return segs, boxes


def build_problem(
    ir: PcbIR,
    *,
    outline: list[list[float]],
    config: realize.RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
) -> PrProblem:
    """The slice-1 problem off a built IR: every 2-pin segment a wire,
    every pad a static shape, rules resolved the realize way."""
    layers = tuple(str(l["name"]) for l in ir.stackup)
    layer_index = {name: i for i, name in enumerate(layers)}
    class_rules = config.class_rules or {}
    net_name = tuple(str(ir.net_name[i]) for i in range(ir.n_nets))
    net_rules = {
        name: rules.resolve_net_rules(
            str(ir.net_class[i]),
            layer_is_outer=True,
            fab_caps=config.fab_caps,
            overrides=class_rules.get(str(ir.net_class[i])),
        )
        for i, name in enumerate(net_name)
    }
    ctx = rules.RuleContext(
        fab_caps=config.fab_caps,
        net_rules=net_rules,
        outer_layers=frozenset({layers[0], layers[-1]}),
        coated=True,
    )
    pads = realize.pads_for_ir(ir, list(layers), footprints)
    name_to_net = {name: i for i, name in enumerate(net_name)}
    seg_rows: list[tuple[float, float, float, float, float]] = []
    box_rows: list[tuple[float, ...]] = []
    seg_meta: list[tuple[int, int]] = []
    box_meta: list[tuple[int, int]] = []
    pad_by_pin: dict[tuple[str, str], dict[str, Any]] = {}
    for pad in pads:
        net_id = name_to_net.get(str(pad.get("net") or ""), -1)
        lmask = _pad_lmask(pad, layer_index)
        segs, boxes = _obstacle_rows(pad, net_id, lmask)
        seg_rows += segs
        seg_meta += [(net_id, lmask)] * len(segs)
        box_rows += boxes
        box_meta += [(net_id, lmask)] * len(boxes)
        pad_by_pin.setdefault((str(pad.get("refdes")), str(pad.get("pin"))), pad)
    geoms = realize.pad_geometry(ir, footprints)

    def terminal_lmask(pin_id: int) -> int:
        inst = int(ir.pin_instance[pin_id])
        pad = pad_by_pin.get((str(ir.instance_refdes[inst]), str(ir.pin_label[pin_id])))
        if pad is not None:
            return _pad_lmask(pad, layer_index)
        # No pad row (unplaced or net-less): a drilled pad still sits on
        # every layer, else the instance's own side — same rule pads use.
        if geoms[pin_id].drill_mm:
            return (1 << len(layers)) - 1
        return 1 << (len(layers) - 1 if bool(ir.inst_bottom[inst]) else 0)

    wires: list[WireSpec] = []
    for seg_id in range(ir.n_segments):
        pa, pb = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        a_pt, b_pt = pin_point(ir, pa), pin_point(ir, pb)
        if a_pt is None or b_pt is None:
            continue
        net_id = int(ir.seg_net[seg_id])
        wires.append(
            WireSpec(
                seg_id=seg_id,
                net_id=net_id,
                a=a_pt,
                b=b_pt,
                a_lmask=terminal_lmask(pa),
                b_lmask=terminal_lmask(pb),
                width_mm=net_rules[net_name[net_id]].track_width_mm,
            )
        )
    # One via geometry for the prototype: the class's resolved figure.
    diams = {(r.via_dia_mm, r.via_drill_mm) for r in net_rules.values()}
    via_dia_mm = max(d[0] for d in diams if d[0] is not None) if diams else 0.0
    via_drill_mm = max(d[1] for d in diams if d[1] is not None) if diams else 0.0
    obs = Obstacles(
        seg_xy=np.asarray(seg_rows, dtype=np.float64)[:, :4]
        if seg_rows
        else np.zeros((0, 4)),
        seg_r=np.asarray([r[4] for r in seg_rows], dtype=np.float64)
        if seg_rows
        else np.zeros(0),
        seg_net=np.asarray([m[0] for m in seg_meta], dtype=np.int64)
        if seg_rows
        else np.zeros(0, dtype=np.int64),
        seg_lmask=np.asarray([m[1] for m in seg_meta], dtype=np.uint8)
        if seg_rows
        else np.zeros(0, dtype=np.uint8),
        box_c=np.asarray([r[:2] for r in box_rows], dtype=np.float64)
        if box_rows
        else np.zeros((0, 2)),
        box_hw=np.asarray([r[2:4] for r in box_rows], dtype=np.float64)
        if box_rows
        else np.zeros((0, 2)),
        box_cs=np.asarray(
            [(math.cos(r[4]), math.sin(r[4])) for r in box_rows], dtype=np.float64
        )
        if box_rows
        else np.zeros((0, 2)),
        box_net=np.asarray([m[0] for m in box_meta], dtype=np.int64)
        if box_rows
        else np.zeros(0, dtype=np.int64),
        box_lmask=np.asarray([m[1] for m in box_meta], dtype=np.uint8)
        if box_rows
        else np.zeros(0, dtype=np.uint8),
    )
    return PrProblem(
        layers=layers,
        outline=np.asarray(outline, dtype=np.float64),
        edge_mm=realize._board_edge_min_mm(config),
        clearance_mm=max(r.clearance_mm for r in net_rules.values()),
        track_mm=max(w.width_mm for w in wires) if wires else 0.0,
        via_dia_mm=via_dia_mm,
        via_drill_mm=via_drill_mm,
        wires=tuple(wires),
        obs=obs,
        pads=tuple(pads),
        ctx=ctx,
        net_rules=net_rules,
        net_name=net_name,
        parts=build_parts(ir),
        net_attrs=build_net_attrs(ir, net_name),
    )
