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

from precis.pcb import landpattern

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
    methods (plus :meth:`swap_pins` and :meth:`add_via`, which don't fit
    that naming pattern but follow the same contract), which are the
    *only* sanctioned way to change state — each one owns exactly which
    dirty masks it sets, which is the cascade contract every other engine
    (optimizer, realizer) depends on.

    All levels' arrays are always **allocated** once built — "level" is a
    fidelity a *caller* (an estimator in :mod:`precis.pcb.cost`) chooses to
    look at, not a statement about what data exists. That is what lets one
    IR object serve the coarse-vs-fine admissibility comparison in
    :mod:`precis.pcb.cost` (evaluate the same object twice, once
    restricted to L1 data, once allowed L4 data) without rebuilding it.
    """

    stackup: list[dict[str, Any]]

    # ---- L0: pins + nets (hypergraph) ---------------------------------
    instance_refdes: (
        np.ndarray
    )  # object[n_inst] -> str (opaque id = index; refdes is a late label)
    inst_extended_part: np.ndarray  # bool[n_inst] (JLC "Extended" part fee applies)
    #: object[n_inst] -> str | None — the catalog LCSC C-number
    #: (``pcb_components.part_lcsc``) when the instance is a real catalog
    #: part; ``None`` for an unlinked instance (mounting hole,
    #: hand-authored placeholder) — never an empty string, so a caller can
    #: tell "no part" from "empty C-number". The join key a caller with
    #: real footprint data (:meth:`~precis.store.Store.pcb_footprints_for`,
    #: keyed by C-number) needs to build the refdes-keyed ``footprints``
    #: dict :func:`precis.pcb.realize.pad_geometry` accepts.
    instance_part_lcsc: np.ndarray
    pin_instance: np.ndarray  # int32[n_pins]
    pin_label: np.ndarray  # object[n_pins] -> str (pad name; export label only)
    pin_net: np.ndarray  # int32[n_pins], NO_NET if unconnected
    #: float64[n_pins] — the pin's pad offset from its instance centroid, in
    #: FOOTPRINT-LOCAL mm (pre-rotation, pre-mirror). Use :func:`pin_point`
    #: to place one in board space; do not add these to ``inst_x``/``inst_y``
    #: directly or you silently drop rotation and side.
    #:
    #: **ALWAYS SYNTHESIZED today** by :mod:`precis.pcb.landpattern` —
    #: dimensionally sane for the pin count, but not the real part.
    #: :func:`from_graph` takes no ``footprints`` argument, so there is no
    #: path by which a cached pad's real offset can reach this array.
    #: Without it every pin of an instance resolves to the instance
    #: centroid: coincident tracks (spurious 0mm ``clearance`` errors no
    #: router can fix), ``crossings`` computed on a degenerate graph, and
    #: ROTATE/SIDE_FLIP/PIN_SWAP all provably cost-neutral for want of
    #: sub-instance geometry.
    #:
    #: Pad SIZE (``pin_w``/``pin_h`` below) IS taken from the real
    #: footprint when cached (:func:`precis.pcb.realize.pad_geometry`) —
    #: this OFFSET is not; ``pin_offsets_synthesized`` records which is
    #: which and must never be lost on the way to fabrication. Fab export
    #: is unaffected (:func:`precis.pcb.padplace.board_pads` sources
    #: position+size together, bypassing the IR) but the router, DRC and
    #: the ``level='fab'`` preview all read this array. Closing the gap
    #: means reconciling per-pin real offsets with netlist pin identity
    #: through the L0 pin model.
    pin_dx: np.ndarray
    pin_dy: np.ndarray
    #: bool[n_pins] — True where the offset above is a synthesized estimate
    #: rather than real cached pad geometry. Same discipline as
    #: :attr:`precis.pcb.cost.TermValue.is_bound`: a bound must never be
    #: silently reported as a measurement.
    pin_offsets_synthesized: np.ndarray
    #: float64[n_pins] — this pin's own pad SIZE, footprint-local mm,
    #: independent of ``pin_dx``/``pin_dy`` above (a pad's extent is not
    #: implied by its position). :func:`from_graph` fills this from
    #: :mod:`precis.pcb.landpattern`'s package-family synthesis
    #: (:func:`~precis.pcb.landpattern.sizes_for`) by default;
    #: :func:`precis.pcb.realize.pad_geometry` overrides per pin when real
    #: cached ``part_footprints`` geometry exists — the ONE size store, so
    #: router/DRC/gerber-preview never read conflicting numbers for one
    #: pad.
    pin_w: np.ndarray
    pin_h: np.ndarray
    #: object[n_pins] -> str ('circle'|'rect') — real SMD pads are not
    #: circles (module docstring's "non-circular pads" note); a synthesized
    #: bound picks the shape its own package family would plausibly have
    #: (round for a THT header/testpoint, rectangular otherwise). Nothing
    #: downstream may assume 'circle' — :func:`precis.pcb.realize.
    #: pads_for_ir`'s consumers read this field rather than hardcoding one.
    pin_shape: np.ndarray
    #: bool[n_pins] — same discipline as ``pin_offsets_synthesized``, but
    #: tracked SEPARATELY: a pin's SIZE can be real (a cached footprint's
    #: actual pad) even while its OFFSET stays a synthesized placeholder
    #: (:mod:`precis.pcb.landpattern`'s offsets are synthesized always, by
    #: design — see that module's docstring), so one flag cannot honestly
    #: describe both.
    pin_pad_synthesized: np.ndarray
    net_name: np.ndarray  # object[n_nets] -> str
    net_domain: np.ndarray  # object[n_nets] -> str ('electrical'|'fluidic'|'thermal')
    net_class: np.ndarray  # object[n_nets] -> str
    #: float64[n_nets], nan = no current annotation (``pcb_nets.est_
    #: current_a``). This is a real, already-shipped store column -- the
    #: "current annotation" :mod:`precis.pcb.rules`'s resolver reads to
    #: derive an IPC-2221 track width, distinct from the LLM-authored
    #: datasheet :class:`precis.pcb.objectives.NetAnnotation` side-channel
    #: (impedance/edge-rate), which stays a `CostConfig` dict since it has
    #: no store column yet.
    net_current_a: np.ndarray

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
    # Two independent lock bits, not one — `fixed='xy'|'rot'|'both'`
    # (pcb-guided-place-route Slice 6) needs to lock translation and
    # rotation separately, so a single bool can't carry it (a 'rot'-only
    # part must stay translatable). `optimize.py`'s move generators are
    # what actually enforce these; the IR only carries the bits.
    inst_fixed_xy: np.ndarray  # bool[n_inst] -- 'xy' or 'both'
    inst_fixed_rot: np.ndarray  # bool[n_inst] -- 'rot' or 'both'

    # ---- L4: metric annotations -----------------------------------------
    seg_gap_capacity: (
        np.ndarray
    )  # float64[n_seg], strands-that-fit; nan = not yet computed
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

    #: The board's authored panel boundary — a polygon ring (``(x, y)``
    #: pairs, mm), or ``None`` when no outline feature exists yet (a design
    #: that hasn't authored one). Board-config data, same status as
    #: ``stackup`` above (not part of the dirty-mask cascade -- nothing
    #: mutates it via a move, only :func:`from_graph`/:func:`session.
    #: build_ir` populate it once at hydration). ``precis.pcb.cost``'s
    #: ``board_edge_clearance`` term and ``optimize.py``'s TRANSLATE-move
    #: clamp both read this SAME field, so a design with an authored
    #: outline gets one consistent boundary everywhere rather than each
    #: consumer inventing its own board-scale guess (see gr267456 —
    #: "two components implementing one rule" is the defect family this is
    #: closing, not repeating).
    outline: list[tuple[float, float]] | None = None

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
        self,
        inst_id: int,
        *,
        x: float | None = None,
        y: float | None = None,
        rot: float | None = None,
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
        for seg_id in self._segs_of_instance.get(inst_id, []):
            self.dirty_l4[seg_id] = True
            self.dirty_l5[seg_id] = True

    def set_layer(self, seg_id: int, layer: int) -> None:
        """L1 mutator: (re)assign a segment's routing layer. Dirties L1 and
        L4 for that segment (its gap-capacity draw moves to a new layer's
        congestion pool) and L5 (copper must re-realize). L2 and L3 are
        untouched — a layer reassignment doesn't change which side of an
        obstacle a connection takes, nor any component's position."""
        if not (0 <= layer < self.n_layers):
            raise ValueError(
                f"layer {layer} out of range for a {self.n_layers}-layer stackup"
            )
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
            raise ValueError(
                f"layer {layer} out of range for a {self.n_layers}-layer stackup"
            )
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
            raise ValueError(
                f"pin {pin_id} has {hi - lo} incident darts, got {len(dart_order)}"
            )
        self.rotation_darts[lo:hi] = dart_order
        for dart in dart_order:
            seg_id = dart // 2
            self.dirty_l2[seg_id] = True
            self.dirty_l4[seg_id] = True
            self.dirty_l5[seg_id] = True

    def swap_pins(self, pin_a: int, pin_b: int) -> None:
        """L0 mutator: exchange which net occupies ``pin_a`` vs ``pin_b`` —
        the move that lets an MCU's GPIO assignment (or a symmetric part's
        pin labeling) become a search variable, per the module docstring's
        "a win that only exists in graph space" note. Both pins must
        belong to the SAME instance (a swap that also moved a net to a
        different instance would be a netlist edit, not a relabeling) and
        carry equal rotation-CSR degree (this slice restricts pin swap to
        simple, single-dart pins — a hub pin with several darts would need
        its own dart-order permutation, future work, not silently
        mismatched here).

        Dirties L2 (each pin's rotation slot now names a different dart)
        and L4/L5 for every segment that touched either pin — ``seg_net``
        is untouched (electrical membership doesn't change), but the
        segment's endpoint now sits at the OTHER pin's own fixed footprint
        position, so gap/loop/coupling numbers must re-derive.
        **L1 and L3 stay clean**: no layer or component position changed —
        this really is "zero physical cost" (backlog, verbatim) at the
        IR's own granularity. Only ``_segs_of_instance[instance]`` is
        scanned (never the whole board), the same locality budget every
        other mutator here honours.
        """
        if pin_a == pin_b:
            return
        inst_a, inst_b = int(self.pin_instance[pin_a]), int(self.pin_instance[pin_b])
        if inst_a != inst_b:
            raise ValueError("swap_pins requires both pins on the same instance")
        lo_a, hi_a = (
            int(self.rotation_index[pin_a]),
            int(self.rotation_index[pin_a + 1]),
        )
        lo_b, hi_b = (
            int(self.rotation_index[pin_b]),
            int(self.rotation_index[pin_b + 1]),
        )
        if (hi_a - lo_a) != (hi_b - lo_b):
            raise ValueError(
                f"swap_pins requires equal rotation-CSR degree (pin {pin_a} has "
                f"{hi_a - lo_a}, pin {pin_b} has {hi_b - lo_b})"
            )
        self.pin_net[pin_a], self.pin_net[pin_b] = (
            int(self.pin_net[pin_b]),
            int(self.pin_net[pin_a]),
        )
        a_darts = self.rotation_darts[lo_a:hi_a].copy()
        b_darts = self.rotation_darts[lo_b:hi_b].copy()
        self.rotation_darts[lo_a:hi_a] = b_darts
        self.rotation_darts[lo_b:hi_b] = a_darts

        touched: list[int] = []
        for seg_id in self._segs_of_instance.get(inst_a, ()):
            changed = False
            if int(self.seg_pin_a[seg_id]) == pin_a:
                self.seg_pin_a[seg_id] = pin_b
                changed = True
            elif int(self.seg_pin_a[seg_id]) == pin_b:
                self.seg_pin_a[seg_id] = pin_a
                changed = True
            if int(self.seg_pin_b[seg_id]) == pin_a:
                self.seg_pin_b[seg_id] = pin_b
                changed = True
            elif int(self.seg_pin_b[seg_id]) == pin_b:
                self.seg_pin_b[seg_id] = pin_a
                changed = True
            if changed:
                touched.append(seg_id)
        for seg_id in touched:
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


