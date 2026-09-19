"""pcb-ewod-multitile Slice 2 rounds 3-4, pcb-pre-place-route-blocks Slice
2 — DRC rules against REAL generator output, no DB.

Round 3: ``precis.handlers.pcb.PcbHandler._render_drc`` gated the whole
``view='drc'`` on ``pcb_copper_list`` (router-realized copper) being
non-empty — a pre-route-DRC contract from ``pcb-guided-place-route``,
predating this generator. A standalone ``ewod_pad_array`` board has every
net at fanout 1 (its own single pin), so ``op='route'`` never wrote it a
single ``pcb_copper`` row even once run (a dangling <2-member net is
marked ``'realized'`` in ``pcb_routes`` only — it never has segments), so
the handler-gated view could never run a single DRC rule against a bare
EWOD board. **Round 4 widened that gate**: it runs a pads-only pass
whenever REAL (non-synthesized) pad geometry exists, labelling the
reduced scope. **pcb-pre-place-route-blocks Slice 2 retires the
motivating gap**: the escape fabric (neck track + plaza via) is now real
``pcb_fixed_copper``, which ``pcb_copper_list`` unions in — a standalone
EWOD board now has realized copper from the moment ``generators:[...]``
is applied, so ``view='drc'`` runs the FULL pass, not the reduced one
(``test_pcb_ewod_generator.py``'s own handler-level contract test).

These tests still drive :mod:`precis.pcb.drc`'s rules directly against
the SAME board-coordinate geometry the production path emits — pads via
:func:`precis.pcb.padplace.board_pads` (now electrode BODIES only, one
per pin), copper via :attr:`~precis.pcb.generators.GeneratorExpansion.
copper` flattened the same way :meth:`precis.store._pcb_ops.PcbMixin.
pcb_fixed_copper_list` flattens a stored row — independent of the
handler/DB layer, the fastest, most targeted way to stress specific
rule/geometry interactions (e.g. the exact-geometry sweeps below) without
paying for a store round-trip per case. See ``test_pcb_ewod_generator.py``
for the store/handler wiring layer and
``test_pcb_ewod_generator_geometry.py`` for pure zigzag/gap geometry.

**Two pcb-pre-place-route-blocks Slice 2 geometry residues — both now
CLOSED (docs/backlog/pcb-pre-place-route-blocks.md's own "geometry
residues" item; the first by docs/backlog/pcb-ewod-multitile.md
"Rulings 2026-09-18" item 1):**

1. **RULE-DERIVED corridor closes the diagonal-escape stub gap.** At
   default sizing, a diagonal escape's neck TRACK used to read a couple
   hundredths of a mm under the fab's absolute clearance floor against
   its two flanking neighbours' bodies (the round-4 taper-clearance fix
   was calibrated for a near-zero-width taper; Slice 2's track is
   constant-width the whole way, and the corridor round 4's
   ``plaza_corner_chamfer`` opened was a fixed ``gap + 0.01`` margin, too
   narrow to host a fab-minimum track plus two fab clearances). Two
   fixes were tried against that fixed margin and rejected: widening it
   as a flat constant (regressed an UNRELATED zigzag-wall clearance,
   because the meshing wall's own zero-deflection margin never grew to
   follow it), and narrowing ``stub_width`` below the fab's minimum trace
   width (emitted 0.038mm copper JLC cannot etch). The ruling instead
   sizes the chamfer FROM the corridor a fab-legal stub actually needs
   (``stub_width_floor + 2*trace_spacing_floor + slack`` —
   ``precis.pcb.generators.resolve_ewod_sizing``'s own
   ``plaza_corner_chamfer`` derivation) and re-solves the meshing wall's
   own margin against the resulting (much larger) chamfer
   (``_edge_sign``'s ``margin_t0``/``margin_t1``, and the MIRROR-side
   widening ``_needs_diagonal_margin_widen`` adds for a corner electrode
   that doesn't itself chamfer but shares the same physical corner point
   with one that does) — the stub-vs-flanking-body gap and the
   previously-unexplained zigzag regression turn out to be the SAME
   under-solved margin, fixed together.
2. ``check_via_pad_keepout``'s PRE-EXISTING circumscribed-circle pad
   approximation over-states a LARGE polygon pad's (an electrode body's)
   effective radius enough that a plaza via reads as a keepout violation
   against its OWN net's body — invisible before Slice 2 because the via
   was a pad, never a ``copper`` via row this check iterates at all.
   Fixed with a narrow same-net-and-``fixed`` exemption
   (``drc.py::check_via_pad_keepout``'s own docstring); a foreign-net or
   router-placed via against the same pad stays enforced
   (``test_router_via_landing_on_an_electrode_pad_still_errors`` below).
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from shapely.geometry import Point as SPoint  # type: ignore[import-untyped]
from shapely.geometry import Polygon

from precis.pcb import DEFAULT_STACKUP, drc
from precis.pcb import generators as pcb_generators
from precis.pcb.capabilities import capability_for
from precis.pcb.padplace import board_pads
from precis.pcb.rules import resolve_net_rules
from precis.store._pcb_ops import _normalize_local_footprint

_CAP4 = capability_for("4layer")
_LAYERS = [str(layer["name"]) for layer in DEFAULT_STACKUP]


def _pin_for_net(name: str, net: str) -> str:
    prefix = f"{name}_"
    assert net.startswith(prefix), (net, prefix)
    return net[len(prefix) :]


def _flatten_copper(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``expansion.copper``'s nested ``{ctype, layer, net, geom, ...}``
    shape flattened to the ``{ctype, layer, net, **geom}`` shape
    :mod:`precis.pcb.drc` (and every other ``model["copper"]`` consumer)
    expects — the same flattening :meth:`precis.store._pcb_ops.PcbMixin.
    pcb_fixed_copper_list` does at DB read time, reproduced here since
    these tests never touch the DB. ``fixed: True`` on every row too —
    every ``expansion.copper`` row IS this generator's own authored fixed
    copper once stored (``pcb_fixed_copper_list``'s own "authored rows
    carry fixed: True" marker), which ``drc.py::check_via_pad_keepout``'s
    same-net exemption (geometry residues) reads."""
    out = []
    for r in rows:
        item = {k: v for k, v in r.items() if k not in ("geom", "envelope", "meta")}
        item.update(r.get("geom") or {})
        item["fixed"] = True
        out.append(item)
    return out


