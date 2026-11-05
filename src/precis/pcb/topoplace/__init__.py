"""topo-place — the fast topological layer in front of the geometry router
(``topo.py``). Spec: docs/backlog/pcb-topo-place.md.

**The model.** A design is one immutable snapshot
(:class:`~.design.TopoDesign`): parts by index, pins as footprint-local
offsets, per-signal-net pin lists, lock/freedoms bitsets, the outline
and a seed pose — poses are ``(x, y, rot_quarter, side)`` rows where
rotation is only ever a quarter turn and side 0/1 is top/bottom.
Phase 0 (this slice, 1a) works on straight airwires: a net's wires are
the MST over its pins' board positions, crossings are proper straddles
between different nets' segments — cheap enough to score thousands of
moves a second, honest enough that "0 crossings" is a routable answer.
Phase 1 (slice 2) swaps the proxy for wires-as-triangle-edge-sequences
on a CDT mesh with a layer per wire piece and a via bracket; the
:class:`~.placer.Ladder` hook is how that richer evaluator enters the
same anneal (Decision 10's cost ladder).

Authored copper uses outward polygon boundaries as mesh obstacles. Each
disconnected polygon becomes its own zone with the row's net and layer:
dropping a multipart row loses copper, while hulling it blocks empty gaps.

**The search.** The pairwise part map (``pairmap.py``) scores every
net-connected pair: clique strength 1/(k−1) per shared net, and the
exact facing-row crossing count as an inversion count over 4×4
relative rotations (×2 mirror for a back-side part; R14). The placer
anneals translate/rotate/flip/swap moves on the incremental proxy,
pulled by a decaying rubber band toward the restart's anchor elite;
the archive keeps a small DIVERSE set of good poses so restarts don't
collapse onto one optimum (Decision 9).

**Boundary.** One copy of the kernel, shipped in the
``precis-geom[pcb-kernel]`` wheel slice (Decision 21) — nothing here
may import ``precis.store`` (the wheel import test walks it), and
writes reach the DB only through the ``pcb_place`` job path.

``realise._repair_bigon`` is shared by occupancy witnesses and emission:
a probe cannot promise an arc exchange the emitter would refuse.
Shove acceptance preserves untouched nets' concrete planned descendants
and final shaped-copper clearance; crossing keys alone miss shaping/barrel
failures, and ancestry alone hides lost descendants. Failed withdrawals keep
their original copper because removal can expose shorts between survivors.
Multi-start compares actual failure sets before copper cost, preserving
earlier incomparable incumbents. Order-loop retries retain connected-partner
and encounter-order requirements; only a complete-state shove can establish
that a disconnected partner was rescued. Full identity, retry and accounting
contracts: ``docs/reference/pcb-kernel-contracts.md#shove-and-branch-identity``.

Plane-pad escapes run after signals to preserve signal corridors. Reuse
requires a barrel spanning the stub and independently joined pour; final pours
are rechecked against pre-escape primitives so escapes cannot certify each
other. The length cap is inside candidate verification so a long reuse cannot
hide a shorter fresh site. Midpoint/quarter witnesses precede inset endpoints
and local analytic shadow subtraction to preserve established paths and avoid
work on successful transitions.

Barrel legality does not imply stub legality. Remaining certified planter
points run only after all established single-site/endpoint searches across
every size; this preserves successful fanouts' room for later pads.
Dynamic-index identity/version keys the full survivor cache; single-point
callers retain their first choice. Up to 64 nearest outward zone-boundary
proposals per size follow all portal failures. Shared pad/web, environment,
drill and dynamic predicates certify each center before whole-stub/join/cap
checks; proposal buffers grant no overlapping-zone exemption. This bounded
fallback makes no completeness claim.

Outward copper obstacles prevent chords from admitting taut wires inside
clearance bands; exact pad, swept track and annular via bodies alone certify
joins, so envelopes and drill voids cannot invent contact. Foreign pairs owe
the larger net rule; keepouts have no ownership exemption. Pre-route stubs
retain an all-layer zone veto to protect future corridors; deferred stubs
check their settled layer. Whole-stub and barrel checks include the outline,
since center-only checks miss concave notches. Spatial indices select nearby
bodies before exact predicates, keeping continuation affordable.
Full escape/obstacle/search details:
``docs/reference/pcb-kernel-contracts.md#plane-escapes-and-copper-obstacles``.

``TopoDesign.pin_nets`` resolves names through the full IR and owners through
the routed table: singleton/plane filtering changes branch indices, never
rules. Emission and persistence retain guarded coordinates; later coalescing
or rounding can consume clearance or mask web. All placement, route, fanout
and escape oracles reserve ``clearance.FAB_QUANTUM_MM`` (0.000225 mm) to
cover the 0.000213 mm relative loss possible at 0.0001 mm export precision.
Widening keeps that reserve without changing accepted widths.

Bend spacing evaluates the complete shared-vertex fan and preserves crossing
counts: sequential admission can reject collective legal moves, while an
empty bigon after layer assignment can short wires on different layers.
Exact repair points supplement sampled detours, retaining midpoint fallback
when an endpoint kink fails. Detailed ownership, coalescing, quantization and
placement-audit contracts:
``docs/reference/pcb-kernel-contracts.md#ownership-and-fabrication-reserve``.
"""

from precis.pcb.topoplace.archive import Elite, EliteArchive
from precis.pcb.topoplace.design import (
    FREEDOM_BACK_SIDE,
    FREEDOM_ROTATE,
    PRESETS,
    FlexSlot,
    ObjectiveShares,
    SilkRoom,
    SwapGroup,
    TopoDesign,
    TopoZone,
    design_from_ir,
)
from precis.pcb.topoplace.mesh import (
    MeshConflict,
    MeshConflictError,
    TopoMesh,
    build_mesh,
    edge_blocks_layer,
)
from precis.pcb.topoplace.pairmap import PairMap, PairStat, PairSummary, pair_map
from precis.pcb.topoplace.pins import PinAssignment, assign_pins, check_slots
from precis.pcb.topoplace.placer import Ladder, TopoPlaceResult, topo_place
from precis.pcb.topoplace.proxy import (
    ProxyState,
    backtrack_total,
    direction_total,
    evaluate,
    label_boxes_for,
    label_room_walks,
    label_spots_for,
    margin_total,
    overlap_area,
    pair_gap_flagged,
    pair_gap_report,
    pair_gap_violations,
    pin_world,
    settle_label_room,
    settle_measures,
    settle_pose,
    spread_total,
    via_estimate_total,
)

__all__ = [
    "FREEDOM_BACK_SIDE",
    "FREEDOM_ROTATE",
    "PRESETS",
    "Elite",
    "EliteArchive",
    "FlexSlot",
    "Ladder",
    "MeshConflict",
    "MeshConflictError",
    "ObjectiveShares",
    "PairMap",
    "PairStat",
    "PairSummary",
    "PinAssignment",
    "ProxyState",
    "SilkRoom",
    "SwapGroup",
    "TopoDesign",
    "TopoMesh",
    "TopoPlaceResult",
    "TopoZone",
    "assign_pins",
    "backtrack_total",
    "build_mesh",
    "check_slots",
    "design_from_ir",
    "direction_total",
    "edge_blocks_layer",
    "evaluate",
    "label_boxes_for",
    "label_room_walks",
    "label_spots_for",
    "margin_total",
    "overlap_area",
    "pair_gap_flagged",
    "pair_gap_report",
    "pair_gap_violations",
    "pair_map",
    "pin_world",
    "settle_label_room",
    "settle_measures",
    "settle_pose",
    "spread_total",
    "topo_place",
    "via_estimate_total",
]
