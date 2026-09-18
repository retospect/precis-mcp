"""The PCB kernel — the whole place-and-route pipeline in pure Python,
from a netlist to gerbers, over one progressively-enriched IR.

The keystone-kind philosophy (shared with cad and structure): own a
legible IR the LLM reads as structure — a circuit is already a graph, a
board already placed rectangles — and keep every stage a pure function
over it so each is unit-testable in isolation. Export is the one place a
design leaves the relational graph. Freerouting (:mod:`~precis.pcb.route`)
is the one rented kernel, and it is no longer the critical path: the
in-house realizer + maze router produce the copper the gerbers are cut
from.

Map, in pipeline order (design doc:
``docs/backlog/pcb-guided-place-route.md``):

* **IR** — :mod:`~precis.pcb.ir` (L0-L5 enrichment levels, per-net rules
  via :mod:`~precis.pcb.rules`, objective vectors in
  :mod:`~precis.pcb.objectives`).
* **Eyes** — :mod:`~precis.pcb.eyes` / :mod:`~precis.pcb.ratsnest`:
  ratsnest + crossing count, proximity, signal trace, measures — the
  pre-routing objective, folded over
  :meth:`precis.store._pcb_ops.PcbMixin.pcb_graph`.
* **Parts + footprints** — :mod:`~precis.pcb.catalog` (jlcparts dump),
  :mod:`~precis.pcb.jlc_api` (live stock), :mod:`~precis.pcb.easyeda` +
  :mod:`~precis.pcb.footprint` (pad geometry), :mod:`~precis.pcb.padplace`
  (pads in board coordinates), :mod:`~precis.pcb.landpattern`
  (synthesized pads when no footprint is cached),
  :mod:`~precis.pcb.escape` (footprint-intrinsic escape precompute),
  :mod:`~precis.pcb.capabilities` (fab rules as versioned data).
* **Place + route** — :mod:`~precis.pcb.place` (force-directed seed),
  :mod:`~precis.pcb.optimize` (the joint place+route annealer, the
  package's largest engine), :mod:`~precis.pcb.cost` (its one cost
  function), :mod:`~precis.pcb.pinswap`, :mod:`~precis.pcb.generators`
  (computed components such as EWOD arrays), :mod:`~precis.pcb.session`
  (IR↔DB glue for the ``pcb_place``/``pcb_route`` jobs).
* **Copper** — :mod:`~precis.pcb.realize` (sketch → copper geometry),
  :mod:`~precis.pcb.maze` (grid router that cannot violate clearance),
  :mod:`~precis.pcb.planes` (pours), :mod:`~precis.pcb.tiling` (a net
  owns a region on a layer), :mod:`~precis.pcb.silk` +
  :mod:`~precis.pcb.stroke_font` (silkscreen), :mod:`~precis.pcb.geom`
  (segment/fillet/quantization primitives).
* **Checks** — :mod:`~precis.pcb.drc` (geometric DRC on realized copper;
  its graph-level half lives in ``ir.py``), :mod:`~precis.pcb.connectivity`
  (is each net's copper one piece).
* **Out** — :mod:`~precis.pcb.gerber` (Gerber X2 + Excellon),
  :mod:`~precis.pcb.svg`, :mod:`~precis.pcb.gerber_view` (render the board
  from its gerbers), :mod:`~precis.pcb.schematic`, :mod:`~precis.pcb.export`
  (the text/dict exporters), :mod:`~precis.pcb.route` (Freerouting via
  Specctra, optional).

Dependencies: shapely is imported at module top by ``drc``, ``generators``,
``gerber``, ``ir``, ``planes``, ``realize`` and ``tiling`` — it is a core
dependency (pyproject), not confined to the tiling pass any more.
``geom.py`` stays dependency-free by convention because its primitives
are closed-form, not because the package as a whole is. No GL, no
meshing, no embedder. The handler (:mod:`precis.handlers.pcb`) renders
results as TOON; this package owns the algorithms.
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