def _ewod_model(
    **params: Any,
) -> tuple[pcb_generators.GeneratorExpansion, dict[str, Any]]:
    """One ``ewod_pad_array`` expansion, placed at the origin, in the
    EXACT board-coordinate geometry a real routed board's ``model`` would
    carry — pads via the real ``_normalize_local_footprint``/
    ``board_pads`` production path (electrode BODIES only, pcb-pre-place-
    route-blocks Slice 2), copper via ``expansion.copper`` flattened the
    same way the store does — not a hand-rolled stand-in, so these tests
    can't drift from what the store/handler actually produce."""
    expansion = pcb_generators.expand("ewod_pad_array", "ARR1", params)
    fp_name, fp_data = _normalize_local_footprint(expansion.footprints[0])
    inst = {**expansion.components[0], "refdes": "ARR1"}
    pin_to_net = {("ARR1", c["pin"]): c["net"] for c in expansion.connections}
    pads, drills = board_pads(
        [inst],
        {},
        layers=_LAYERS,
        pin_to_net=pin_to_net,
        local_footprints={fp_name: fp_data},
    )
    model = {
        "layers": _LAYERS,
        "copper": _flatten_copper(expansion.copper),
        "pads": pads,
        "drills": drills,
    }
    return expansion, model


def _net_rules_for(
    expansion: pcb_generators.GeneratorExpansion, cap: Any
) -> dict[str, Any]:
    """The SAME per-net-class resolution ``_pcb_apply``'s upserted
    ``pcb_net_classes`` row feeds ``view='drc'`` through, built by hand
    here since these tests never touch the DB."""
    resolved_by_class = {
        cls: resolve_net_rules(
            cls, layer_is_outer=True, fab_caps=cap, overrides=overrides
        )
        for cls, overrides in expansion.net_classes.items()
    }
    return {
        str(n["name"]): resolved_by_class[str(n["net_class"])]
        for n in expansion.nets
        if n.get("net_class") in resolved_by_class
    }


def _electrode_bodies_only(model: dict[str, Any]) -> dict[str, Any]:
    """Electrode BODIES, no copper at all — the net-class override is a
    claim about electrode-to-electrode adjacency specifically (spec:
    "electrode-gap adjacency checked against the authored gap"); the neck
    TRACK is a DIFFERENT, tighter clearance budget of its own (module
    docstring's "geometry residues" section) that this net class was
    never meant to paper over. ``model["pads"]`` is already
    bodies-only as of pcb-pre-place-route-blocks Slice 2 (the stub/via
    pad rows are gone), so this now only has to drop ``copper``."""
    return {**model, "copper": []}