def _build_segments_index(
    seg_pin_a: np.ndarray, seg_pin_b: np.ndarray, pin_instance: np.ndarray
) -> dict[int, list[int]]:
    idx: dict[int, list[int]] = {}
    for seg_id, (pa, pb) in enumerate(zip(seg_pin_a, seg_pin_b)):
        for inst_id in (int(pin_instance[pa]), int(pin_instance[pb])):
            idx.setdefault(inst_id, []).append(seg_id)
    return idx


def from_graph(
    graph: dict[str, Any],
    *,
    stackup: list[dict[str, Any]] | None = None,
    outline: list[tuple[float, float]] | list[list[float]] | None = None,
) -> PcbIR:
    """Build an L0 :class:`PcbIR` from the plain-dict graph shape shared
    with :mod:`precis.pcb.eyes` (``{"instances":[...], "nets":[...],
    "unconnected":[...]}``) — no DB, so this stays independently
    unit-testable.

    ``outline`` is the board's authored panel boundary (a polygon ring),
    carried on the returned :class:`PcbIR` verbatim as ``(x, y)`` float
    tuples — ``None`` (the default) when the caller has none yet, e.g. a
    design that hasn't authored a board outline feature. Never derived
    here from anything else (no "guess a rectangle from the parts"
    fallback) — that guess belongs to a consumer that has decided it wants
    one (see :attr:`PcbIR.outline`'s own docstring).

    **Segment decomposition is a star per net** (first member is the hub):
    a design *choice* the netlist records at L0, not something geometry
    dictates — a net's electrical meaning doesn't care which two-pin edges
    represent it, only that they span every member. An MST/Steiner
    alternative is a future move class (`re-root the star`), not a
    correctness requirement of this slice.

    L1 layers and L3 positions are left **unset** unless the graph already
    supplies them, matching what the netlist/placement store actually
    knows at hydration time. The L2 rotation CSR is *shaped* by real
    topology (every dart gets a slot, sized by each pin's actual degree)
    but its initial **content is plain segment-creation order** — not a
    geometry-derived embedding, just an arbitrary starting order a caller
    is free to overwrite via :meth:`PcbIR.set_rotation` (see
    :func:`propose_rotation_from_positions` for a principled one).
    """
    # Default to the house 4-layer stackup: a bare `from_graph(graph)` must
    # yield an IR whose layer mutators work (`set_layer(0, 1)`, plane
    # promotion) — an empty stackup makes every layer index "out of range"
    # and only an explicit `stackup=[]` caller could want that.
    if stackup is None:
        from precis.pcb import DEFAULT_STACKUP

        stackup = DEFAULT_STACKUP
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
    net_current_a: list[float] = []
    seg_net: list[int] = []
    seg_pin_a: list[int] = []
    seg_pin_b: list[int] = []

    for net in graph.get("nets") or []:
        net_id = len(net_name)
        net_name.append(net["name"])
        net_domain.append(net.get("domain") or "electrical")
        net_class.append(net.get("net_class") or "")
        current = net.get("est_current_a")
        net_current_a.append(math.nan if current is None else float(current))
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

    # Per-pin pad offsets. Grouped by instance so each part gets ONE
    # coherent land pattern: assigning offsets pin-by-pin would let two
    # pins of the same part draw from different package families and land
    # on top of each other, which is the exact defect this fixes.
    #
    # Order within an instance is pin-creation order, which is netlist
    # member order — the same order the author wrote. That is what makes a
    # pin swap mean something: adjacency in the land pattern follows the
    # author's pin numbering rather than an arbitrary permutation.
    pin_dx = np.zeros(n_pins, dtype=np.float64)
    pin_dy = np.zeros(n_pins, dtype=np.float64)
    pin_synth = np.zeros(n_pins, dtype=bool)
    # Pad SIZE, same per-instance grouping and the same reason: two pins
    # of one part must come from ONE package family's size table, not
    # whichever pin happened to be visited first. See PcbIR.pin_w's own
    # docstring for the defect this closes (every pad the same 0.4mm disc,
    # regardless of package).
    pin_w = np.zeros(n_pins, dtype=np.float64)
    pin_h = np.zeros(n_pins, dtype=np.float64)
    pin_shape = _obj_array([""] * n_pins)
    pin_pad_synth = np.zeros(n_pins, dtype=bool)
    _pins_of_inst: dict[int, list[int]] = {}
    for pid, inst_id in enumerate(pin_instance):
        _pins_of_inst.setdefault(int(inst_id), []).append(pid)
    for inst_id, pids in _pins_of_inst.items():
        label = str(instances[inst_id].get("label") or "")
        offsets, synthesized = landpattern.offsets_for(len(pids), label=label)
        for pid, (dx, dy) in zip(pids, offsets, strict=True):
            pin_dx[pid] = dx
            pin_dy[pid] = dy
            pin_synth[pid] = synthesized
        sizes, size_synthesized = landpattern.sizes_for(len(pids), label=label)
        for pid, (w, h, shape) in zip(pids, sizes, strict=True):
            pin_w[pid] = w
            pin_h[pid] = h
            pin_shape[pid] = shape
            pin_pad_synth[pid] = size_synthesized

    # CSR slots are allocated by actual pin degree (every dart needs a
    # home), but the *order* within each pin's slot is plain segment-
    # creation order — arbitrary, not geometry-derived. That is the
    # honest reading of "no placeholder derived from anything": the
    # shape reflects real topology (how many darts a pin has), the
    # content doesn't pretend to already be a chosen embedding.
    darts_by_pin: list[list[int]] = [[] for _ in range(n_pins)]
    for seg_id in range(n_seg):
        darts_by_pin[seg_pin_a[seg_id]].append(seg_id * 2)
        darts_by_pin[seg_pin_b[seg_id]].append(seg_id * 2 + 1)
    rotation_index = np.zeros(n_pins + 1, dtype=np.int32)
    flat_darts: list[int] = []
    for p in range(n_pins):
        rotation_index[p + 1] = rotation_index[p] + len(darts_by_pin[p])
        flat_darts.extend(darts_by_pin[p])

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
        outline=(
            [(float(p[0]), float(p[1])) for p in outline]
            if outline is not None
            else None
        ),
        instance_refdes=_obj_array([inst["refdes"] for inst in instances]),
        inst_extended_part=np.array(
            [bool(inst.get("extended_part")) for inst in instances], dtype=bool
        ),
        instance_part_lcsc=_obj_array([inst.get("part_lcsc") for inst in instances]),
        pin_instance=np.array(pin_instance, dtype=np.int32),
        pin_label=_obj_array(pin_label),
        pin_net=np.array(pin_net, dtype=np.int32),
        pin_dx=pin_dx,
        pin_dy=pin_dy,
        pin_offsets_synthesized=pin_synth,
        pin_w=pin_w,
        pin_h=pin_h,
        pin_shape=pin_shape,
        pin_pad_synthesized=pin_pad_synth,
        net_name=_obj_array(net_name),
        net_domain=_obj_array(net_domain),
        net_class=_obj_array(net_class),
        net_current_a=np.array(net_current_a, dtype=np.float64),
        seg_net=np.array(seg_net, dtype=np.int32),
        seg_pin_a=np.array(seg_pin_a, dtype=np.int32),
        seg_pin_b=np.array(seg_pin_b, dtype=np.int32),
        seg_layer=np.full(n_seg, UNSET_LAYER, dtype=np.int8),
        net_plane_layer=np.full(n_nets, UNSET_LAYER, dtype=np.int8),
        via_layer_span=np.zeros(0, dtype=np.uint16),
        via_net=np.zeros(0, dtype=np.int32),
        seg_side=np.zeros(n_seg, dtype=np.int8),
        rotation_index=rotation_index,
        rotation_darts=np.array(flat_darts, dtype=np.int32),
        inst_x=inst_x,
        inst_y=inst_y,
        # An authored/persisted rotation, not always zero. Hardcoding zeros
        # here silently discarded every rotation the optimizer had settled
        # the moment an IR was rebuilt from the store — which is once per
        # job, so placement's rotations never reached routing and no
        # rebuilt IR could reproduce the pin coordinates of its own copper.
        inst_rot=np.array(
            [float(inst.get("rot") or 0.0) for inst in instances], dtype=float
        ),
        inst_fixed_xy=np.array(
            [(inst.get("fixed") or "") in ("xy", "both") for inst in instances],
            dtype=bool,
        ),
        inst_fixed_rot=np.array(
            [(inst.get("fixed") or "") in ("rot", "both") for inst in instances],
            dtype=bool,
        ),
        seg_gap_capacity=np.full(n_seg, np.nan),
        seg_region_density=np.full(n_seg, np.nan),
        seg_copper_length_mm=np.full(n_seg, np.nan),
        dirty_l1=np.zeros(n_seg, dtype=bool),
        dirty_l2=np.zeros(n_seg, dtype=bool),
        dirty_l3=np.zeros(n_inst, dtype=bool),
        dirty_l4=np.zeros(n_seg, dtype=bool),
        dirty_l5=np.zeros(n_seg, dtype=bool),
    )
    ir._segs_of_instance = _build_segments_index(
        ir.seg_pin_a, ir.seg_pin_b, ir.pin_instance
    )
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
            other_pin = (
                int(ir.seg_pin_b[seg_id]) if end == 0 else int(ir.seg_pin_a[seg_id])
            )
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


