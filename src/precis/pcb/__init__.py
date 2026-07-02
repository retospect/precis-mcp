"""The PCB *eyes* (ADR 0042 §8) — pure-Python analysis over the netlist +
placement graph: the ratsnest + crossing count (the pre-routing objective),
proximity, DRC-lite, the logical signal trace, and the measure (measuring-
tape) evaluators.

No GL, no meshing, no embedder — exact geometry / graph folds over the data
the store hands up (:meth:`precis.store._pcb_ops.PcbMixin.pcb_graph`). The
handler renders the results as TOON; this package owns the algorithms so they
are unit-testable in isolation.
"""

from __future__ import annotations