# ── electrode-gap net-class clearance floor ──────────────────────────────


def test_default_gap_electrode_adjacency_needs_the_net_class_override_to_stay_clean():
    """The default ``gap`` (0.10mm) sits BELOW the fab's flat
    ``trace_spacing_mm`` house_default tier (0.15mm at 4-layer) — without
    the dedicated ``ewod_{name}`` net class every ordinary
    electrode-to-electrode adjacency pair on every EWOD board would carry
    a spurious WARN. The override changes nothing about whether the
    finding COULD fire, only what a clean board looks like."""
    expansion, model = _ewod_model(grid=[3, 3])
    electrode_only = _electrode_bodies_only(model)
    findings_without_override = drc.check_clearance(electrode_only, _CAP4)
    assert any(
        f.rule == "clearance" and f.severity == "warn"
        for f in findings_without_override
    )
    net_rules = _net_rules_for(expansion, _CAP4)
    assert drc.check_clearance(electrode_only, _CAP4, net_rules=net_rules) == []


def test_gap_under_the_fab_floor_is_a_clearance_error_naming_the_floor():
    """Acceptance criterion 5: an authored ``gap`` narrower than the fab's
    OWN minimum copper isolation is a DRC ERROR (the net-class override
    only ever changes the WARN threshold — the ERROR floor is
    ``jlc_min``, untouched by any override, module docstring of
    ``precis.pcb.generators``)."""
    jlc_min = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min is not None
    bad_gap = jlc_min / 2.0
    expansion, model = _ewod_model(
        grid=[3, 3], gap=bad_gap, via={"dia": 0.25, "drill": 0.15}
    )
    electrode_only = _electrode_bodies_only(model)
    net_rules = _net_rules_for(expansion, _CAP4)
    findings = drc.check_clearance(electrode_only, _CAP4, net_rules=net_rules)
    errors = [f for f in findings if f.rule == "clearance" and f.severity == "error"]
    assert errors
    assert any(f"{jlc_min:.3f}" in f.detail for f in errors)


def test_gap_comfortably_above_the_fab_floor_stays_clean():
    jlc_min = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min is not None
    expansion, model = _ewod_model(grid=[3, 3], gap=jlc_min + 0.01)
    electrode_only = _electrode_bodies_only(model)
    net_rules = _net_rules_for(expansion, _CAP4)
    assert drc.check_clearance(electrode_only, _CAP4, net_rules=net_rules) == []


@pytest.mark.parametrize("grid", [[3, 3], [8, 8], [9, 9], [3, 8]])
def test_diagonal_escape_electrode_bodies_stay_clear_of_each_other(grid):
    """**Round 4's own fix still holds.** A corner electrode's diagonal
    escape runs exactly along the 45-degree line joining its own flat
    corner to the two FLANKING (cardinal-escaping) neighbours' own flat
    corners, and those corners sit only ``gap/sqrt(2)`` from that line —
    a pure trigonometric fact, independent of stub width, that sits BELOW
    the fab's own absolute copper floor at default sizing.
    ``precis.pcb.generators._electrode_polygon`` chamfers exactly those
    two flanking corners (``plaza_corner_chamfer``) back out to the
    array's own uniform ``gap`` design target — checked here against
    ELECTRODE BODIES ONLY (no copper), which pcb-pre-place-route-blocks
    Slice 2 did not touch and does not regress. The neck TRACK's own
    clearance against those same flanking bodies is
    ``test_diagonal_escape_stub_track_clears_the_fab_floor`` below, not
    this test."""
    _, model = _ewod_model(grid=grid)
    body_only = _electrode_bodies_only(model)
    findings = drc.check_clearance(body_only, _CAP4)
    errors = [f for f in findings if f.rule == "clearance" and f.severity == "error"]
    assert errors == [], [f.detail for f in errors]


def _assert_clearance_errors_are_only_the_known_stub_track_gap(
    errors: list[Any],
) -> None:
    """The whole-suite invariant (module docstring item 1, now CLOSED): a
    ``check_clearance`` error list from an ``ewod_pad_array`` model must
    be empty at any sizing this file sweeps — kept as a named helper
    (rather than a bare ``== []`` at each call site) so a future
    regression's failure message still says what it broke."""
    assert errors == [], [f"{f.where} :: {f.detail}" for f in errors]