def net_member_counts(ir: PcbIR) -> dict[int, int]:
    """Distinct connected-pin count per net id — a count of 0 or 1 is a
    "dangling" net (see :func:`unconnected_items`): legal (a test point, an
    NC net, a mounting-hole net), never fatal, and structurally incapable
    of ever being routed. Shared by that check and the ``pcb_route`` job's
    dangling-net exemption: a net this small has nothing to route, ever,
    so it must not be left permanently absent from ``pcb_routes`` — that
    absence is what silently wedges the ``route_complete`` gate forever."""
    pins_per_net: dict[int, set[int]] = {}
    for pin_id in range(ir.n_pins):
        net_id = int(ir.pin_net[pin_id])
        if net_id != NO_NET:
            pins_per_net.setdefault(net_id, set()).add(pin_id)
    return {net_id: len(pins_per_net.get(net_id, ())) for net_id in range(ir.n_nets)}


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
    for net_id, n in net_member_counts(ir).items():
        if n < 2:
            out.append(
                {"code": "dangling-net", "net": str(ir.net_name[net_id]), "pins": n}
            )
    return out


def _layer_graph(ir: PcbIR, layer: int) -> tuple[int, int, list[list[int]]]:
    """(vertex count, edge count, connected components as pin-id lists) for
    the subgraph of segments assigned exactly to ``layer``. Vias aren't
    folded in here (a through via bridges every layer, which is the part
    of the problem that genuinely doesn't decompose per layer per the
    backlog — left to the optimizer's constraint set, not this bound)."""
    seg_ids = [s for s in range(ir.n_segments) if int(ir.seg_layer[s]) == layer]
    pins = sorted(
        {int(ir.seg_pin_a[s]) for s in seg_ids}
        | {int(ir.seg_pin_b[s]) for s in seg_ids}
    )
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
    """**Feasibility predicate only — not the ``crossings`` cost-term
    backing** (:func:`same_layer_crossing_count` is). Answers "is this
    layer's segment graph forced non-planar" (could it in principle be
    drawn on one layer without a crossing) via the classical Euler
    planar-graph edge bound: a simple planar graph on V>=3 vertices has at
    most 3V-6 edges, so ``E - (3V - 6)`` (when positive) can't all be
    drawn without a crossing.

    Provably always zero on a real board: :func:`from_graph`
    star-decomposes every net (one hub pin, spokes), and a pin belongs to
    exactly one net, so a layer's entire segment graph is a
    vertex-disjoint FOREST of per-net stars — a forest always satisfies
    ``E <= V-1 <= 3V-6``. Abstract planarity says nothing about a
    particular embedding's geometry (a forest can still be DRAWN with
    arbitrarily many crossings), which is why this cannot back a "does it
    currently cross" cost term. Kept as the cheap, geometry-free
    feasibility check (:func:`per_layer_planar`).

    ``refine=False`` (coarse/L1): one bound over the whole layer's graph,
    O(1) after counting V, E. ``refine=True`` (finer/L2): the same bound
    per connected component, summed — provably >= the coarse bound
    (components can't share crossings), tighter without changing the
    formula.
    """
    v, e, components = _layer_graph(ir, layer)
    if not refine:
        return euler_bound(v, e)
    return sum(
        euler_bound(len(comp), _edges_in(ir, layer, comp)) for comp in components
    )


