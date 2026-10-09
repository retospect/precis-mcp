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

Route warm starts use Hungarian Manhattan-distance assignment from connected
fixed-copper via terminals to admissible channel pads. Replay with In2.Cu/B.Cu
routed 51/55 versus the radial start's 42/55 (mx456/mx457, td472840): fewer
crossings alone did not predict routing yield. `warm_start="radial"` retains
that alternative; incomplete terminal geometry falls back per group. A shorter
Euclidean assignment is accepted only at the same Manhattan optimum (four-ULP
sum tolerance): this preserves the older EWOD routing floor without losing
the replay gain. Coordinate row ordering was refused after it fell to49/55. The
anneal's crossing move and authored-baseline swap persistence stay unchanged;
`last_route.warm_start` records each group's method and distance in mm.

Negotiated congestion remains a per-route opt-in, never a global default.
Its handler and realizer supported the knob before the job params contract
registered it; that omission refused every advertised request at enqueue
(gr469872). Registering the existing parameter fixes reachability without
changing copper policy, the 60-second cap, or off-by-default routing. The
loop reports what it did (``realize.NegotiationReport``: iterations,
conflict trail, budget, proposals committed, whether its result was taken)
and the route job prints that line — on the EWOD dogfood board's B.Cu lock
it never converges (ewod-pcb thread, 2026-10-07).

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

Fresh local EasyEDA Pro intake retains accepted LINE/ARC records and ordinary
through vias as fixed source copper, so preview does not erase existing routing
(gr470192). This supersedes the older regenerate-only default, still available
as copper="none". Partial updates retain that older behavior: their intentionally
unchanged nets/outline cannot safely receive changed copper. Import and source
copper share one transaction; neither the reader nor preview invents routing.

Internal :mod:`~precis.pcb.snapshot` captures raw relational routing inputs and
checkpoints for dev/test replay. Public graph/hash projections omit geometry and
copper intentionally; generator re-expansion and authoring inference were refused
as clone mechanisms because they change the board under diagnosis. Replay creates
a fresh slug atomically and refuses production targets or conflicting shared caches.
Source-absent referenced catalog/footprint keys must also stay absent: target cache
knowledge would otherwise change graph flags or geometry on the replayed board.
Deleting target data or inventing source placeholders was refused.

Dependencies: shapely is imported at module top by ``drc``, ``generators``,
``gerber``, ``ir``, ``planes``, ``realize`` and ``tiling`` — it is a core
dependency (pyproject), not confined to the tiling pass any more.
``geom.py`` stays dependency-free by convention because its primitives
are closed-form, not because the package as a whole is. No GL, no
meshing, no embedder. The handler (:mod:`precis.handlers.pcb`) renders
results as TOON; this package owns the algorithms.

The maze-grid pitch cap retains two-thirds of clearance, bounded by grid_for's
existing pitch floor. A one-third experiment gained one net on the faithful
dogfood replay (12 gains/11 losses, zero geometric DRC errors), but regressed
ESP32 reference seed5 and fab-film seeds2/3/4. The global refinement was withdrawn:
deterministic routing competition changes with the grid, so finer is not a
monotonic yield improvement. The measured fine-grid fixture remains an explicit
experiment, not a default or new API. Clearance/layers/fixed copper, all route/DRC
ratchets and search budgets stay authoritative; diagnostic parity is separate.

Pads are not pins (gr451276). A pin is a netlist name; a pad is footprint
copper keyed by pad number, carried as the IR's pad set
(``ir.FootprintPad``/``footprint_pad_set``, memoised on
``PcbIR.footprint_pads`` by ``session.apply_real_pin_offsets``). Pins join
pads by name, first pad per name supplying the pin's own position/size;
every other pad of a real footprint is emitted by ``realize.pads_for_ir``,
claimed on the router grid (``_footprint_pad_claims``, under its pin's net
or a per-land sentinel) and hulled by the courtyard, so pad count equals the
footprint's regardless of wiring. Pads were never promoted to pins because
pin ids carry pin swaps, net indexing and pin_to_net-by-name. Connectivity
unions the pads of one pin: the part bonds them internally, so a second lead
is not an island of its net. Alternatives refused: seeding pins from the
footprint (breaks pin identity), and net-less extra pads (made a pin's own
second land a foreign obstacle to its net, measured on GND).

Instance pinout inspection (``eyes.pinout``) reads raw stored pads and
reuses ``padplace.place_pad_point``. IR/fab pads were refused as its input:
synthesized bounds cannot establish numbering, and per-layer flashes
multiply through-hole lands. Explicit pin-to-pad and footprint naming are
reported separately rather than changing routing's mapping policy. Missing
placement leaves board coordinates unavailable; source geometry does not
verify a vendor pinout or connector mating orientation.

Unsaved intake preview overlays explicit proposed labels on those same stored
rows, separately from persisted pin/net evidence. It never authors the draft:
canonical ownership conflicts remain inspectable proposals, while authoring
still refuses them. Prose/numbering/mating/net inference and provider fetches
were refused; a read-only echo is not a routing or alias-storage policy.
"""

from __future__ import annotations

from typing import Any

#: The note ``pcb_route`` writes on a dangling net (fewer than 2 members,
#: nothing to route). The row's status is ``'realized'`` (so route_complete
#: is never wedged), so this prefix is the ONE marker that tells a read-side
#: summary the net was never actually routed. Writer and both readers
#: (``pcb_route`` job, ``view='route-status'`` header, board ``## route
#: status``) use it, so they cannot drift.
DANGLING_NET_NOTE_PREFIX = "dangling net"
DANGLING_NET_NOTE = f"{DANGLING_NET_NOTE_PREFIX} (<2 members) — nothing to route"

#: The pseudo-status a dangling net counts under in a route-status summary.
ROUTE_DANGLING = "dangling"


def route_summary_status(status: str | None, note: str | None) -> str:
    """The summary bucket for one net: its stored status, except a dangling
    net (stored ``'realized'``) which counts as ``'dangling'``."""
    st = status or "unrouted"
    if st == "realized" and (note or "").startswith(DANGLING_NET_NOTE_PREFIX):
        return ROUTE_DANGLING
    return st


def format_route_summary(counts: dict[str, int]) -> str:
    """``"58 net(s): 29 routed, 26 failed, 3 dangling (fewer than 2 pins,
    nothing to route)"`` -- routed, failed, any other status (sorted), then
    dangling. ``'realized'`` reads as ``routed`` here only."""
    total = sum(counts.values())
    order = ["realized", "failed"]
    order += sorted(
        k for k in counts if k not in ("realized", "failed", ROUTE_DANGLING)
    )
    order.append(ROUTE_DANGLING)
    parts: list[str] = []
    for k in order:
        n = counts.get(k, 0)
        if not n:
            continue
        if k == "realized":
            parts.append(f"{n} routed")
        elif k == ROUTE_DANGLING:
            parts.append(f"{n} dangling (fewer than 2 pins, nothing to route)")
        else:
            parts.append(f"{n} {k}")
    return f"{total} net(s): {', '.join(parts)}"


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
