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