@pytest.mark.parametrize("grid", [[3, 3], [8, 8], [9, 9], [3, 8]])
def test_diagonal_escape_stub_track_clears_the_fab_floor(grid):
    """**FLIPPED by docs/backlog/pcb-ewod-multitile.md "Rulings
    2026-09-18" item 1** — this test used to pin a KNOWN sub-floor gap
    here (module docstring item 1's old text); the corridor
    ``plaza_corner_chamfer`` opens is now sized FROM the fab-minimum
    track width plus two fab clearances
    (:func:`precis.pcb.generators.resolve_ewod_sizing`'s own derivation),
    not a fixed +0.01mm margin on top of ``gap`` — a diagonal escape's
    constant-width neck track (:func:`precis.pcb.generators.
    _stub_track_row`) now clears its two flanking neighbours' bodies by
    construction, at DEFAULT sizing, across every grid this file
    sweeps."""
    _, model = _ewod_model(grid=grid)
    findings = drc.check_clearance(model, _CAP4)
    errors = [f for f in findings if f.rule == "clearance" and f.severity == "error"]
    _assert_clearance_errors_are_only_the_known_stub_track_gap(errors)


def test_stub_width_never_drops_below_the_fab_minimum_trace_width():
    """``resolve_ewod_sizing`` floors ``stub_width`` at the fab's minimum
    trace width — the DEFAULT value, with no ``stub_width`` override at
    all (``params.get("stub_width", _trace_width_floor)``'s own
    default)."""
    sizing = pcb_generators.resolve_ewod_sizing({"grid": [3, 3]})
    trace_min = _CAP4.jlc_min["trace_width_mm"]
    assert trace_min is not None
    assert sizing["stub_width"] == pytest.approx(trace_min)
    assert sizing["stub_width"] >= trace_min


def test_stub_width_override_below_the_fab_minimum_is_floored():
    """docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-18" item 1's
    own new contract: an explicit ``stub_width`` override narrower than
    the fab's minimum trace width is raised back up to that floor, not
    honoured as unetchable copper — the corridor is no longer sized to
    accommodate a deliberately-narrowed track as an escape hatch (that
    escape hatch existed only because the OLD fixed-margin corridor
    couldn't host a fab-minimum track at all; the rule-derived corridor
    always can, so there is nothing left to escape)."""
    jlc_min_spacing = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min_spacing is not None
    trace_min = _CAP4.jlc_min["trace_width_mm"]
    assert trace_min is not None
    sizing = pcb_generators.resolve_ewod_sizing(
        {"grid": [3, 3], "stub_width": jlc_min_spacing / 4.0}
    )
    assert sizing["stub_width"] == pytest.approx(trace_min)
    _, model = _ewod_model(grid=[3, 3], stub_width=jlc_min_spacing / 4.0)
    errors = [
        f
        for f in drc.check_clearance(model, _CAP4)
        if f.rule == "clearance" and f.severity == "error"
    ]
    _assert_clearance_errors_are_only_the_known_stub_track_gap(errors)


def test_stub_width_override_wider_than_gap_is_still_capped_to_gap():
    """The pre-existing, UNCHANGED clamp (module docstring's own
    ``stub_width = min(stub_width_uncapped, gap)``, never a corridor
    matter): an author-requested ``stub_width`` wider than ``gap`` is
    silently narrowed to ``gap`` — a track any wider clips a diagonally-
    adjacent electrode's own corner (``gap*sqrt(2)`` away there,
    unrelated to the plaza-corner chamfer)."""
    gap = pcb_generators._DEFAULT_GAP_MM
    sizing = pcb_generators.resolve_ewod_sizing({"grid": [3, 3], "stub_width": 5.0})
    assert sizing["stub_width"] == pytest.approx(gap)
    _, model = _ewod_model(grid=[3, 3], stub_width=5.0)
    errors = [
        f
        for f in drc.check_clearance(model, _CAP4)
        if f.rule == "clearance" and f.severity == "error"
    ]
    _assert_clearance_errors_are_only_the_known_stub_track_gap(errors)


