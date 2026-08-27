"""The PCB intermediate representation — ONE structure, six progressive
enrichment levels (L0-L5). See docs/backlog/pcb-guided-place-route.md
§"The IR: one progressively-enriched structure" for the design rationale;
this module is the "read the spec before coding" foundation slice 3 exists
to ship.

**Why one structure instead of six**: dropping everything above level k
must still leave a valid level-k object, so the optimizer can work as deep
in cheap graph space as it can and descend only when forced. Each level
*decorates* the previous rather than replacing it — L3 positions don't
erase L2's topology, they sit beside it.

**The invariant this module exists to protect: the L2 combinatorial
embedding (rotation order + side choices) is stored EXPLICITLY and is
never, ever derived from L3 coordinates.** If a caller recomputed L2 from
positions on every move, a component move would silently re-derive
topology and the whole point of the layered IR — that a move perturbs
only nearby levels, leaving the sketch intact — evaporates; we would be
back to maze-router behaviour with extra steps. So there is deliberately
**no `compute_embedding(positions)` method on :class:`PcbIR`.** The only
position-aware embedding helper is :func:`propose_rotation_from_positions`,
a free function (not a method — it cannot be reached by `ir.<tab>`) whose
name says "propose", plus :func:`validate_embedding`, which only *checks*
a stored embedding against current positions and never mutates state. A
move never touches L2 storage.

**Arrays, not objects.** Every per-segment/per-instance/per-via field is a
parallel numpy array; a segment (or instance, or via) is an integer index,
not a Python object. An object graph of ``Segment`` instances doesn't
reach the move rates the optimizer needs (10^4-10^7 moves over a real
board) — see the locality budget in the backlog's Engines section.

**Layers are integer indexes, never strings**, in every IR field. The
stackup array (``DEFAULT_STACKUP`` in :mod:`precis.pcb`) is already
ordered, so index *is* identity; ``"F.Cu"`` etc. are an export label only,
looked up by position, and never appear in IR state. A via's layer span is
a contiguous bitmask (bit k set ⇔ the via blocks layer k), so "does this
via block layer k" is a bit test, not a string compare.

**Keepouts and vias are one primitive**: both are layer-masked obstacle
regions (:attr:`PcbIR.via_layer_span`). A via additionally carries
connectivity (:attr:`PcbIR.via_net` ``>= 0``); a keepout connects nothing
(``via_net == NO_NET``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np

#: Sentinel: a pin/segment/via with no net (unconnected / pure keepout).
NO_NET: int = -1
#: Sentinel: a segment/net with no layer assigned yet (L1 not yet decided).
UNSET_LAYER: int = -1


class Level(IntEnum):
    """The six enrichment levels. Ordinal order IS the ladder order — a
    move at level k dirties only levels > k (see :meth:`PcbIR.move_instance`
    et al.), which is exactly why this is an ``IntEnum`` and not a plain
    set of constants: callers compare levels with ``<``/``>``."""

    L0 = 0  # pins + nets (hypergraph)
    L1 = 1  # integer layer per segment; vias as transitions
    L2 = 2  # explicit combinatorial embedding (rotation system + sides)
    L3 = 3  # component x/y/rotation; pin positions
    L4 = 4  # metric annotations: gap capacity, region density
    L5 = 5  # realized copper (arcs, tangents, widths) — realize.py fills this in a later slice


def _obj_array(values: list[Any]) -> np.ndarray:
    """A numpy object array that never gets silently cast to a 2D array of
    characters — the usual ``np.array(list_of_str)`` footgun when the
    strings happen to be equal length."""
    arr = np.empty(len(values), dtype=object)
    for i, v in enumerate(values):
        arr[i] = v
    return arr


@dataclass(slots=True)
class PcbIR:
    """The progressively-enriched IR. Construct via :func:`from_graph`
    (a plain-dict-in build, so this module stays independently
    unit-testable without a DB); mutate via the ``move_*`` / ``set_*``
    methods, which are the *only* sanctioned way to change state — each
    one owns exactly which dirty masks it sets, which is the cascade
    contract every other engine (optimizer, realizer) depends on.

    All levels' arrays are always **allocated** once built — "level" is a
    fidelity a *caller* (an estimator in :mod:`precis.pcb.cost`) chooses to
    look at, not a statement about what data exists. That is what lets one
    IR object serve the coarse-vs-fine admissibility comparison in
    :mod:`precis.pcb.cost` (evaluate the same object twice, once
    restricted to L1 data, once allowed L4 data) without rebuilding it.
    """

    stackup: list[dict[str, Any]]

    # ---- L0: pins + nets (hypergraph) ---------------------------------
    instance_refdes: np.ndarray  # object[n_inst] -> str (opaque id = index; refdes is a late label)
    inst_extended_part: np.ndarray  # bool[n_inst] (JLC "Extended" part fee applies)
    pin_instance: np.ndarray  # int32[n_pins]
    pin_label: np.ndarray  # object[n_pins] -> str (pad name; export label only)
    pin_net: np.ndarray  # int32[n_pins], NO_NET if unconnected
    net_name: np.ndarray  # object[n_nets] -> str
    net_domain: np.ndarray  # object[n_nets] -> str ('electrical'|'fluidic'|'thermal')
    net_class: np.ndarray  # object[n_nets] -> str

    # ---- L1: integer layer per segment; vias as transitions -----------
    seg_net: np.ndarray  # int32[n_seg]
    seg_pin_a: np.ndarray  # int32[n_seg]
    seg_pin_b: np.ndarray  # int32[n_seg]
    seg_layer: np.ndarray  # int8[n_seg], UNSET_LAYER until assigned
    net_plane_layer: np.ndarray  # int8[n_nets], UNSET_LAYER unless promoted to a plane
    via_layer_span: np.ndarray  # uint16[n_via], bitmask
    via_net: np.ndarray  # int32[n_via], NO_NET => pure keepout

    # ---- L2: explicit combinatorial embedding --------------------------
    seg_side: np.ndarray  # int8[n_seg], reserved obstacle-side annotation (vocabulary settles in sketch.py, slice 7)
    rotation_index: np.ndarray  # int32[n_pins + 1], CSR row-ptr into rotation_darts
    rotation_darts: np.ndarray  # int32[?], dart id = seg_id*2 + endpoint (0=a, 1=b); explicit cyclic order per pin

    # ---- L3: component x/y/rotation -------------------------------------
    inst_x: np.ndarray  # float64[n_inst], nan until placed
    inst_y: np.ndarray
    inst_rot: np.ndarray  # degrees
    inst_fixed: np.ndarray  # bool[n_inst]

    # ---- L4: metric annotations -----------------------------------------
    seg_gap_capacity: np.ndarray  # float64[n_seg], strands-that-fit; nan = not yet computed
    seg_region_density: np.ndarray  # float64[n_seg], nan = not yet computed

    # ---- L5: realized copper ---------------------------------------------
    seg_copper_length_mm: np.ndarray  # float64[n_seg], nan until realize.py runs

    # ---- dirty masks: the invalidation cascade, made testable ------------
    dirty_l1: np.ndarray  # bool[n_seg]
    dirty_l2: np.ndarray  # bool[n_seg]
    dirty_l3: np.ndarray  # bool[n_inst]
    dirty_l4: np.ndarray  # bool[n_seg]
    dirty_l5: np.ndarray  # bool[n_seg]

    # ---- derived indices (not stored state; rebuilt at construction) -----
    _segs_of_instance: dict[int, list[int]] = field(default_factory=dict, repr=False)

    # -- sizes --------------------------------------------------------
    @property
    def n_instances(self) -> int:
        return len(self.instance_refdes)

    @property
    def n_pins(self) -> int:
        return len(self.pin_instance)

    @property
    def n_nets(self) -> int:
        return len(self.net_name)

    @property
    def n_segments(self) -> int:
        return len(self.seg_net)

    @property
    def n_vias(self) -> int:
        return len(self.via_layer_span)

    @property
    def n_layers(self) -> int:
        return len(self.stackup)

    # -- mutators: each owns exactly which levels it dirties -----------
    def move_instance(
        self, inst_id: int, *, x: float | None = None, y: float | None = None, rot: float | None = None
    ) -> None:
        """L3 move. Dirties L3 at ``inst_id``; dirties L4/L5 **locally** —
        only the segments whose endpoints touch this instance — never the
        whole board. L1 and L2 are untouched: this is the invariant the
        whole architecture rests on (see the module docstring). A fixed
        instance may still be "moved" here (callers enforce the `fixed`
        policy; the IR itself doesn't referee it — that's a placer
        concern)."""
        if x is not None:
            self.inst_x[inst_id] = x
        if y is not None:
            self.inst_y[inst_id] = y
        if rot is not None:
            self.inst_rot[inst_id] = rot
        self.dirty_l3[inst_id] = True
        for seg_id in self._segs_of_instance.get(inst_id, ()):
            self.dirty_l4[seg_id] = True
            self.dirty_l5[seg_id] = True

    def set_layer(self, seg_id: int, layer: int) -> None:
        """L1 mutator: (re)assign a segment's routing layer. Dirties L1 and
        L4 for that segment (its gap-capacity draw moves to a new layer's
        congestion pool) and L5 (copper must re-realize). L2 and L3 are
        untouched — a layer reassignment doesn't change which side of an
        obstacle a connection takes, nor any component's position."""
        if not (0 <= layer < self.n_layers):
            raise ValueError(f"layer {layer} out of range for a {self.n_layers}-layer stackup")
        self.seg_layer[seg_id] = layer
        self.dirty_l1[seg_id] = True
        self.dirty_l4[seg_id] = True
        self.dirty_l5[seg_id] = True

    def promote_plane(self, net_id: int, layer: int) -> None:
        """L1 mutator: assign ``net_id`` to plane role on ``layer`` — a
        layer-*role* decision (backlog: roles are emergent, not hardcoded).
        Its own segments stop being routed traces (they dog-bone fan out
        instead), so they're dirtied at L1/L4/L5 same as a layer
        reassignment; L3 is untouched (no component moved)."""
        if not (0 <= layer < self.n_layers):
            raise ValueError(f"layer {layer} out of range for a {self.n_layers}-layer stackup")
        self.net_plane_layer[net_id] = layer
        for seg_id in np.flatnonzero(self.seg_net == net_id):
            self.dirty_l1[seg_id] = True
            self.dirty_l4[seg_id] = True
            self.dirty_l5[seg_id] = True

    def demote_plane(self, net_id: int) -> None:
        """Undo :meth:`promote_plane` — same dirty footprint."""
        self.net_plane_layer[net_id] = UNSET_LAYER
        for seg_id in np.flatnonzero(self.seg_net == net_id):
            self.dirty_l1[seg_id] = True
            self.dirty_l4[seg_id] = True
            self.dirty_l5[seg_id] = True

    def set_side(self, seg_id: int, side: int) -> None:
        """L2 mutator: flip which side of an obstacle a connection takes.
        Dirties L2 and L4 (its gap changes) and L5 (geometry follows
        topology) for that segment only. **L0 and L1 stay clean** — this is
        literally the rubber-band property: the topology choice is
        independent of layer assignment and of the netlist itself."""
        self.seg_side[seg_id] = side
        self.dirty_l2[seg_id] = True
        self.dirty_l4[seg_id] = True
        self.dirty_l5[seg_id] = True

    def set_rotation(self, pin_id: int, dart_order: list[int]) -> None:
        """L2 mutator: explicitly store the cyclic order of incident darts
        at ``pin_id`` (a *dart* is ``seg_id*2 + endpoint``). This is the
        **only** way ``rotation_darts`` is populated for a pin besides the
        empty-at-construction default — never derived from position data
        inside this class. Dirties L2 (and downstream L4/L5) for every
        segment incident to this pin."""
        lo, hi = int(self.rotation_index[pin_id]), int(self.rotation_index[pin_id + 1])
        if len(dart_order) != hi - lo:
            raise ValueError(f"pin {pin_id} has {hi - lo} incident darts, got {len(dart_order)}")
        self.rotation_darts[lo:hi] = dart_order
        for dart in dart_order:
            seg_id = dart // 2
            self.dirty_l2[seg_id] = True
            self.dirty_l4[seg_id] = True
            self.dirty_l5[seg_id] = True

    def add_via(self, *, layer_span: int, net_id: int = NO_NET) -> int:
        """Append a new via/keepout (:attr:`via_layer_span` bitmask,
        :attr:`via_net`). ``net_id=NO_NET`` (the default) is a pure
        keepout — connects nothing; ``net_id >= 0`` additionally carries
        connectivity for that net. Keepouts and vias are one primitive
        (module docstring) precisely so this is the only constructor
        either needs. No dirty flags: nothing in the IR depended on a via
        that didn't exist yet."""
        via_id = self.n_vias
        self.via_layer_span = np.append(self.via_layer_span, np.uint16(layer_span))
        self.via_net = np.append(self.via_net, np.int32(net_id))
        return via_id

    def clean(self, level: Level, seg_ids: list[int] | None = None) -> None:
        """Acknowledge that an engine consumed the dirty flags at ``level``
        and recomputed — clears them. ``seg_ids=None`` clears every flag at
        that level (L3 clears by instance id instead, since its mask is
        instance-indexed)."""
        mask = {
            Level.L1: self.dirty_l1,
            Level.L2: self.dirty_l2,
            Level.L3: self.dirty_l3,
            Level.L4: self.dirty_l4,
            Level.L5: self.dirty_l5,
        }[level]
        if seg_ids is None:
            mask[:] = False
        else:
            mask[seg_ids] = False


