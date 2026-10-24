"""Problem state, torch-free (spec §State).

A :class:`PrProblem` is the structure-of-arrays view of a board the
prototype solves: static obstacles as core+radius shapes with a layer
set, two-pin wires as ratsnest edges (no Steiner points), the movable
:class:`Parts` pose table (spec §State Parts), per-net
:class:`NetAttrs` (spec §State Net attributes), the per-net current trees
(:mod:`precis.pcb.pr.trees`, spec §State "Nets as trees"), and the rule
context every ``required(a, b, layer)`` answer is resolved against.
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
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.pcb import realize, rules
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    PcbIR,
    instance_courtyard_polygons,
    pin_point,
)
from precis.pcb.pr import trees
from precis.pcb.pr.trees import NetTree

#: Half-extent of the courtyard square a pinless movable instance (a
#: mounting hole, a fiducial) reserves when its footprint gives no pads —
#: the placer's own fallback (``optimize.COURTYARD_MIN_SEPARATION_MM / 2``).
_PINLESS_COURT_HALF_MM = 0.5


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
    """One two-pin ratsnest edge — one tree edge of its net (spec §State
    "Nets as trees"; the IR's star decomposition is the seed tree).
    ``a_lmask``/``b_lmask`` are the layer sets the terminals' copper
    occupies — an SMD pad pins its span to its own side, a through-hole
    pin admits every layer. ``a_part``/``b_part`` are the movable-part
    rows (:class:`DynGeom` index, −1 = a fixed terminal) the endpoints
    ride on, ``a_local``/``b_local`` the pin offsets in that part's
    (pre-mirrored) local frame, so the relaxer derives the endpoint from
    the pose instead of holding it fixed. ``width_mm`` is the edge's
    width floor: the class track width raised to the IPC-2221 width its
    tree-edge current demands (:mod:`precis.pcb.pr.trees`)."""

    seg_id: int
    net_id: int
    a: tuple[float, float]
    b: tuple[float, float]
    a_lmask: int
    b_lmask: int
    width_mm: float
    a_part: int = -1
    b_part: int = -1
    a_local: tuple[float, float] = (0.0, 0.0)
    b_local: tuple[float, float] = (0.0, 0.0)
    #: Layers a span of this wire may run on: the board's routable
    #: (signal) layers intersected with the net class's ``layers`` lock
    #: (``realize._net_class_layers``). The end spans keep their pad's
    #: layer (a pad→via stub), as vroute's terminals do.
    route_lmask: int = -1


@dataclass(frozen=True, slots=True)
class DynGeom:
    """Geometry that rides on movable-part poses (spec §Where parts move,
    4c: "poses are variables beside the wires"). Every row is in its
    part's PRE-MIRRORED local frame: world = centre + R_cw(θ)·local, the
    board's clockwise-from-north convention (:mod:`precis.pcb.pose`),
    so a bottom part's mirror is baked into the local coordinates and
    never a variable (side changes only by a 4d move, not in v1).

    Parts: ``part_inst`` the IR instance id per row, ``pose0`` the
    starting (x, y, θ_rad) with θ positive clockwise, the two lock bits,
    ``side`` 0 top / 1 bottom / 2 both (a through-hole part blocks both
    sides, spec §4-0), and ``group`` the block (hierarchy level, spec
    §Hierarchy: an authored IR group, else the part itself).

    Courtyards are oriented boxes: ``court_c``/``court_hw`` the local
    centre and half extents (the courtyard hull's local bounding box —
    over-covering, the safe direction). Pad copper is ``dseg_*`` (segment
    cores, a point is a degenerate segment) and ``dbox_*`` (oriented
    boxes with local axis angle ``dbox_phi``, math-CCW radians); the
    world axis of a local box is ``phi − θ``.
    """

    part_inst: np.ndarray  # (M,) int64
    pose0: np.ndarray  # (M, 3) x, y, theta_rad (CW+)
    lock_xy: np.ndarray  # (M,) bool
    lock_rot: np.ndarray  # (M,) bool
    side: np.ndarray  # (M,) int8: 0 top, 1 bottom, 2 both
    group: np.ndarray  # (M,) int64, block id (0..G-1)
    court_c: np.ndarray  # (M, 2)
    court_hw: np.ndarray  # (M, 2)
    dseg_part: np.ndarray  # (Sd,) int64
    dseg_local: np.ndarray  # (Sd, 4)
    dseg_r: np.ndarray  # (Sd,)
    dseg_net: np.ndarray  # (Sd,) int64
    dseg_lmask: np.ndarray  # (Sd,) uint8
    dbox_part: np.ndarray  # (Bd,) int64
    dbox_c: np.ndarray  # (Bd, 2) local centre
    dbox_hw: np.ndarray  # (Bd, 2)
    dbox_phi: np.ndarray  # (Bd,) local axis angle, rad
    dbox_net: np.ndarray  # (Bd,) int64
    dbox_lmask: np.ndarray  # (Bd,) uint8
    #: Courtyards of the LOCKED placed instances, in world coordinates
    #: (``scourt_cs`` the box x-axis direction, math-CCW): static
    #: obstacles a movable courtyard keeps clear of.
    scourt_c: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    scourt_hw: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    scourt_cs: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    scourt_side: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int8))

    @property
    def n_parts(self) -> int:
        return int(self.part_inst.shape[0])

    @property
    def n_groups(self) -> int:
        return int(self.group.max()) + 1 if self.n_parts else 0


def _empty_dyn() -> DynGeom:
    z = np.zeros
    return DynGeom(
        part_inst=z(0, dtype=np.int64),
        pose0=z((0, 3)),
        lock_xy=z(0, dtype=bool),
        lock_rot=z(0, dtype=bool),
        side=z(0, dtype=np.int8),
        group=z(0, dtype=np.int64),
        court_c=z((0, 2)),
        court_hw=z((0, 2)),
        dseg_part=z(0, dtype=np.int64),
        dseg_local=z((0, 4)),
        dseg_r=z(0),
        dseg_net=z(0, dtype=np.int64),
        dseg_lmask=z(0, dtype=np.uint8),
        dbox_part=z(0, dtype=np.int64),
        dbox_c=z((0, 2)),
        dbox_hw=z((0, 2)),
        dbox_phi=z(0),
        dbox_net=z(0, dtype=np.int64),
        dbox_lmask=z(0, dtype=np.uint8),
    )


def world_to_local(
    px: float, py: float, cx: float, cy: float, theta: float
) -> tuple[float, float]:
    """Inverse of ``world = c + R_cw(θ)·local`` (:func:`precis.pcb.pose.
    rotate_cw`): the pre-mirrored local coordinates of a world point."""
    dx, dy = px - cx, py - cy
    c, s = math.cos(theta), math.sin(theta)
    return dx * c - dy * s, dx * s + dy * c


def local_to_world(
    lx: float, ly: float, cx: float, cy: float, theta: float
) -> tuple[float, float]:
    c, s = math.cos(theta), math.sin(theta)
    return cx + lx * c + ly * s, cy - lx * s + ly * c


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
    #: Per-net current tree, net id -> :class:`~precis.pcb.pr.trees.NetTree`
    #: (spec §State "Nets as trees"): the IR's star decomposition with its
    #: per-edge current and the width that current demands. Single-pin nets
    #: have no entry. Slice 1 routes two-pin :attr:`wires` and does not read
    #: it; the free-part solve (slice 2+) re-plans the tree in a 4d move.
    net_trees: dict[int, NetTree] = field(default_factory=dict)
    #: Movable-part geometry (slice 2, spec §Where parts move). Empty when
    #: the problem was built with ``free_parts=False`` (slice 1: every pad
    #: a static obstacle).
    dyn: DynGeom = field(default_factory=_empty_dyn)
    #: ``(net_a, net_b, layer) -> required mm`` memo: the relaxer asks for
    #: every wire pair × layer and there are far fewer net pairs than wire
    #: pairs. A dict field on a frozen dataclass is mutable by design here.
    _req_cache: dict[tuple[int, int, int], float] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def n_layers(self) -> int:
        return len(self.layers)

    @property
    def free_parts(self) -> bool:
        return self.dyn.n_parts > 0

    def required_mm(self, net_a: int, net_b: int, layer: int) -> float:
        """The warn-tier spacing net ``net_a`` owes net ``net_b`` on
        ``layer`` — ``rules.required`` resolved (net class, pair rules,
        voltage tiers). Same net merges (0). Memoised per net pair."""
        if net_a >= 0 and net_a == net_b:
            return 0.0
        key = (net_a, net_b, layer) if net_a <= net_b else (net_b, net_a, layer)
        hit = self._req_cache.get(key)
        if hit is not None:
            return hit
        name_a = self.net_name[net_a] if net_a >= 0 else ""
        name_b = self.net_name[net_b] if net_b >= 0 else ""
        req = rules.required(
            rules.Owner(name_a), rules.Owner(name_b), self.layers[layer], self.ctx
        )
        val = float(req.warn_mm if req.warn_mm is not None else req.error_mm)
        self._req_cache[key] = val
        return val

    def pose_of(self, part_row: int) -> tuple[float, float, float]:
        """A movable part's starting ``(x, y, θ_rad)``."""
        p = self.dyn.pose0[part_row]
        return float(p[0]), float(p[1]), float(p[2])


def write_poses(ir: PcbIR, problem: PrProblem, poses: np.ndarray) -> None:
    """Write relaxed movable-part poses ``(M, 3)`` (x, y, θ_rad CW+) back
    onto the IR with :meth:`PcbIR.move_instance` — the one L3 mutator —
    so the next outer round rebuilds pads, obstacles and the topology
    seed from the moved board (spec §4a: "rebuilt from the current
    geometry every outer round"). Locked axes keep their IR value."""
    dyn = problem.dyn
    for row in range(dyn.n_parts):
        inst = int(dyn.part_inst[row])
        x, y, th = (float(v) for v in poses[row])
        kw: dict[str, float] = {}
        if not dyn.lock_xy[row]:
            kw["x"] = x
            kw["y"] = y
        if not dyn.lock_rot[row]:
            kw["rot"] = math.degrees(th) % 360.0
        if kw:
            ir.move_instance(inst, **kw)


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


def _open_ring(poly: np.ndarray) -> np.ndarray:
    """An authored outline ring often repeats its first vertex last; the
    distance kernel wants the open polygon (a zero-length edge has no
    normal). Also orients it counter-clockwise, which ``outline_margin``
    assumes (positive inside)."""
    poly = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
    if len(poly) >= 2 and np.allclose(poly[0], poly[-1]):
        poly = poly[:-1]
    if len(poly) >= 3:
        x, y = poly[:, 0], poly[:, 1]
        area2 = float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
        if area2 < 0.0:
            poly = poly[::-1].copy()
    return poly


def _movable_rows(ir: PcbIR) -> list[int]:
    """IR instances the free-part solve may move: placed, and not locked
    on both axes."""
    out: list[int] = []
    for i in range(ir.n_instances):
        if math.isnan(float(ir.inst_x[i])) or math.isnan(float(ir.inst_y[i])):
            continue
        if bool(ir.inst_fixed_xy[i]) and bool(ir.inst_fixed_rot[i]):
            continue
        out.append(i)
    return out


def _build_dyn(
    ir: PcbIR,
    pads: list[dict[str, Any]],
    movable: list[int],
    name_to_net: dict[str, int],
    layer_index: dict[str, int],
) -> DynGeom:
    """Local-frame geometry for every movable instance: its pads (the
    same :func:`realize.pads_for_ir` rows, inverse-transformed through the
    instance's current pose so they follow it), its courtyard box, its
    side and block."""
    row_of = {inst: r for r, inst in enumerate(movable)}
    m = len(movable)
    pose0 = np.zeros((m, 3))
    for r, inst in enumerate(movable):
        rot = float(ir.inst_rot[inst])
        pose0[r] = (
            float(ir.inst_x[inst]),
            float(ir.inst_y[inst]),
            math.radians(0.0 if math.isnan(rot) else rot),
        )
    lock_xy = np.array([bool(ir.inst_fixed_xy[i]) for i in movable], dtype=bool)
    lock_rot = np.array([bool(ir.inst_fixed_rot[i]) for i in movable], dtype=bool)
    side = np.array(
        [1 if bool(ir.inst_bottom[i]) else 0 for i in movable], dtype=np.int8
    )
    # Blocks: an authored/pattern IR group is a block; everything else is
    # its own block. Ids are dense 0..G-1.
    gid: dict[int, int] = {}
    group = np.zeros(m, dtype=np.int64)
    for r, inst in enumerate(movable):
        g = int(ir.inst_group[inst]) if ir.inst_group.shape[0] > inst else -1
        key = g if g >= 0 else -(inst + 1)
        group[r] = gid.setdefault(key, len(gid))
    # Courtyards: hull-derived local polygon → local bbox (pre-mirrored).
    courts = instance_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=_PINLESS_COURT_HALF_MM,
    )
    court_c = np.zeros((m, 2))
    court_hw = np.zeros((m, 2))

    def local_box(
        inst: int, mirrored: bool
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        ring = courts[inst]
        if not ring:
            return (0.0, 0.0), (_PINLESS_COURT_HALF_MM, _PINLESS_COURT_HALF_MM)
        xs = np.array([p[0] for p in ring])
        ys = np.array([p[1] for p in ring])
        if mirrored:
            xs = -xs  # mirror before rotate: the pre-mirrored frame
        return (
            (float(xs.max() + xs.min()) / 2.0, float(ys.max() + ys.min()) / 2.0),
            (float(xs.max() - xs.min()) / 2.0, float(ys.max() - ys.min()) / 2.0),
        )

    for r, inst in enumerate(movable):
        court_c[r], court_hw[r] = local_box(inst, side[r] == 1)
    # Locked placed instances: their courtyards are static world boxes.
    sc_c: list[tuple[float, float]] = []
    sc_hw: list[tuple[float, float]] = []
    sc_cs: list[tuple[float, float]] = []
    sc_side: list[int] = []
    for inst in range(ir.n_instances):
        if inst in row_of:
            continue
        x, y = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        if math.isnan(x) or math.isnan(y):
            continue
        rot = float(ir.inst_rot[inst])
        th = math.radians(0.0 if math.isnan(rot) else rot)
        bottom = bool(ir.inst_bottom[inst])
        (lcx, lcy), hw = local_box(inst, bottom)
        sc_c.append(local_to_world(lcx, lcy, x, y, th))
        sc_hw.append(hw)
        sc_cs.append((math.cos(-th), math.sin(-th)))
        sc_side.append(1 if bottom else 0)
    dseg: list[tuple[int, float, float, float, float, float, int, int]] = []
    dbox: list[tuple[int, float, float, float, float, float, int, int]] = []
    refdes_row = {str(ir.instance_refdes[i]): row_of[i] for i in movable}
    static_row = {
        str(ir.instance_refdes[i]): k
        for k, i in enumerate(
            inst for inst in range(ir.n_instances) if inst not in row_of
        )
    }
    for pad in pads:
        if pad.get("drill"):
            sk = static_row.get(str(pad.get("refdes")))
            if sk is not None and sk < len(sc_side):
                sc_side[sk] = 2
        row = refdes_row.get(str(pad.get("refdes")))
        if row is None:
            continue
        r = int(row)
        if pad.get("drill"):
            side[r] = 2  # a through-hole part blocks both sides
        net_id = name_to_net.get(str(pad.get("net") or ""), -1)
        lmask = _pad_lmask(pad, layer_index)
        segs, boxes = _obstacle_rows(pad, net_id, lmask)
        cx, cy, th = pose0[r]
        for x0, y0, x1, y1, rad in segs:
            lx0, ly0 = world_to_local(x0, y0, cx, cy, th)
            lx1, ly1 = world_to_local(x1, y1, cx, cy, th)
            dseg.append((r, lx0, ly0, lx1, ly1, rad, net_id, lmask))
        for bx, by, bhw, bhh, phi in boxes:
            lx, ly = world_to_local(bx, by, cx, cy, th)
            # world axis = phi_local − θ  ⇒  phi_local = phi_world + θ
            dbox.append((r, lx, ly, bhw, bhh, phi + th, net_id, lmask))
    f64 = np.float64
    return DynGeom(
        part_inst=np.asarray(movable, dtype=np.int64),
        pose0=pose0,
        lock_xy=lock_xy,
        lock_rot=lock_rot,
        side=side,
        group=group,
        court_c=court_c,
        court_hw=court_hw,
        dseg_part=np.asarray([d[0] for d in dseg], dtype=np.int64),
        dseg_local=np.asarray([d[1:5] for d in dseg], dtype=f64).reshape(-1, 4),
        dseg_r=np.asarray([d[5] for d in dseg], dtype=f64),
        dseg_net=np.asarray([d[6] for d in dseg], dtype=np.int64),
        dseg_lmask=np.asarray([d[7] for d in dseg], dtype=np.uint8),
        dbox_part=np.asarray([d[0] for d in dbox], dtype=np.int64),
        dbox_c=np.asarray([d[1:3] for d in dbox], dtype=f64).reshape(-1, 2),
        dbox_hw=np.asarray([d[3:5] for d in dbox], dtype=f64).reshape(-1, 2),
        dbox_phi=np.asarray([d[5] for d in dbox], dtype=f64),
        dbox_net=np.asarray([d[6] for d in dbox], dtype=np.int64),
        dbox_lmask=np.asarray([d[7] for d in dbox], dtype=np.uint8),
        scourt_c=np.asarray(sc_c, dtype=f64).reshape(-1, 2),
        scourt_hw=np.asarray(sc_hw, dtype=f64).reshape(-1, 2),
        scourt_cs=np.asarray(sc_cs, dtype=f64).reshape(-1, 2),
        scourt_side=np.asarray(sc_side, dtype=np.int8),
    )


def build_problem(
    ir: PcbIR,
    *,
    outline: list[list[float]],
    config: realize.RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
    free_parts: bool = False,
    net_overrides: dict[str, dict[str, Any]] | None = None,
) -> PrProblem:
    """The problem off a built IR: every 2-pin segment (tree edge) a
    wire, rules resolved the realize way. With ``free_parts=False``
    (slice 1) every pad is a static shape; with ``free_parts=True``
    (slice 2) the pads of placed, not-fully-locked instances ride their
    part's pose (:class:`DynGeom`) and only the rest — locked parts,
    mounting holes — stay static obstacles. Wire widths are the tree
    edge's current-derived floor where the net has a tree."""
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
    movable = _movable_rows(ir) if free_parts else []
    dyn = (
        _build_dyn(ir, pads, movable, name_to_net, layer_index)
        if movable
        else _empty_dyn()
    )
    movable_refdes = {str(ir.instance_refdes[i]) for i in movable}
    row_of_inst = {int(inst): r for r, inst in enumerate(dyn.part_inst)}
    seg_rows: list[tuple[float, float, float, float, float]] = []
    box_rows: list[tuple[float, ...]] = []
    seg_meta: list[tuple[int, int]] = []
    box_meta: list[tuple[int, int]] = []
    pad_by_pin: dict[tuple[str, str], dict[str, Any]] = {}
    for pad in pads:
        pad_by_pin.setdefault((str(pad.get("refdes")), str(pad.get("pin"))), pad)
        if str(pad.get("refdes")) in movable_refdes:
            continue  # rides its part's pose (DynGeom)
        net_id = name_to_net.get(str(pad.get("net") or ""), -1)
        lmask = _pad_lmask(pad, layer_index)
        segs, boxes = _obstacle_rows(pad, net_id, lmask)
        seg_rows += segs
        seg_meta += [(net_id, lmask)] * len(segs)
        box_rows += boxes
        box_meta += [(net_id, lmask)] * len(boxes)
    # Board-level mounting holes: unplated drills, every layer, no net.
    all_layers = (1 << len(layers)) - 1
    for hole in ir.mounting_holes:
        if hole.part is not None:
            continue  # footprint-owned: follows its part, not a board hole
        hx, hy = float(hole.x), float(hole.y)
        r = max(float(hole.drill_mm or 0.0), float(hole.ring_dia_mm or 0.0)) / 2.0
        if r > 0.0:
            seg_rows.append((hx, hy, hx, hy, r))
            seg_meta.append((-1, all_layers))
    geoms = realize.pad_geometry(ir, footprints)
    tree_width: dict[int, float] = {}
    net_trees = trees.build_net_trees(
        ir,
        track_floor_mm={
            i: net_rules[name].track_width_mm for i, name in enumerate(net_name)
        },
    )
    for tree in net_trees.values():
        for e in range(tree.n_edges):
            s = int(tree.edge_seg[e])
            if s >= 0:
                tree_width[s] = float(tree.edge_width_mm[e])

    def anchor(pin_id: int, pt: tuple[float, float]) -> tuple[int, tuple[float, float]]:
        inst = int(ir.pin_instance[pin_id])
        r = row_of_inst.get(inst)
        if r is None:
            return -1, (0.0, 0.0)
        cx, cy, th = dyn.pose0[r]
        return r, world_to_local(pt[0], pt[1], float(cx), float(cy), float(th))

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

    signal = realize._signal_layers(ir)
    all_bits = (1 << len(layers)) - 1
    route_masks: dict[int, int] = {}
    for net_id in range(ir.n_nets):
        allowed, _why = realize._net_class_layers(ir, net_id, config, signal)
        mask = sum(1 << l for l in allowed) if allowed else all_bits
        route_masks[net_id] = mask or all_bits
    wires: list[WireSpec] = []
    for seg_id in range(ir.n_segments):
        pa, pb = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        a_pt, b_pt = pin_point(ir, pa), pin_point(ir, pb)
        if a_pt is None or b_pt is None:
            continue
        net_id = int(ir.seg_net[seg_id])
        a_part, a_local = anchor(pa, a_pt)
        b_part, b_local = anchor(pb, b_pt)
        wires.append(
            WireSpec(
                seg_id=seg_id,
                net_id=net_id,
                a=a_pt,
                b=b_pt,
                a_lmask=terminal_lmask(pa),
                b_lmask=terminal_lmask(pb),
                width_mm=max(
                    net_rules[net_name[net_id]].track_width_mm,
                    tree_width.get(seg_id, 0.0),
                ),
                a_part=a_part,
                b_part=b_part,
                a_local=a_local,
                b_local=b_local,
                route_lmask=route_masks.get(net_id, all_bits),
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
        outline=_open_ring(np.asarray(outline, dtype=np.float64)),
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
        net_attrs=build_net_attrs(ir, net_name, overrides=net_overrides),
        net_trees=net_trees,
        dyn=dyn,
    )