def _brute_force_old_ring_min_foreign_clearance(
    via_dia: float, hv_separation: float, half: float, gap: float
) -> float:
    """The superseded uniform-ring family's own minimum foreign-copper
    clearance, computed straight from its documented formula (this
    module's old ``_plaza_capacity`` docstring, before docs/backlog/
    pcb-ewod-multitile.md "Rulings 2026-09-19" item 8): ``R = (via_dia +
    hv_separation + slack) / (2*sin(22.5deg))``, EVERY slot (cardinal or
    diagonal) on that one ring, cardinals at radius ``R`` directly,
    diagonals at ``(R/sqrt(2), R/sqrt(2))`` in magnitude — reusing this
    file's own :func:`precis.pcb.generators._family_foreign_clearance`
    (a pure geometry function, unaffected by which slot family calls it)
    to measure it, so this comparison is exact, not approximated."""
    slack = pcb_generators._GEOMETRY_ROUNDING_SLACK_MM
    chord_factor = 2.0 * math.sin(math.pi / 8.0)
    ring_radius = (via_dia + hv_separation + slack) / chord_factor
    u0 = ring_radius / math.sqrt(2.0)
    return pcb_generators._family_foreign_clearance(ring_radius, u0, half, gap)


def test_plaza_family_beats_the_superseded_ring_at_default_via_hv_numbers():
    """docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19" item 8:
    the axis-aligned two-parameter family's minimum foreign-copper
    clearance strictly beats the old uniform ring's, at the SAME
    ``half`` (the actual sizing figure a real board ends up with,
    default ``pitch``/``gap`` here) and the SAME default via/hv
    numbers — the whole point of decoupling cardinal (``slot_a``) from
    diagonal (``slot_b``) instead of tying both to one ring radius."""
    cap = pcb_generators.capability_for(pcb_generators._FAB_PROCESS)
    via_dia = cap.jlc_min["via_diameter_mm"]
    hv_separation = cap.jlc_min["trace_spacing_mm"]
    assert via_dia is not None and hv_separation is not None
    sizing = pcb_generators.resolve_ewod_sizing({"grid": [3, 3]})
    gap = sizing["gap"]
    half = sizing["half"]
    old = _brute_force_old_ring_min_foreign_clearance(via_dia, hv_separation, half, gap)
    new = pcb_generators._family_foreign_clearance(
        sizing["slot_a"], sizing["slot_b"], half, gap
    )
    assert new > old, (old, new)


def test_plaza_family_all_28_slot_pairs_clear_the_via_via_floor():
    """docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19" item 8's
    own pairwise constraint, checked EXHAUSTIVELY (all C(8,2)=28 pairs
    among the family's 8 slots, not just the 3 chord shapes
    :func:`precis.pcb.generators._family_min_pair_chord` reduces to) at
    the ``(slot_a, slot_b)`` :func:`precis.pcb.generators._plaza_capacity`
    actually picked for the default via/hv numbers."""
    cap = pcb_generators.capability_for(pcb_generators._FAB_PROCESS)
    via_dia = cap.jlc_min["via_diameter_mm"]
    hv_separation = cap.jlc_min["trace_spacing_mm"]
    assert via_dia is not None and hv_separation is not None
    capacity = pcb_generators._plaza_capacity(
        pcb_generators._DEFAULT_GAP_MM, via_dia, hv_separation
    )
    a, b = capacity["slot_a"], capacity["slot_b"]
    slots = {
        "N": (0.0, -a),
        "S": (0.0, a),
        "E": (a, 0.0),
        "W": (-a, 0.0),
        "NE": (b, -b),
        "NW": (-b, -b),
        "SE": (b, b),
        "SW": (-b, b),
    }
    floor = pcb_generators._pair_floor(via_dia, hv_separation)
    names = list(slots)
    pairs = [(n1, n2) for i, n1 in enumerate(names) for n2 in names[i + 1 :]]
    assert len(pairs) == 28
    for n1, n2 in pairs:
        (x1, y1), (x2, y2) = slots[n1], slots[n2]
        dist = math.hypot(x1 - x2, y1 - y2)
        assert dist >= floor - 1e-9, (n1, n2, dist, floor)


def test_plaza_family_via_to_via_clearance_survives_coordinate_rounding():
    """Companion round-4 finding, now against the axis-aligned family
    (docs/backlog/pcb-ewod-multitile.md "Rulings 2026-09-19" item 8):
    every one of the family's 28 slot pairs sits AT LEAST
    :func:`precis.pcb.generators._pair_floor` apart, and the closest
    pair(s) sit EXACTLY there with zero margin whenever ``hv_separation``
    itself falls back to the fab's own ``jlc_min`` floor (no
    ``drive_voltage_v`` declared) — the SAME floor
    ``check_clearance``'s ERROR tier checks against. Placed-pad
    coordinate rounding (4 decimal places, :func:`precis.pcb.padplace.
    place_footprint_pads`) can then shave a few 0.00001mm off the exact
    analytic chord, enough at zero margin to flip a genuinely-
    manufacturable family into a spurious ERROR. Isolated to VIA-VS-VIA
    pairs specifically."""
    _, model = _ewod_model(grid=[3, 3])
    findings = drc.check_clearance(model, _CAP4)
    via_via_errors = [
        f
        for f in findings
        if f.rule == "clearance"
        and f.severity == "error"
        and {f.objects[0]["ctype"], f.objects[1]["ctype"]} == {"via"}
    ]
    assert via_via_errors == [], [f.detail for f in via_via_errors]