def _build_segments_index(seg_pin_a: np.ndarray, seg_pin_b: np.ndarray, pin_instance: np.ndarray) -> dict[int, list[int]]:
    idx: dict[int, list[int]] = {}
    for seg_id, (pa, pb) in enumerate(zip(seg_pin_a, seg_pin_b)):
        for inst_id in (int(pin_instance[pa]), int(pin_instance[pb])):
            idx.setdefault(inst_id, []).append(seg_id)
    return idx


def from_graph(graph: dict[str, Any], *, stackup: list[dict[str, Any]] | None = None) -> PcbIR:
    """Build an L0 :class:`PcbIR` from the plain-dict graph shape shared
    with :mod:`precis.pcb.eyes` (``{"instances":[...], "nets":[...],
    "unconnected":[...]}``) — no DB, so this stays independently
    unit-testable.

    **Segment decomposition is a star per net** (first member is the hub):
    a design *choice* the netlist records at L0, not something geometry
    dictates — a net's electrical meaning doesn't care which two-pin edges
    represent it, only that they span every member. An MST/Steiner
    alternative is a future move class (`re-root the star`), not a
    correctness requirement of this slice.

    L1 layers, the L2 embedding, and L3 positions are left **unset** (no
    placeholder derived from anything) unless the graph already supplies
    ``x``/``y`` for an instance, matching what the netlist/placement store
    actually knows at hydration time.
    """
    stackup = stackup if stackup is not None else []
    instances = graph.get("instances") or []
    refdes_to_id = {inst["refdes"]: i for i, inst in enumerate(instances)}
    n_inst = len(instances)

    pin_instance: list[int] = []
    pin_label: list[str] = []
    pin_net: list[int] = []
    pin_lookup: dict[tuple[str, str], int] = {}

    def _pin(refdes: str, pin: str, net_id: int) -> int:
        key = (refdes, pin)
        if key in pin_lookup:
            return pin_lookup[key]
        pid = len(pin_instance)
        pin_lookup[key] = pid
        pin_instance.append(refdes_to_id[refdes])
        pin_label.append(pin)
        pin_net.append(net_id)
        return pid

    net_name: list[str] = []
    net_domain: list[str] = []
    net_class: list[str] = []
    seg_net: list[int] = []
    seg_pin_a: list[int] = []
    seg_pin_b: list[int] = []

    for net in graph.get("nets") or []:
        net_id = len(net_name)
        net_name.append(net["name"])
        net_domain.append(net.get("domain") or "electrical")
        net_class.append(net.get("net_class") or "")
        members = net.get("members") or []
        member_pins = [_pin(m["refdes"], m.get("pin") or "1", net_id) for m in members]
        for other in member_pins[1:]:
            seg_net.append(net_id)
            seg_pin_a.append(member_pins[0])
            seg_pin_b.append(other)

    for u in graph.get("unconnected") or []:
        _pin(u["refdes"], u["pin"], NO_NET)

    n_pins = len(pin_instance)
    n_seg = len(seg_net)
    n_nets = len(net_name)

    rotation_index = np.zeros(n_pins + 1, dtype=np.int32)  # empty CSR: no embedding chosen yet

    inst_x = np.full(n_inst, np.nan)
    inst_y = np.full(n_inst, np.nan)
    for inst in instances:
        i = refdes_to_id[inst["refdes"]]
        if inst.get("x") is not None:
            inst_x[i] = float(inst["x"])
        if inst.get("y") is not None:
            inst_y[i] = float(inst["y"])

    ir = PcbIR(
        stackup=stackup,
        instance_refdes=_obj_array([inst["refdes"] for inst in instances]),
        inst_extended_part=np.array([bool(inst.get("extended_part")) for inst in instances], dtype=bool),
        pin_instance=np.array(pin_instance, dtype=np.int32),
        pin_label=_obj_array(pin_label),
        pin_net=np.array(pin_net, dtype=np.int32),
        net_name=_obj_array(net_name),
        net_domain=_obj_array(net_domain),
        net_class=_obj_array(net_class),
        seg_net=np.array(seg_net, dtype=np.int32),
        seg_pin_a=np.array(seg_pin_a, dtype=np.int32),
        seg_pin_b=np.array(seg_pin_b, dtype=np.int32),
        seg_layer=np.full(n_seg, UNSET_LAYER, dtype=np.int8),
        net_plane_layer=np.full(n_nets, UNSET_LAYER, dtype=np.int8),
        via_layer_span=np.zeros(0, dtype=np.uint16),
        via_net=np.zeros(0, dtype=np.int32),
        seg_side=np.zeros(n_seg, dtype=np.int8),
        rotation_index=rotation_index,
        rotation_darts=np.zeros(0, dtype=np.int32),
        inst_x=inst_x,
        inst_y=inst_y,
        inst_rot=np.zeros(n_inst),
        inst_fixed=np.array([(inst.get("fixed") or "") in ("xy", "both") for inst in instances], dtype=bool),
        seg_gap_capacity=np.full(n_seg, np.nan),
        seg_region_density=np.full(n_seg, np.nan),
        seg_copper_length_mm=np.full(n_seg, np.nan),
        dirty_l1=np.zeros(n_seg, dtype=bool),
        dirty_l2=np.zeros(n_seg, dtype=bool),
        dirty_l3=np.zeros(n_inst, dtype=bool),
        dirty_l4=np.zeros(n_seg, dtype=bool),
        dirty_l5=np.zeros(n_seg, dtype=bool),
    )
    ir._segs_of_instance = _build_segments_index(ir.seg_pin_a, ir.seg_pin_b, ir.pin_instance)
    return ir


