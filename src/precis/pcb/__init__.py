"""The PCB kernel — the whole place-and-route pipeline in pure Python,
from a netlist to gerbers, over one progressively-enriched IR.

The keystone-kind philosophy (shared with cad and structure): own a
legible IR the LLM reads as structure — a circuit is already a graph, a
board already placed rectangles — and keep every stage a pure function
over it so each is unit-testable in isolation. Export is the one place a
design leaves the relational graph.

``pcb_review`` works the shipped ``pcb-tapeout`` and conditionally assigned
``pcb-mech-env`` judgment ledgers through the same checklist sync/status/write
path. Mechanical review is declaration-driven (flex process, fab features or
board metadata), so a plain rigid board gains no implicit mechanical gate.
Mechanical verdicts cover the whole board and consume the ``pcb:mech-env``
input revision: pcb_graph omits fab polygons, copper and footprint geometry,
so its fingerprint alone cannot invalidate a mask/coverlay decision. Missing
geometry records ``not checkable (gr520329)``, never an inferred pass.

**Routing is retired** (Reto 2026-10-25): the vector router
(:mod:`~precis.pcb.vroute`) was deleted — main keeps tag ``vroute-final``
for its last build. Topological routing (topo slice 2,
:mod:`~precis.pcb.topo`) is the only router and its realiser is not
landed yet, so new boards get no routed copper: :func:`pcb.local.route`
and ``put(kind='pcb', args={'op':'route'})`` refuse with
:data:`ROUTE_RETIRED`, and :mod:`~precis.pcb.realize` keeps only the pad
geometry + stored-copper model + gerber-model slice that placement, DRC,
fab render/export and stored runs still need.

Stored ``meta.last_route.warm_start`` records each group's method and
distance in mm for boards routed before the cut — matched roles also
carry `cost_mm`/`prior_cost_mm`, which `format_warm_start` renders into
`view='route-status'` (stored runs only).

Footprint-owned geometry (pads, polygon vertices, holes, courtyard) is
stored footprint-local and only mapped to world through :mod:`~precis.pcb.pose`
(mirror-X for bottom, rotate clockwise, translate); never persist a part-owned
absolute position, or the part moves and it stays behind.

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
  :mod:`~precis.pcb.capabilities` (fab rules as versioned data),
  :mod:`~precis.pcb.datasheets` (the ``datasheet_pull`` trigger + read
  side) and :mod:`~precis.pcb.datasheet_facts` (the extracted pin table /
  required externals / conditional-table block on the part ref —
  ``get(kind='part', view='facts')`` reads it, the ``datasheet_facts``
  job writes it).
* **Place** — :mod:`~precis.pcb.place` (force-directed seed),
  :mod:`~precis.pcb.optimize` (the placement annealer, the
  package's largest engine), :mod:`~precis.pcb.cost` (its one cost
  function), :mod:`~precis.pcb.pinswap`, :mod:`~precis.pcb.generators`
  (computed components such as EWOD arrays), :mod:`~precis.pcb.session`
  (IR↔DB glue for the ``pcb_place`` job).
  A fully placed, legal input stays the incumbent: when the placed
  result's T1+D14 wirelength lands >2% worse than the input's,
  ``optimize`` restores the input poses (gr504889).
* **Copper** — :mod:`~precis.pcb.realize` (pad geometry, the
  stored-copper model, the gerber-model assembly — the router that drew
  new copper is deleted),
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
  (the text/dict exporters, Specctra ``.dsn`` included),
  :mod:`~precis.pcb.jlc_order` (JLCPCB upload → quote →
  create — the guarded unpaid-order step; docs/backlog/pcb-jlc-order.md).
  :mod:`~precis.pcb.board_script` also emits local workspace Python from
  a batch without DB or catalog access (``scripts/pcb-batch-to-script``).
  It retains library footprint names and deduplicates supplied local pad
  sets as marked synthesized fallbacks: a name cannot establish equivalent
  geometry offline. The CLI locks supplied positions by default; ``omit``
  removes poses and their constraints for local placement. The emitter's
  original default still preserves authored placement flags for readback.
* **Argue** — :mod:`~precis.pcb.argue`: the handle grammar (``U1``,
  ``U1.3``, ``net:GND``, ``feature:outline``) the fab render stamps as
  ``data-handle`` and the board page's text box submits; resolution
  against the stored board, the one LLM turn, the ``pcb_notes`` ledger
  (``se_notes``' shape). Handles are name-keyed text resolved at read
  time: a clicked handle is checked at submit (a stale one is refused with
  the roster), a stored one that stops resolving is a report, not an
  error.

Fresh local EasyEDA Pro intake retains accepted LINE/ARC records and ordinary
through vias as fixed source copper, so preview does not erase existing routing
(gr470192). This supersedes the older regenerate-only default, still available
as copper="none". Partial updates retain that older behavior: their intentionally
unchanged nets/outline cannot safely receive changed copper. Import and source
copper share one transaction; neither the reader nor preview invents routing.

The web selection adapter binds actual instance IDs and shown raw snapshots to
Gerber flash targets; refdes and footprint UUID/design-local key remain separate.
Unknown identity stays unavailable. Shared design chat proposes a single move;
explicit Apply uses the existing handler under the bounded cache/row guard and
rejects any generator, copper or annotation collateral. Replay-normalized IDs
and routing hashes were refused as viewer identity because they omit those facts.

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

The route pitch cap retains two-thirds of clearance, bounded by
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

Slice 2 of docs/backlog/pcb-lazy-netlist-and-checks.md then closed the other
direction: every footprint pad NAME is also a declared pin —
``_pcb_apply`` completes a component's pins from the cached footprint's
``pin_map`` (``store._pcb_ops._pcb_complete_footprint_pins``), so the
netlist can name every land while pads still outnumber pins wherever two
pads share one name. A declared pin with no net and no §1j marker is
``drc.check_unconnected_pins`` (warn while the board is worked, error at
the authored ``fab`` phase); a pad no pin claims at all is
``drc.check_undeclared_pads`` — and a part whose footprint is not cached
is reported NOT-CHECKED, never a clean pass. The marker vocabulary
(``nc``/``unused``/``must_terminate``, on ``pcb_pins.meta`` + the ``nc``
tag) is what keeps that complaint an oracle and not noise.

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

#: Reto 2026-10-25: the vector router (vroute) is deleted (tag
#: ``vroute-final``); topological routing (topo slice 2) is the only
#: router and its realiser is not landed yet. ``op='route'`` and
#: ``pcb.local.route`` refuse with this pair — stored copper and stored
#: route statuses for boards routed before the cut stay legible.
ROUTE_RETIRED = (
    "pcb: routing is retired — the vector router (vroute) was deleted "
    "(tag vroute-final); the topological router is the only router and "
    "its realiser is not landed yet"
)
ROUTE_RETIRED_NEXT = (
    "put(args={'op':'place'}) for placement; routed copper is not available yet"
)

#: The closed §1i electrical-role vocabulary a pin may declare (Slice 5 of
#: docs/backlog/pcb-lazy-netlist-and-checks.md): ``power_in``/``power_out``
#: (supply pins), ``input``/``output``, ``bidi``, ``passive`` (two-pin
#: passives), ``open_drain``/``tristate`` (drive-shareable), ``nc``.
#: Validated at ``_pcb_apply``, lands in ``pcb_pins.tags`` — ``nc`` doubles
#: as the §1j marker since a no-connect IS the electrical fact. It lives
#: here, not in ``drc.py``, because ``ir.py`` reads it too and
#: ``ir → drc → rules → ir`` would be a cycle.
PIN_ETYPES: tuple[str, ...] = (
    "power_in",
    "power_out",
    "input",
    "output",
    "bidi",
    "passive",
    "open_drain",
    "tristate",
    "nc",
)

#: The closed ``current_role`` vocabulary a pin may declare
#: (``pcb_pins.meta``, via ``op='pins'`` — docs/backlog/
#: pcb-datasheet-review.md slice 2, decisions log 2026-10-13): whether
#: the pin sits IN the net's current path. ``series`` carries the net's
#: through current (a fuse, switch, regulator pass device, connector
#: contact); ``shunt`` taps it without carrying it (a decoupling cap, a
#: TVS) and is the one exemption ``drc.check_over_current`` honours;
#: ``signal`` is an endpoint that drives or sinks only the signal's own
#: current. ``pcb-component-model.md`` keeps the defaulting rule that
#: also reads it (declared net current wins; else MAX over
#: series-capable pins).
PIN_CURRENT_ROLES: tuple[str, ...] = ("series", "shunt", "signal")

#: Footprint-graphics ceilings (gr520150): a vendor file can carry
#: thousands of primitives per footprint — past these caps the overflow
#: is reported as dropped rather than stored or rendered. Enforced by
#: both decoders (epro/easyeda) and by
#: :func:`precis.pcb.rows.normalize_footprint_graphics`.
MAX_FOOTPRINT_GRAPHICS = 500
MAX_GRAPHIC_POINTS = 2000

#: Ceramic-capacitor dielectric classes, ranked by capacitance stability —
#: lower number is better (docs/backlog/pcb-datasheet-review.md in-scope
#: 5: "dielectric rank C0G > X7R > X5R > Y5V/Z5U"). ``np0`` is the IEC
#: spelling of the same C0G material and shares its rank. A ``min_
#: dielectric`` pin annotation (``op='pins'``) is validated against these
#: keys, and ``drc.check_passive_rating`` compares a parsed catalog
#: dielectric's rank against a declared minimum's.
CAP_DIELECTRIC_RANK: dict[str, int] = {
    "c0g": 0,
    "np0": 0,
    "x8r": 1,
    "x8l": 1,
    "x7r": 1,
    "x7s": 2,
    "x6s": 2,
    "x5r": 2,
    "x5s": 2,
    "y5v": 3,
    "z5u": 3,
}

#: The margin policy ``drc.check_passive_rating`` derates passives by
#: (slice 3, decisions log 2026-10-13): a short NAMED policy, not a
#: table — the manufacturer derating table overrides it later per
#: manufacturer/family, and the revision is what slice 4's verdict
#: inputs consume. Fixed policy: a capacitor's rated voltage must be ≥
#: factor × the voltage across it (2× for X5R/X7R-class and anything
#: unrated; 1.5× for C0G/NP0, whose capacitance barely falls with bias),
#: and a resistor may dissipate at most 50 % of its rated power.
PASSIVE_MARGIN_POLICY = "fixed-derating"
PASSIVE_MARGIN_REVISION = 1
PASSIVE_CAP_VOLTAGE_FACTOR: dict[str, float] = {"c0g": 1.5, "np0": 1.5}
PASSIVE_CAP_VOLTAGE_FACTOR_DEFAULT = 2.0
PASSIVE_RESISTOR_POWER_FRACTION = 0.5

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


def format_warm_start(report: list[dict[str, Any]] | None) -> list[str]:
    """Display lines for ``meta.last_route.warm_start`` — one per matched
    supply role (gr491373: the matcher stores ``cost_mm`` vs
    ``prior_cost_mm`` and nothing ever showed it)::

        pin matching drv: 2 client(s), 41.23 mm as wired → 10.00 mm
        matched (-31.23 mm)

    Entries without both costs — the radial/distance per-refdes warm-start
    rows (``{refdes, method, distance_mm}``) — render nothing. Shared by
    the ``pcb_route`` job summary and ``view='route-status'`` so the two
    cannot drift."""
    lines: list[str] = []
    if not isinstance(report, list):
        # ``refs.meta`` is user-writable verbatim — a hand-authored scalar
        # ``warm_start`` must not crash ``view='route-status'``.
        return lines
    for entry in report:
        if not isinstance(entry, dict):
            continue
        cost, prior = entry.get("cost_mm"), entry.get("prior_cost_mm")
        if not isinstance(cost, (int, float)) or not isinstance(prior, (int, float)):
            continue
        # bool is an int subclass; a True cost must not render as ``1.00 mm``.
        if isinstance(cost, bool) or isinstance(prior, bool):
            continue
        clients = entry.get("clients")
        lead = (
            f"{clients} client(s), "
            if isinstance(clients, int) and not isinstance(clients, bool)
            else ""
        )
        lines.append(
            f"pin matching {entry.get('role')}: {lead}"
            f"{prior:.2f} mm as wired → {cost:.2f} mm matched "
            f"({cost - prior:+.2f} mm)"
        )
    return lines


#: Route cost of In1.Cu in the hybrid preset, as a multiple of the base
#: step (pcb-stackup-regions rulings: "about 2-3x", "high cost does not need
#: to be massively high"). Slice 1's table could not tune it (no board there
#: routed on In1), so this is the middle of the ruled band.
HYBRID_IN1_ROUTE_COST = 2.5

#: The net the stackup presets fill with and plane on. ``op='stackup',
#: preset=, ground=`` substitutes another name for a board whose ground net
#: is called something else.
PRESET_GROUND_NET = "GND"

#: Named stackups ``put(args={'op':'stackup','preset':...})`` writes
#: (pcb-stackup-regions slice 3, Reto's rulings 2026-10-16):
#:
#: - ``4l-two-sided`` — the hybrid (:data:`DEFAULT_STACKUP` instead routes
#:   all four layers and pours nothing — the 2026-10-23 no-flood-fill
#:   ruling). F.Cu/B.Cu
#:   signal with a GND ``fill``; In1.Cu a GND plane that also routes at
#:   :data:`HYBRID_IN1_ROUTE_COST`; In2.Cu carries derived power regions
#:   (``power_regions``) with GND filling whatever no region claims. Sides
#:   on (slice 5 reads the board's ``placement_sides``).
#: - ``4l-top-only`` — the same copper, parts on the top side only.
#: - ``2l`` — two signal layers, each with a GND fill.
#:
#: ``direction`` gives each routable layer its axis (h/v alternating down the
#: stack, so a via between neighbours always hands off between axes).
STACKUP_PRESETS: dict[str, list[dict[str, Any]]] = {
    "4l-two-sided": [
        {"name": "F.Cu", "role": "signal", "direction": "h", "fill": "GND"},
        {
            "name": "In1.Cu",
            "role": "plane",
            "plane_net": "GND",
            "routable": True,
            "direction": "h",
            "route_cost": HYBRID_IN1_ROUTE_COST,
        },
        {
            "name": "In2.Cu",
            "role": "plane",
            "direction": "v",
            "fill": "GND",
            "power_regions": True,
        },
        {"name": "B.Cu", "role": "signal", "direction": "v", "fill": "GND"},
    ],
    "2l": [
        {"name": "F.Cu", "role": "signal", "direction": "h", "fill": "GND"},
        {"name": "B.Cu", "role": "signal", "direction": "v", "fill": "GND"},
    ],
}
STACKUP_PRESETS["4l-top-only"] = [dict(e) for e in STACKUP_PRESETS["4l-two-sided"]]

#: Which board sides a preset places parts on (``pcb_boards.meta
#: ['placement_sides']``; slice 5's side flip reads it).
PRESET_SIDES: dict[str, str] = {
    "4l-two-sided": "both",
    "4l-top-only": "top",
    "2l": "both",
}


def stackup_preset(
    name: str, *, ground: str = PRESET_GROUND_NET
) -> list[dict[str, Any]]:
    """A fresh copy of preset ``name`` with its ground net renamed to
    ``ground``. Raises ``KeyError`` for an unknown name."""
    out = []
    for entry in STACKUP_PRESETS[name]:
        e = dict(entry)
        for key in ("plane_net", "fill"):
            if e.get(key) == PRESET_GROUND_NET:
                e[key] = ground
        out.append(e)
    return out


#: The default stackup every board is born with: all four layers route,
#: **none pours**. Reto's ruling 2026-10-23 (me507871, superseding the
#: 2026-10-17 "each layer should be filled" ruling, gr495395 closed): "no
#: fill, we just fill with traces. (no floodfil.)" — copper area comes
#: from routed traces widened by net property, not a flood pour, so the
#: GND ``fill``/``power_regions`` declarations bb1265dbf and 527ce6d63
#: put here are gone. Pours are opt-in: a board authors one with
#: ``op='plane_net'``/``op='stackup'`` (a ``plane_net``/``fill``/
#: ``power_regions`` key on its own stackup), a stackup preset, or an
#: import's ``pcb_planes`` rows; boards born before this change keep
#: their stamped stackup, fills included. Migration 0138's backfill
#: literal is the even older v1 one; no column default reads this.
#:
#: **``role`` answers two questions; the explicit flags split them.**
#: :func:`precis.pcb.ir.layer_is_routable`/:func:`~precis.pcb.ir.
#: layer_is_pourable` answer "may this layer carry a routed trace" and
#: "may the AUTOMATIC annealer choose this layer to pour on its own" as
#: two INDEPENDENT questions, falling back to ``role`` when the key is
#: absent. A human ``op='plane_net'`` instruction is honoured on any layer
#: regardless of either flag; only the annealer's own guesses are gated.
#:
#: The other entry keys (:data:`precis.pcb.ir.STACKUP_LAYER_KEYS`):
#: ``plane_net`` (the layer's whole-board plane), ``fill`` (the net that
#: pours whatever area no plane or region claims, after routing),
#: ``power_regions`` (derive power-net regions here,
#: :func:`precis.pcb.planes.derive_regions`), ``direction``, ``reference``,
#: ``route_cost``, and ``pour_nets`` — net(s) an import found carried by a
#: board-covering fill on a signal layer (gr486958).
DEFAULT_STACKUP: list[dict[str, Any]] = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "signal", "routable": True},
    {"name": "In2.Cu", "role": "signal", "routable": True},
    {"name": "B.Cu", "role": "signal"},
]