@pytest.mark.parametrize(
    ("grid", "variant"),
    [
        ([1, 1], "full"),
        ([2, 2], "full"),
        ([4, 4], "full"),
        ([16, 16], "full"),
        ([32, 32], "full"),
        ([4, 4], "rim"),
        ([16, 16], "rim"),
        ([32, 32], "rim"),
    ],
)
def test_min_clearance_stays_within_the_known_gap_across_grid_sizes(grid, variant):
    """The exact-geometry stress sweep the round-4 fix (both the
    plaza-corner chamfer and the rim via-reach rewrite) was validated
    against, at every grid size from degenerate (a single pad, no plaza
    at all) up to the spec's own 1024-pad ceiling, for either variant.
    **Bodies alone stay fully clean** (round 4's own guarantee, untouched
    by Slice 2); since docs/backlog/pcb-ewod-multitile.md "Rulings
    2026-09-18" item 1, copper-inclusive findings must be empty too — a
    genuine geometry regression still fails this test."""
    _, model = _ewod_model(grid=grid, variant=variant)
    body_errors = [
        f
        for f in drc.check_clearance(_electrode_bodies_only(model), _CAP4)
        if f.rule == "clearance" and f.severity == "error"
    ]
    assert body_errors == [], [f.detail for f in body_errors]

    findings = drc.check_clearance(model, _CAP4)
    errors = [f for f in findings if f.rule == "clearance" and f.severity == "error"]
    _assert_clearance_errors_are_only_the_known_stub_track_gap(errors)


# ── plaza via annular ring ────────────────────────────────────────────────


def test_plaza_via_annular_ring_is_checked_and_manufacturable_at_default_sizing():
    """Round 3 taught ``check_annular_ring`` to also ring-check drilled
    FOOTPRINT pads (not ``model["copper"]`` vias only) because the plaza
    via was, at the time, a drilled THT pad by design — never a
    ``copper`` via row. pcb-pre-place-route-blocks Slice 2 reverses that
    design call (the via is real ``copper`` now), which makes this the
    ORDINARY via-vs-``model["copper"]`` path ``check_annular_ring``
    already had, not the footprint-pad extension round 3 added — either
    way, the check has always run against this generator's own via since
    round 3, and keeps running now for the same reason via a different
    mechanism. This asserts the check actually RUNS (not merely "returns
    [] because it can't see anything") two ways: it fires ERROR on a
    deliberately undersized via, and it does NOT fire ERROR on the
    generator's own default sizing
    (a real bug the same fix surfaced — ``via_drill``'s old bare 0.2mm
    default, decoupled from the jlc_min-derived ``via_dia`` default, rang
    below even the fab's absolute floor; fixed by deriving both from the
    SAME capability figure, landing the ring exactly AT the floor).

    A WARN is still expected at default sizing — round 2's own documented
    choice was the jlc_min TIER for via sizing generally (a plaza via's
    ring gets no reliability-driven margining, module docstring's own
    ``via_dia``/``stub_width`` reasoning), so a floor-sized default
    legitimately spends all its headroom against ``house_default``. An
    author who wants a quiet board passes an explicit, larger ``via``."""
    _, undersized = _ewod_model(grid=[3, 3], via={"dia": 0.30, "drill": 0.28})
    bad_findings = drc.check_annular_ring(undersized, _CAP4)
    assert any(f.severity == "error" for f in bad_findings)

    _, clean = _ewod_model(grid=[3, 3])
    default_findings = drc.check_annular_ring(clean, _CAP4)
    assert all(f.severity == "warn" for f in default_findings)

    _, margined = _ewod_model(grid=[3, 3], via={"dia": 0.7, "drill": 0.15}, pitch=2.2)
    assert drc.check_annular_ring(margined, _CAP4) == []


# ── rim variant's corner via reach (round-4 sibling-gap verification) ───