# ── L2: propose + validate, never define (see module docstring) ────────
def propose_rotation_from_positions(ir: PcbIR) -> dict[int, list[int]]:
    """A **proposal**, not the embedding: for every pin with ≥2 incident
    segment-darts, sort them by the angle (atan2) of the segment's *other*
    endpoint around this pin's L3 position. Returns ``{pin_id: dart_order}``
    for the caller to apply via :meth:`PcbIR.set_rotation` if they choose
    to — deliberately two steps, so no code path can move a component and
    have the embedding change as a side effect.

    Only meaningful once L3 positions exist; pins whose own or whose
    neighbours' positions are unset (nan) are skipped.
    """
    n_pins = ir.n_pins
    darts_at_pin: dict[int, list[int]] = {p: [] for p in range(n_pins)}
    for seg_id in range(ir.n_segments):
        darts_at_pin[int(ir.seg_pin_a[seg_id])].append(seg_id * 2)
        darts_at_pin[int(ir.seg_pin_b[seg_id])].append(seg_id * 2 + 1)

    def _pos(pin_id: int) -> tuple[float, float]:
        inst = int(ir.pin_instance[pin_id])
        return float(ir.inst_x[inst]), float(ir.inst_y[inst])

    proposal: dict[int, list[int]] = {}
    for pin_id, darts in darts_at_pin.items():
        if len(darts) < 2:
            continue
        px, py = _pos(pin_id)
        if math.isnan(px) or math.isnan(py):
            continue

        def _angle(dart: int, px: float = px, py: float = py) -> float:
            seg_id, end = divmod(dart, 2)
            other_pin = int(ir.seg_pin_b[seg_id]) if end == 0 else int(ir.seg_pin_a[seg_id])
            ox, oy = _pos(other_pin)
            if math.isnan(ox) or math.isnan(oy):
                return 0.0
            return math.atan2(oy - py, ox - px)

        proposal[pin_id] = sorted(darts, key=_angle)
    return proposal