def euler_bound(v: int, e: int) -> int:
    """The Euler planar-edge-count bound itself — public (not
    ``_euler_bound``) so callers needing the raw feasibility formula (and
    :func:`same_layer_crossing_bound`'s own two call shapes) share one
    implementation rather than drifting apart. **No longer the
    ``crossings`` cost term's backing** (see
    :func:`same_layer_crossing_count`'s and
    :func:`same_layer_crossing_bound`'s docstrings for why: this formula
    is provably zero on any star-decomposed board, which is every board
    :func:`from_graph` produces)."""
    if v < 3:
        return 0
    return max(0, e - (3 * v - 6))


def _edges_in(ir: PcbIR, layer: int, pins: list[int]) -> int:
    pin_set = set(pins)
    return sum(
        1
        for s in range(ir.n_segments)
        if int(ir.seg_layer[s]) == layer
        and int(ir.seg_pin_a[s]) in pin_set
        and int(ir.seg_pin_b[s]) in pin_set
    )


def per_layer_planar(ir: PcbIR, layer: int) -> bool:
    """**Necessary, not sufficient**: True means the Euler edge bound
    doesn't rule out a planar drawing of this layer's segment graph (it
    may still not be planar — genuine planarity testing is a separate,
    finer future refinement); False means it definitely is not planar
    (some crossing is unavoidable, since the bound is proven, not
    heuristic). Uses the refined (per-component) bound, since that's the
    tighter and therefore more useful "definitely not planar" signal.
    **A feasibility question, not a "does it currently cross" one** — see
    :func:`same_layer_crossing_bound`'s docstring; True here is fully
    compatible with the CURRENT layout having many real, geometric
    crossings (:func:`same_layer_crossing_count`), since a forest is
    always abstractly planar yet can be drawn crossing itself freely."""
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