def test_rim_corner_via_lands_outside_its_own_electrode_body():
    """Round-3 flagged this as an UNVERIFIED suspicion ("likely the SAME
    root cause"); round 4 verified it was real, but a DIFFERENT bug than
    suspected: the old ``_rim_via_point`` diagonal branch measured
    ``slot_radius`` from the corner pad's OWN centre rather than from its
    virtual plaza's centre (a full ``pitch`` away), landing the via
    partway to the corner — still WELL INSIDE the pad's own polygon. A
    via buried under its own same-net pad is not a DRC violation (nothing
    to short), but it IS exactly the via-in-pad situation this whole
    generator exists to avoid, silently, on every rim corner.
    pcb-pre-place-route-blocks Slice 2 moved the via off ``pads`` and
    onto ``copper`` — read it from there now, keyed by net (``pin`` no
    longer names a copper row directly)."""
    exp = pcb_generators.expand(
        "ewod_pad_array", "ARR", {"grid": [4, 4], "variant": "rim"}
    )
    bodies_by_pin = {p["pin"]: p for p in exp.footprints[0]["pads"]}
    vias_by_pin = {
        _pin_for_net("ARR", str(item["net"])): item
        for item in exp.copper
        if item["ctype"] == "via"
    }
    checked = 0
    for pin, body in bodies_by_pin.items():
        via = vias_by_pin.get(pin)
        if via is None:
            continue
        vx, vy = via["geom"]["x"], via["geom"]["y"]
        assert not Polygon(body["poly"]).buffer(0).contains(SPoint(vx, vy)), (
            f"{pin}: via landed inside its own electrode body"
        )
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("grid", [[4, 4], [8, 8], [16, 16]])
def test_rim_corner_via_clears_its_flanking_cardinal_neighbours_vias(grid):
    """A rim's 4 corners are a genuine multi-consumer plaza in every way
    that matters (the corner pad's diagonal virtual plaza and its TWO
    cardinal neighbours' own virtual plazas are literally the SAME
    hollow cell) — round-4 finding, once the via-in-own-pad bug above was
    fixed, this via-to-via pair became the next (and real) manifestation
    of round 3's suspected sibling gap: two independently-reached vias
    with nothing guaranteeing their mutual clearance. ``_rim_via_point``
    routes every rim pad's via through the SAME
    :func:`precis.pcb.generators._plaza_slot_point` ring construction a
    real plaza's own consumers use, which is what proves mutual
    clearance in the first place. Isolated to VIA-VS-VIA pairs, same as
    the full-grid plaza-ring test above."""
    _, model = _ewod_model(grid=grid, variant="rim")
    findings = drc.check_clearance(model, _CAP4)
    via_via_errors = [
        f
        for f in findings
        if f.rule == "clearance"
        and f.severity == "error"
        and {f.objects[0]["ctype"], f.objects[1]["ctype"]} == {"via"}
    ]
    assert via_via_errors == [], [f.detail for f in via_via_errors]


# ── via-pad keep-out still protects the field ────────────────────────────


def test_router_via_landing_on_an_electrode_pad_still_errors():
    """DESIGN CALL (round 3): the plaza via stays a drilled footprint
    pad, never a persistent ``model["copper"]`` via row — but that never
    put an electrode pad at risk from a REAL router-placed via elsewhere
    on a shared board: ``check_via_pad_keepout`` reads ``model["pads"]``
    generically (any shape, any role), so a polygon electrode is
    protected on the exact same terms as a rect/obround one, no
    generator-specific waiver needed (acceptance criterion 4)."""
    _, model = _ewod_model(grid=[3, 3])
    electrode = next(
        p
        for p in model["pads"]
        if p.get("role") == "electrode" and p["shape"] == "polygon"
    )
    via = {
        "ctype": "via",
        "net": "OTHER_NET",
        "x": electrode["x"],
        "y": electrode["y"],
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": [electrode["layer"]],
    }
    model_with_via = {**model, "copper": [via]}
    findings = drc.check_via_pad_keepout(model_with_via, _CAP4)
    assert findings
    assert all(f.rule == "via_pad_keepout" and f.severity == "error" for f in findings)


def test_own_plaza_via_never_false_positives_via_pad_keepout_against_own_electrode():
    """docs/backlog/pcb-pre-place-route-blocks.md geometry residues:
    ``check_via_pad_keepout``'s circumscribed-circle approximation of an
    electrode's real (crenellated/chamfered) outline used to
    false-positive the generator's OWN plaza via against its OWN
    electrode body -- same net, correct-by-construction geometry
    (``_plaza_capacity``'s own derived spacing), no foreign via involved.
    Fixed with a same-net exemption; the OTHER-net case immediately above
    (:func:`test_router_via_landing_on_an_electrode_pad_still_errors`)
    stays enforced."""
    _, model = _ewod_model(grid=[3, 3])
    findings = drc.check_via_pad_keepout(model, _CAP4)
    same_net_false_positives = [
        f for f in findings if f.objects[0]["via_net"] == f.objects[0]["pad_net"]
    ]
    assert same_net_false_positives == [], [f.detail for f in same_net_false_positives]