def validate_embedding(ir: PcbIR) -> list[int]:
    """Read-only check: which pins' *stored* rotation order (L2) is no
    longer consistent (up to cyclic rotation) with the angular order
    implied by *current* L3 positions. Never mutates state — this is the
    "coordinates validate the embedding" half of the invariant; the
    optimizer decides what to do about a mismatch (re-propose, or treat it
    as a constraint violation), this function only reports it.
    """
    proposal = propose_rotation_from_positions(ir)
    mismatched: list[int] = []
    for pin_id, proposed in proposal.items():
        lo, hi = int(ir.rotation_index[pin_id]), int(ir.rotation_index[pin_id + 1])
        stored = list(ir.rotation_darts[lo:hi])
        if len(stored) != len(proposed):
            mismatched.append(pin_id)
            continue
        if not _same_cyclic_order(stored, proposed):
            mismatched.append(pin_id)
    return mismatched


def _same_cyclic_order(a: list[int], b: list[int]) -> bool:
    """True if list ``b`` is a cyclic rotation of list ``a`` (same darts,
    same relative order, any starting point)."""
    if sorted(a) != sorted(b) or not a:
        return not a and not b
    n = len(a)
    doubled = a + a
    return any(doubled[i : i + n] == b for i in range(n))


# ── Graph feasibility checks (no geometry, no shapely) ──────────────────
# These back `view='drc'` before any geometry exists and later become
# optimizer constraints. Deliberately purely combinatorial: real geometric
# DRC (clearance, courtyard overlap, board edge) is `drc.py`, a later
# module operating on L5 copper.