def compute_gap_capacity(
    ir: PcbIR, *, pitch_mm: float = 0.3, seg_ids: list[int] | None = None
) -> None:
    """Fill L4 ``seg_gap_capacity`` (strands-that-fit) from L3 positions:
    a segment's binding gap is approximated as the distance from either
    endpoint's instance to the nearest *other* instance, divided by
    ``pitch_mm`` (class trace width + clearance). Segments whose endpoint
    positions are unset are left ``nan`` — genuinely undefined, not
    silently zeroed; :mod:`precis.pcb.cost` is what turns "undefined"
    into an optimistic bound rather than this function guessing.

    ``seg_ids=None`` (the default) recomputes every segment, as before.
    :mod:`precis.pcb.optimize` passes a restricted list so a placement
    move's gap-capacity recompute stays bounded to the segments that
    move actually touched, instead of re-scanning the whole board —
    the locality contract the joint optimizer's per-move budget depends
    on (see that module's docstring for the full incremental-update
    reasoning, including :func:`nearest_other_instance`'s ``instance_id``
    return, which is what lets the optimizer know *which other* segments
    a move can invalidate).
    """
    ids = range(ir.n_segments) if seg_ids is None else seg_ids
    for seg_id in ids:
        found = nearest_other_instance(ir, seg_id)
        if found is not None:
            gap, _nearest_id = found
            ir.seg_gap_capacity[seg_id] = math.floor(gap / pitch_mm)