def test_router_via_clear_of_the_field_stays_quiet():
    _, model = _ewod_model(grid=[3, 3])
    far_via = {
        "ctype": "via",
        "net": "OTHER_NET",
        "x": 1000.0,
        "y": 1000.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],
    }
    model_with_via = {**model, "copper": [far_via]}
    assert drc.check_via_pad_keepout(model_with_via, _CAP4) == []


# ── pad_sizes merged electrodes (round 6) ────────────────────────────────


@pytest.mark.parametrize(
    ("variant", "grid", "cells"),
    [
        # Adjacent to a real via plaza (3x3's only plaza sits at P1_1;
        # (0,0)/(0,1) both touch it diagonally/cardinally).
        ("full", [3, 3], [[0, 0], [0, 1]]),
        # Adjacent to the array's own EXTERNAL mesh boundary (bottom row).
        ("full", [4, 4], [[3, 0], [3, 1]]),
        # A merge away from any plaza/boundary interaction, mid-field.
        ("full", [9, 9], [[5, 5], [5, 6], [6, 5], [6, 6]]),
        # rim variant: a straight-edge pair on the top rim (adjacent to
        # the rim's own hollow hollow interior AND the external edge).
        ("rim", [4, 4], [[0, 1], [0, 2]]),
        # rim variant: a corner pad merged with its cardinal neighbour.
        ("rim", [4, 4], [[0, 0], [0, 1]]),
    ],
)
def test_merged_pad_min_clearance_stays_at_or_above_the_fab_floor(variant, grid, cells):
    """The same exact-geometry sweep as
    ``test_min_clearance_stays_within_the_known_gap_across_grid_sizes``,
    now with a ``pad_sizes`` merge covering the span -- adjacent to a real
    plaza, adjacent to the external mesh boundary, mid-field, and on both
    a rim's straight edge and its corner, on top of the ordinary
    unmerged-neighbour geometry every one of these still has along its
    OTHER walls. Bodies alone stay fully clean; copper-inclusive findings
    must be empty too (docs/backlog/pcb-ewod-multitile.md "Rulings
    2026-09-18" item 1)."""
    _, model = _ewod_model(grid=grid, variant=variant, pad_sizes=[{"cells": cells}])
    body_errors = [
        f
        for f in drc.check_clearance(_electrode_bodies_only(model), _CAP4)
        if f.rule == "clearance" and f.severity == "error"
    ]
    assert body_errors == [], [f.detail for f in body_errors]

    findings = drc.check_clearance(model, _CAP4)
    errors = [f for f in findings if f.rule == "clearance" and f.severity == "error"]
    _assert_clearance_errors_are_only_the_known_stub_track_gap(errors)


def test_merged_pad_plaza_via_annular_ring_stays_clean_at_default_sizing():
    _, model = _ewod_model(grid=[3, 3], pad_sizes=[{"cells": [[0, 0], [0, 1]]}])
    findings = drc.check_annular_ring(model, _CAP4)
    assert all(f.severity != "error" for f in findings), [f.detail for f in findings]


def test_merged_pad_via_still_protects_electrode_body_from_a_router_via():
    """Acceptance criterion 4, generalised to a merged pad: the merged
    electrode's own copper is still an ordinary ``model["pads"]`` entry,
    so a foreign router-placed via landing on it still errors -- no
    merged-pad-specific waiver."""
    _, model = _ewod_model(grid=[3, 3], pad_sizes=[{"cells": [[0, 0], [0, 1]]}])
    merged_body = next(
        p
        for p in model["pads"]
        if p.get("role") == "electrode"
        and p["shape"] == "polygon"
        and p.get("net") == "ARR1_R0C0"
    )
    via = {
        "ctype": "via",
        "net": "OTHER_NET",
        "x": merged_body["x"],
        "y": merged_body["y"],
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": [merged_body["layer"]],
    }
    model_with_via = {**model, "copper": [via]}
    findings = drc.check_via_pad_keepout(model_with_via, _CAP4)
    assert findings
    assert all(f.rule == "via_pad_keepout" and f.severity == "error" for f in findings)