def unconnected_items(ir: PcbIR) -> list[dict[str, Any]]:
    """Pins on no net, and nets with fewer than 2 distinct pins (a
    "dangling" net — nothing to connect to)."""
    out: list[dict[str, Any]] = []
    for pin_id in range(ir.n_pins):
        if int(ir.pin_net[pin_id]) == NO_NET:
            inst = int(ir.pin_instance[pin_id])
            out.append(
                {
                    "code": "unconnected-pin",
                    "refdes": str(ir.instance_refdes[inst]),
                    "pin": str(ir.pin_label[pin_id]),
                }
            )
    pins_per_net: dict[int, set[int]] = {}
    for pin_id in range(ir.n_pins):
        net_id = int(ir.pin_net[pin_id])
        if net_id != NO_NET:
            pins_per_net.setdefault(net_id, set()).add(pin_id)
    for net_id in range(ir.n_nets):
        n = len(pins_per_net.get(net_id, ()))
        if n < 2:
            out.append({"code": "dangling-net", "net": str(ir.net_name[net_id]), "pins": n})
    return out


def _layer_graph(ir: PcbIR, layer: int) -> tuple[int, int, list[list[int]]]:
    """(vertex count, edge count, connected components as pin-id lists) for
    the subgraph of segments assigned exactly to ``layer``. Vias aren't
    folded in here (a through via bridges every layer, which is the part
    of the problem that genuinely doesn't decompose per layer per the
    backlog — left to the optimizer's constraint set, not this bound)."""
    seg_ids = [s for s in range(ir.n_segments) if int(ir.seg_layer[s]) == layer]
    pins = sorted({int(ir.seg_pin_a[s]) for s in seg_ids} | {int(ir.seg_pin_b[s]) for s in seg_ids})
    adj: dict[int, list[int]] = {p: [] for p in pins}
    for s in seg_ids:
        a, b = int(ir.seg_pin_a[s]), int(ir.seg_pin_b[s])
        adj[a].append(b)
        adj[b].append(a)
    seen: set[int] = set()
    components: list[list[int]] = []
    for p in pins:
        if p in seen:
            continue
        comp = [p]
        seen.add(p)
        stack = [p]
        while stack:
            cur = stack.pop()
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    comp.append(nxt)
                    stack.append(nxt)
        components.append(comp)
    return len(pins), len(seg_ids), components


