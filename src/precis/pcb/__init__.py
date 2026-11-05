"""The PCB kernel — the whole place-and-route pipeline in pure Python,
from a netlist to gerbers, over one progressively-enriched IR.

The keystone-kind philosophy (shared with cad and structure): own a
legible IR the LLM reads as structure — a circuit is already a graph, a
board already placed rectangles — and keep every stage a pure function
over it so each is unit-testable in isolation. Export is the one place a
design leaves the relational graph.

Inspection and run intake: :mod:`~precis.pcb.inspection`, :mod:`~precis.pcb.workspace`.

Mechanical review is declaration-driven and consumes the whole-board
``pcb:mech-env`` revision because the graph omits fabrication geometry.
Missing geometry is not checkable, never an inferred pass. Ledger and metadata
details: ``docs/reference/pcb-kernel-contracts.md#mechanical-review``.

Placement and route refuse an unset net current (gr520545 —
:mod:`~precis.pcb.currents`).
Placement also refuses negative outline coordinates (gr520794).
``session.require_layout_ready`` owns both checks for handler enqueue,
worker dispatch and offline place/topo-place (gr543693): a direct local
job must not bypass input gates, nor may a queued job trust an earlier
check of board geometry that could have changed before it runs.

**Routing is topological** (Reto 2026-10-25): the vector router
(:mod:`~precis.pcb.vroute`) was deleted — main keeps tag ``vroute-final``
for its last build. Topological routing (topo slice 2,
:mod:`~precis.pcb.topo`, realiser :mod:`~precis.pcb.topoplace.realise`)
is the only router: it runs inside the ``placer='topo'`` job
(``route=True``) and offline through ``pcb.local.route``/``topo_place``.
The standalone ``put(kind='pcb', args={'op':'route'})`` job is retired —
it refuses with :data:`ROUTE_RETIRED` — and
:mod:`~precis.pcb.realize` keeps only the pad geometry + stored-copper
model + gerber-model slice that placement, DRC, fab render/export and
stored runs still need.

Stored warm-start status fields:
``docs/reference/pcb-kernel-contracts.md#stored-route-status``.

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
* **Parts + footprints** — :mod:`~precis.pcb.catalog` (jlcparts),
  :mod:`~precis.pcb.jlc_api` (stock), :mod:`~precis.pcb.easyeda` /
  :mod:`~precis.pcb.footprint` (geometry), :mod:`~precis.pcb.padplace`
  (world placement), :mod:`~precis.pcb.landpattern` (synthesized fallback),
  :mod:`~precis.pcb.escape` (intrinsic escape precompute),
  :mod:`~precis.pcb.capabilities` (versioned fab rules),
  :mod:`~precis.pcb.datasheets` / :mod:`~precis.pcb.datasheet_facts`
  (datasheet pull and extracted facts).
  Copper and physical drills stay distinct: a filled envelope cannot prove
  contact in a bore. DRC approves the boundary actually emitted; analytic
  contact and outward blockers prevent facets from inventing or erasing joins.
  Full geometry contract:
  ``docs/reference/pcb-kernel-contracts.md#footprint-geometry``.
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
  Silk's Voronoi guard sites prevent macOS outer-ray clipping defects; see
  :mod:`~precis.pcb.silk`.
  Vendor silk selection measures printable, geometrically surviving ink
  against the ORIGINAL submitted length, including strokes below the fab's
  ``jlc_min.silk_width_mm``. Removing thin ink from that denominator would
  let a tiny legal tail suppress the existing minimal-ring fallback.
  Accepted vendor strokes retain their authored widths; callers without
  an authoritative minimum keep their existing behavior.
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
  a batch without DB or catalog access (``scripts/pcb-batch-to-script``);
  its module docstring owns the placement/plane/measure normalization,
  footprint-cache and provenance rules. ``examples/boards/nano`` is the
  generated offline dogfood workspace; the former ``nano_usb_pd`` path
  delegates to it after batch/check golden equivalence was established.
* **Argue** — :mod:`~precis.pcb.argue`: the handle grammar (``U1``,
  ``U1.3``, ``net:GND``, ``feature:outline``) the fab render stamps as
  ``data-handle`` and the board page's text box submits; resolution
  against the stored board, the one LLM turn, the ``pcb_notes`` ledger
  (``se_notes``' shape). Handles are name-keyed text resolved at read
  time: a clicked handle is checked at submit (a stale one is refused with
  the roster), a stored one that stops resolving is a report, not an
  error.

Intake, viewer identity, connectivity previews, dependency boundaries,
route-pitch experiments and pad/pin contracts:
``docs/reference/pcb-kernel-contracts.md#intake-and-pad-contracts``.
Fresh Pro intake preserves accepted fixed source copper; partial updates
cannot safely replace it. Viewer Apply binds actual instance identity and raw
snapshots, rejecting collateral changes. Airwires join existing copper
components because a pending net may already contain connected pins.

Pins retain netlist identity; pads retain every physical land, including
multiple lands bonded to one pin. Promoting pads to pins breaks swaps and
name-based net indexing; leaving extra lands net-less creates foreign
obstacles on their own net. Footprint pin completion exposes every land name
without merging these identities. Uncached geometry is NOT-CHECKED.
Pinout inspection reads raw stored pads because synthesized bounds cannot
prove numbering and per-layer flashes duplicate through-hole lands.
Unsaved label overlays remain proposals, never inferred vendor pinouts.

``geom.py`` stays dependency-free for its closed-form primitives; shapely is
a core package dependency. The handler renders TOON; this package owns the
algorithms. Internal :mod:`~precis.pcb.snapshot` owns raw-input replay rules.
The route pitch cap retains two-thirds of clearance: finer grids change
deterministic competition and do not monotonically improve routing yield.
Existing floors, rules and budgets remain authoritative.
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
#: ``vroute-final``); topological routing (topo slice 2,
#: :mod:`precis.pcb.topoplace.realise`) is the only router — it runs
#: inside the ``placer='topo'`` job (``route=True``) and offline via
#: ``pcb.local.route``/``topo_place``. The standalone ``op='route'``
#: job stays refused with this pair — stored copper and stored route
#: statuses for boards routed before the cut stay legible.
ROUTE_RETIRED = (
    "pcb: routing is retired for op='route' — the vector router (vroute) "
    "was deleted (tag vroute-final); topological routing is the only router"
)
ROUTE_RETIRED_NEXT = (
    "put(args={'op':'place','placer':'topo','route':True}) places and "
    "routes via topo; offline: pcb.local.route(board)"
)

#: The closed §1i electrical-role vocabulary a pin may declare (Slice 5 of
#: docs/backlog/pcb-lazy-netlist-and-checks.md): ``power_in``/``power_out``
#: (supply pins), ``input``/``output``, ``bidi``, ``passive`` (two-pin
#: passives), ``open_drain``/``tristate`` (drive-shareable),
#: ``analog_in``/``analog_out`` (gr520810 — direction stays one axis, so
#: analog joins it rather than a parallel signal-kind field), ``nc``.
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
    "analog_in",
    "analog_out",
    "nc",
)

#: The etypes whose electrical facts are current/voltage ratings — they
#: carry ``max_current_a`` + ``max_voltage_v`` and never ``fmax_hz``
#: (Reto, gr520810 comment 4: a supply pin's AC content is a decoupling
#: question, not a bandwidth). Every other etype carries ``fmax_hz``
#: (``nc`` carries no facts at all). Mirrored in
#: ``precis_geom.pcb._POWER_ETYPES`` — that module is stdlib-only.
PIN_POWER_ETYPES: tuple[str, ...] = ("power_in", "power_out")

#: ``pin_functions`` tokens that prove a candidate pin satisfies a flex
#: slot's analog class (gr520810): ``analog_in`` slots land only on pins
#: the table marks AIN/ADC-capable, ``analog_out`` only on DAC-capable
#: ones; a digital-etype slot never lands on a pin whose listed functions
#: are all analog. Read by ``topoplace.pins._effective``; authored-side
#: (builder) etypes are in ``precis_geom.pcb``.
PIN_FN_ANALOG_IN = "ain|adc|analog"
PIN_FN_ANALOG_OUT = "dac|aout|analog_out"

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
