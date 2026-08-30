"""The PCB *eyes* — pure-Python analysis over the netlist + placement
graph: the ratsnest + crossing count (the pre-routing objective),
proximity, DRC-lite, the logical signal trace, and the measure (measuring-
tape) evaluators.

The keystone-kind philosophy (shared with cad and structure): own a
legible IR the LLM reads as structure — a circuit is already a graph, a
board already placed rectangles — and rent the heavy kernel (Freerouting,
gerber generation) only at export time. Export is the one place a design
leaves the relational graph.

No GL, no meshing, no embedder — exact geometry / graph folds over the data
the store hands up (:meth:`precis.store._pcb_ops.PcbMixin.pcb_graph`). The
handler renders the results as TOON; this package owns the algorithms so they
are unit-testable in isolation.
"""

from __future__ import annotations

from typing import Any

#: The v1 default stackup (pcb-guided-place-route Slice 1) — 4-layer rigid
#: FR-4, SIG/GND/PWR/SIG. Roles only (no material/thickness_mm) in v1; the
#: schema (``pcb_boards.stackup``) is shaped to carry dielectric detail
#: later. This is the single Python-side source of truth; migration
#: 0138_pcb_boards_routes.sql inlines the same JSON literal for backfill —
#: keep the two in sync by eye.
#:
#: **``role`` alone answers two questions, and a layer here only ever
#: gets ONE of them** (F.Cu/B.Cu: may be routed, never poured; In1.Cu/
#: In2.Cu: may be poured, never routed) — that split is exactly what
#: keeps this fixed default backward compatible, not a hard limit of the
#: engine. :func:`precis.pcb.ir.layer_is_routable`/:func:`~precis.pcb.ir.
#: layer_is_pourable` answer "may this layer carry a routed trace" and
#: "may the AUTOMATIC annealer choose this layer to pour on its own" as
#: two INDEPENDENT questions: a stackup author who wants the standard
#: 4-layer arrangement a real board actually uses (signal traces flowing
#: AROUND a GND/PWR copper fill on the SAME outer layer, plus routed
#: inner layers) sets an explicit ``"routable": True``/``"pourable":
#: True`` on a layer to add the OTHER capability without losing the one
#: ``role`` already implies — see those two functions' docstrings. A
#: human ``op='plane_net'`` instruction is honoured on any stackup layer
#: regardless of either flag (:func:`~precis.pcb.ir.layer_is_pourable`'s
#: own docstring); only the automatic annealer's own guesses are gated.
DEFAULT_STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
    {"name": "In2.Cu", "role": "plane"},
    {"name": "B.Cu", "role": "signal"},
]