def same_layer_crossing_bound(ir: PcbIR, layer: int, *, refine: bool = False) -> int:
    """An **admissible lower bound** on the number of same-layer crossings
    a layout of this layer's segment graph requires — never an exact
    count, because exact counting needs the L2 embedding *and* L3
    geometry realize.py doesn't own yet. Uses the classical planar-graph
    edge bound: a simple planar graph on V≥3 vertices has at most 3V-6
    edges, so ``E - (3V - 6)`` edges (when positive) provably cannot be
    drawn without crossing (Euler's formula; this is the same inequality
    behind the crossing-number lower bound literature). That satisfies
    admissibility for free — no geometry needed, no possibility of
    overstating.

    ``refine=False`` (coarse/L1): one bound over the whole layer's graph —
    O(1) after counting V, E. ``refine=True`` (finer/L2): the same bound
    computed **per connected component and summed**, which is provably
    ≥ the coarse bound (components can't share crossings), so it is
    tighter without changing the underlying formula — "estimator fidelity
    increases," not "the cost function changes" (backlog: these must stay
    separate). Still embedding-agnostic; a genuinely exact,
    embedding-aware count is future work once realize.py's face tracing
    exists — noted honestly rather than faked.
    """
    v, e, components = _layer_graph(ir, layer)
    if not refine:
        return _euler_bound(v, e)
    return sum(_euler_bound(len(comp), _edges_in(ir, layer, comp)) for comp in components)


