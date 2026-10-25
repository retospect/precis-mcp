"""topo-place — the fast topological layer in front of the smooth-energy
engines (``field.py``, ``precis.pcb.pr``) and the geometry router
(``topo.py``/``vroute.py``). Spec: docs/backlog/pcb-topo-place.md.

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
"""

from precis.pcb.topoplace.archive import Elite, EliteArchive
from precis.pcb.topoplace.design import (
    FREEDOM_BACK_SIDE,
    FREEDOM_ROTATE,
    FlexSlot,
    ObjectiveShares,
    SwapGroup,
    TopoDesign,
    design_from_ir,
)
from precis.pcb.topoplace.pairmap import PairMap, PairStat, PairSummary, pair_map
from precis.pcb.topoplace.pins import PinAssignment, assign_pins, check_slots
from precis.pcb.topoplace.placer import Ladder, TopoPlaceResult, topo_place
from precis.pcb.topoplace.proxy import ProxyState, evaluate, pin_world

__all__ = [
    "FREEDOM_BACK_SIDE",
    "FREEDOM_ROTATE",
    "Elite",
    "EliteArchive",
    "FlexSlot",
    "Ladder",
    "ObjectiveShares",
    "PairMap",
    "PairStat",
    "PairSummary",
    "PinAssignment",
    "ProxyState",
    "SwapGroup",
    "TopoDesign",
    "TopoPlaceResult",
    "assign_pins",
    "check_slots",
    "design_from_ir",
    "evaluate",
    "pair_map",
    "pin_world",
    "topo_place",
]