def nearest_other_instance(ir: PcbIR, seg_id: int) -> tuple[float, int] | None:
    """The distance from ``seg_id``'s nearer endpoint to the closest
    *other* (non-endpoint) instance, AND which instance realized that
    minimum — ``None`` if any position involved is unset (nan).

    The instance id is the extra bit :func:`compute_gap_capacity` doesn't
    need but :mod:`precis.pcb.optimize` does: caching it lets a placement
    move's delta recompute exactly the segments whose nearest-instance
    answer could have changed (the ones incident to the moved instance,
    plus the ones that *used to point at* it) without re-deriving every
    segment's gap from scratch — see that module's docstring.
    """
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    endpoints = {int(ir.pin_instance[a]), int(ir.pin_instance[b])}
    best: float | None = None
    best_id = -1
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
                best_id = other
    if best is None:
        return None
    return best, best_id


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


# ── L3 geometric crossing count: the real `crossings` cost-term backing ──
# (see `same_layer_crossing_bound`'s docstring for the forest proof of
# why the Euler bound above cannot back this term). Uses plain L3
# instance-centroid geometry, same fidelity as
# `compute_gap_capacity`/`nearest_other_instance` above — no shapely, no
# sub-instance pad offsets (those live only in pinswap.py's own local
# geometry, not in the IR).