def _euler_bound(v: int, e: int) -> int:
    if v < 3:
        return 0
    return max(0, e - (3 * v - 6))


def _edges_in(ir: PcbIR, layer: int, pins: list[int]) -> int:
    pin_set = set(pins)
    return sum(
        1
        for s in range(ir.n_segments)
        if int(ir.seg_layer[s]) == layer and int(ir.seg_pin_a[s]) in pin_set and int(ir.seg_pin_b[s]) in pin_set
    )


def per_layer_planar(ir: PcbIR, layer: int) -> bool:
    """**Necessary, not sufficient**: True means the Euler edge bound
    doesn't rule out a planar drawing of this layer's segment graph (it
    may still not be planar — genuine planarity testing is a separate,
    finer future refinement); False means it definitely is not planar
    (some crossing is unavoidable, since the bound is proven, not
    heuristic). Uses the refined (per-component) bound, since that's the
    tighter and therefore more useful "definitely not planar" signal."""
    return same_layer_crossing_bound(ir, layer, refine=True) == 0


@dataclass(frozen=True, slots=True)
class PlaneConnectivity:
    """Result of :func:`plane_connectivity`. This is a **topological**
    proxy, not real island detection (that needs polygon geometry —
    ``planes.py``, a later module, owns antipad-cluster-induced splits).
    What it *does* catch honestly and cheaply: a plane-promoted net with
    zero or exactly one stitching via — a single point of failure a real
    board reviewer would flag on sight."""

    net_id: int
    layer: int
    stitch_vias: list[int]
    ok: bool  # False iff stitch_vias has 0 or 1 members


