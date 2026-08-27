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
DEFAULT_STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
    {"name": "In2.Cu", "role": "plane"},
    {"name": "B.Cu", "role": "signal"},
]