def segment_points(
    ir: PcbIR, seg_id: int
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """``seg_id``'s two endpoints at INSTANCE-centroid granularity — the
    same L3 fidelity every geometric cost.py estimator already uses
    (``loop_inductance``, ``coupling``; see optimize.py's ROTATE note for
    why component rotation/sub-instance pad offset isn't modeled here).
    ``None`` if either endpoint's instance has no L3 position yet —
    callers exclude that segment rather than treating a NaN position as
    the origin, which would manufacture phantom crossings at (0, 0) among
    every unplaced net (the geometric mirror of the "undefined != zero"
    rule the rest of this module's L4 section already follows). Shared by
    :func:`same_layer_crossing_count` (the full per-layer sweep) and
    :mod:`precis.pcb.optimize`'s per-move crossing delta, so both derive
    the identical geometry from one place."""
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
    xa, ya = float(ir.inst_x[ia]), float(ir.inst_y[ia])
    xb, yb = float(ir.inst_x[ib]), float(ir.inst_y[ib])
    if math.isnan(xa) or math.isnan(ya) or math.isnan(xb) or math.isnan(yb):
        return None
    return (xa, ya), (xb, yb)


def instance_pad_radius(ir: PcbIR) -> np.ndarray:
    """Per-instance distance from the part centre to its outermost PIN —
    how much room its land pattern occupies by pin OFFSET alone,
    deliberately **not** widened by pad SIZE (``pin_w``/``pin_h``).

    Needed because a fixed courtyard radius is wrong by construction for
    anything with more than a few pins:
    :data:`precis.pcb.drc.DEFAULT_COURTYARD_RADIUS_MM` is 1.0mm, but a
    14-pin dual-row land pattern reaches 2.27mm — two such parts at the
    nominal 2.0mm centre-to-centre physically interleave pads.

    **Deliberately offset-only, not pad-size-aware — do not widen without
    first fixing router capacity.** A pad-size-aware bound (loose:
    ``hypot(dx, dy) + hypot(w, h) / 2``; exact axis-aligned far-corner:
    ``hypot(abs(dx) + w/2, abs(dy) + h/2)`` — exact because no per-pin
    rotation is independent of ``inst_rot`` here) is measurably tighter,
    but on ``tests/test_pcb_reference_end_to_end.py``'s ESP32-C3 fixture
    it produces MORE unrouted nets on some seeds (zero DRC legality
    findings either way — every regression is ``unrouted``, never a
    placement-legality violation). The tighter keep-out exceeds what
    :mod:`precis.pcb.maze`'s router can absorb at its current
    iteration/schedule budget: a router CAPACITY limit, not a
    placement-legality bug, and out of this function's scope to fix. See
    :func:`instance_keepout_radius_mm` for the shared consumer-facing
    keep-out formula this feeds.

    Rotation-invariant (the max is over a radius, holds for any
    ``inst_rot``). A pinless instance (mounting hole, fiducial) gets
    0.0 — the caller floors this with whatever body radius it believes
    in.
    """
    out = np.zeros(ir.n_instances, dtype=np.float64)
    if ir.n_pins == 0:
        return out
    reach = np.hypot(ir.pin_dx, ir.pin_dy)
    np.maximum.at(out, ir.pin_instance.astype(np.int64), reach)
    return out


#: Gap left around a part's outermost pad when deriving its placement/DRC
#: keep-out. Two adjacent parts' pads end up at least twice this apart,
#: which is where their escape routes have to fit — set it to zero and a
#: legal placement can still be one the router cannot escape. Public (not
#: ``optimize.py``-private) because :func:`instance_keepout_radius_mm`
#: below is the ONE keep-out-radius formula every consumer — the placer's
#: legality check, its seeder, and the DRC courtyard geometry
#: :mod:`precis.handlers.pcb` builds — must share; a second copy is
#: exactly the "one rule, two call sites, drifted" defect this module's
#: other docstrings keep citing (see :func:`pads_for_ir`'s sibling note
#: in :mod:`precis.pcb.realize`).
PAD_BREATHING_MM = 0.6


def instance_keepout_radius_mm(ir: PcbIR, *, min_radius_mm: float = 0.0) -> np.ndarray:
    """Per-instance placement/DRC keep-out radius: each part's own
    :func:`instance_pad_radius` plus :data:`PAD_BREATHING_MM`, floored at
    ``min_radius_mm``.

    ``min_radius_mm`` is the caller's own nominal half-courtyard floor
    (``cost.COURTYARD_MIN_SEPARATION_MM / 2.0`` for every current caller)
    rather than a default baked in here: this module sits BELOW
    :mod:`precis.pcb.cost` (``cost.py`` imports from ``ir.py``, never the
    reverse), so ``ir.py`` cannot import a cost-policy constant without
    creating the cycle — see the module docstring's layering note. Two
    instances are legal / clear when their centres are at least the SUM
    of their two radii apart; a fixed radius for every part is wrong for
    the same reason :func:`instance_pad_radius` is: a 14-pin dual-row
    land pattern reaches 2.27mm from its own centre, a module 8.89mm."""
    return np.maximum(instance_pad_radius(ir) + PAD_BREATHING_MM, min_radius_mm)


def pin_point(ir: PcbIR, pin_id: int) -> tuple[float, float] | None:
    """One pin's position in BOARD space, or ``None`` if unplaced.

    The single place footprint-local pad offsets become board coordinates.
    Every geometric consumer must go through here rather than reading
    ``inst_x``/``inst_y`` for a pin — that shortcut is what put all of a
    part's pins on one coordinate and made ~600 clearance errors
    structurally unfixable by any router.

    Mirror before rotate, rotation clockwise-from-north: the board frame's
    convention, matching :mod:`precis.pcb.padplace` (which pinned the order
    with a test after getting it wrong once). Delegates to
    :func:`precis.pcb.landpattern.rotate_offset` so the transform has ONE
    implementation — two copies of a rotation rule is how the export path
    and the routing path silently disagree about where a pad is.
    """
    inst = int(ir.pin_instance[pin_id])
    x, y = float(ir.inst_x[inst]), float(ir.inst_y[inst])
    if math.isnan(x) or math.isnan(y):
        return None
    dx, dy = float(ir.pin_dx[pin_id]), float(ir.pin_dy[pin_id])
    if dx == 0.0 and dy == 0.0:
        return (x, y)
    rot = float(ir.inst_rot[inst])
    rdx, rdy = landpattern.rotate_offset(dx, dy, 0.0 if math.isnan(rot) else rot)
    return (x + rdx, y + rdy)


def same_layer_crossing_count(ir: PcbIR, layer: int) -> int:
    """A **sweep-line count of ACTUAL straight-line segment intersections**
    on ``layer`` — the real ``crossings`` cost-term backing (see
    :mod:`precis.pcb.cost`'s ``BoundDirection.UPPER`` on that term), and
    the fix for :func:`same_layer_crossing_bound`'s "provably always
    zero" defect: this measures what actually crosses in the CURRENT
    layout, not whether the layer's graph is abstractly forced
    non-planar. ``O(n log n + k)`` via :func:`precis.pcb.geom.
    sweep_line_crossings` (see that function's own docstring for the
    exact complexity tradeoff made).

    **This is an UPPER bound on eventually-REALIZED crossings, never a
    lower one** — realize.py's router can sometimes route around a
    straight-line crossing (a via, a detour), so this can only ever
    overstate the eventual routed count. Zero here is the strong,
    useful guarantee ("straight-line crossings of zero ⇒ routed
    crossings of zero"); a positive count is a real, present conflict at
    THIS fidelity, not a hint that might evaporate at higher fidelity —
    the opposite direction from every LOWER-bound estimator in this
    module.

    Degenerate cases, decided explicitly (delegated to
    :func:`precis.pcb.geom.sweep_line_crossings`, which is where the
    reasoning for each lives):
    - two segments of the SAME net (spokes of one star hub) never count,
      regardless of geometry.
    - segments sharing an endpoint coordinate don't count either.
    - collinear overlap / touch-without-crossing are NOT counted, only
      genuine transversal 'X' crossings.
    - a segment with an unplaced (NaN) endpoint is EXCLUDED entirely
      (:func:`segment_points` returns ``None`` for it) — never treated as
      sitting at the origin, which would manufacture crossings among every
      unplaced net's segments.
    """
    from precis.pcb.geom import sweep_line_crossings

    segments: list[tuple[int, tuple[float, float], tuple[float, float]]] = []
    for s in range(ir.n_segments):
        if int(ir.seg_layer[s]) != layer:
            continue
        points = segment_points(ir, s)
        if points is None:
            continue
        segments.append((int(ir.seg_net[s]), points[0], points[1]))
    return sweep_line_crossings(segments)


def plane_connectivity(ir: PcbIR, net_id: int) -> PlaneConnectivity:
    layer = int(ir.net_plane_layer[net_id])
    if layer == UNSET_LAYER:
        raise ValueError(f"net {net_id} is not plane-promoted")
    stitches = [
        v
        for v in range(ir.n_vias)
        if int(ir.via_net[v]) == net_id
        and bool(int(ir.via_layer_span[v]) & (1 << layer))
    ]
    return PlaneConnectivity(
        net_id=net_id, layer=layer, stitch_vias=stitches, ok=len(stitches) >= 2
    )