# ── L4: metric annotations (gap capacity, region density) ───────────
# These populate the arrays `cost.py`'s L4 estimators consult — pure
# numpy distance math (no shapely: that stays drc.py's, on L5 copper,
# per the Engines split). O(n_seg . n_inst); fine at this slice's board
# scale, not the production congestion estimator (optimize.py's job once
# it becomes a hot loop inside the optimizer).


def compute_gap_capacity(ir: PcbIR, *, pitch_mm: float = 0.3) -> None:
    """Fill L4 ``seg_gap_capacity`` (strands-that-fit) from L3 positions:
    a segment's binding gap is approximated as the distance from either
    endpoint's instance to the nearest *other* instance, divided by
    ``pitch_mm`` (class trace width + clearance). Segments whose endpoint
    positions are unset are left ``nan`` — genuinely undefined, not
    silently zeroed; :mod:`precis.pcb.cost` is what turns "undefined"
    into an optimistic bound rather than this function guessing.
    """
    for seg_id in range(ir.n_segments):
        gap = _nearest_other_instance_gap(ir, seg_id)
        if gap is not None:
            ir.seg_gap_capacity[seg_id] = math.floor(gap / pitch_mm)


def _nearest_other_instance_gap(ir: PcbIR, seg_id: int) -> float | None:
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    endpoints = {int(ir.pin_instance[a]), int(ir.pin_instance[b])}
    best: float | None = None
    for e in endpoints:
        ex, ey = float(ir.inst_x[e]), float(ir.inst_y[e])
        if math.isnan(ex) or math.isnan(ey):
            return None
        for other in range(ir.n_instances):
            if other in endpoints:
                continue
            ox, oy = float(ir.inst_x[other]), float(ir.inst_y[other])
            if math.isnan(ox) or math.isnan(oy):
                continue
            d = math.hypot(ex - ox, ey - oy)
            if best is None or d < best:
                best = d
    return best


def compute_region_density(ir: PcbIR, *, cell_mm: float = 5.0) -> None:
    """Fill L4 ``seg_region_density``: a RUDY-style statistical proxy —
    bin every segment's midpoint into a ``cell_mm`` grid, density = the
    count of segment midpoints sharing a cell. A coarse placement-phase
    congestion signal only (backlog: RUDY is superseded by exact gap
    capacity the moment a layered sketch exists); segments with an unset
    endpoint position are left ``nan``.
    """
    cells: dict[tuple[int, int], list[int]] = {}
    midpoints: dict[int, tuple[int, int]] = {}
    for seg_id in range(ir.n_segments):
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
        xa, ya, xb, yb = ir.inst_x[ia], ir.inst_y[ia], ir.inst_x[ib], ir.inst_y[ib]
        if math.isnan(xa) or math.isnan(xb):
            continue
        mx, my = (xa + xb) / 2.0, (ya + yb) / 2.0
        cell = (int(mx // cell_mm), int(my // cell_mm))
        midpoints[seg_id] = cell
        cells.setdefault(cell, []).append(seg_id)
    for seg_id, cell in midpoints.items():
        ir.seg_region_density[seg_id] = float(len(cells[cell]))


def plane_connectivity(ir: PcbIR, net_id: int) -> PlaneConnectivity:
    layer = int(ir.net_plane_layer[net_id])
    if layer == UNSET_LAYER:
        raise ValueError(f"net {net_id} is not plane-promoted")
    stitches = [
        v
        for v in range(ir.n_vias)
        if int(ir.via_net[v]) == net_id and bool(int(ir.via_layer_span[v]) & (1 << layer))
    ]
    return PlaneConnectivity(net_id=net_id, layer=layer, stitch_vias=stitches, ok=len(stitches) >= 2)
