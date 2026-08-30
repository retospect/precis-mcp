"""PcbHandler end-to-end against a live store.

Exercises the batch authoring path (put with components/nets/connections),
the netlist TOC, the graph-traversal reads (instance neighbourhood, net
members), re-runnability, and soft-delete. Uses the shared ``store`` fixture.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog

# A tiny but real board: an MCU + a bypass cap + a pull-up, on an I2C net.
_DESIGN = {
    "components": [
        {
            "refdes": "U1",
            "label": "ESP32-C3",
            "part": "C2838500",
            "footprint": "QFN-32",
            "roles": ["noisy"],
            "x": 10.0,
            "y": 10.0,
            "pins": [
                {"name": "VDD", "pad": "1", "tags": ["power", "3v3"]},
                {"name": "GND", "pad": "2", "tags": ["gnd"]},
                {"name": "SCL", "pad": "8", "tags": ["bidir", "i2c"]},
            ],
        },
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part": "C1525",
            "footprint": "0402",
            "x": 11.5,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
            "note": "VDD bypass for U1",
        },
        {
            "refdes": "R1",
            "label": "4.7k 0402",
            "part": "C25900",
            "footprint": "0402",
            "x": 13.0,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [
        {"name": "VCC3V3", "class": "power", "current": 0.5},
        {"name": "GND", "class": "gnd"},
        {"name": "I2C_SCL", "class": "i2c"},
    ],
    "connections": [
        {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
        {"net": "VCC3V3", "refdes": "C1", "pin": "1", "note": "bypass hi side"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "GND", "refdes": "C1", "pin": "2"},
        {"net": "I2C_SCL", "refdes": "U1", "pin": "SCL"},
        {"net": "I2C_SCL", "refdes": "R1", "pin": "1"},
    ],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def test_put_creates_and_lists(pcb):
    resp = pcb.put(id="sensor-node", args=_DESIGN)
    assert "created" in resp.body
    assert "+3 part(s)" in resp.body and "+3 net(s)" in resp.body
    # the TOC shows parts + nets
    assert "U1" in resp.body and "ESP32-C3" in resp.body
    assert "I2C_SCL" in resp.body
    # listing shows it
    lst = pcb.get()
    assert "sensor-node" in lst.body


def test_pcb_graph_carries_part_lcsc_per_instance(pcb, store):
    """The join :func:`precis.pcb.session.footprints_by_refdes` needs:
    ``Store.pcb_graph``'s instance rows must carry the SAME ``part_lcsc``
    the store already joins (``pcb_components.part_lcsc``) for
    ``pcb_load`` — before this it was selected there and nowhere else, so
    nothing built off ``pcb_graph`` (every ``PcbIR``) had a C-number to
    remap a cached footprint onto."""
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i["part_lcsc"] for i in graph["instances"]}
    assert by_refdes == {"U1": "C2838500", "C1": "C1525", "R1": "C25900"}


def test_pcb_graph_part_lcsc_is_none_for_a_part_less_component(pcb, store):
    pcb.put(
        id="part-less",
        args={
            "components": [
                {"refdes": "MH1", "label": "mounting hole", "pins": [{"name": "1"}]}
            ]
        },
    )
    ref = store.get_ref(kind="pcb", id="part-less")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    assert graph["instances"][0]["part_lcsc"] is None


def test_pcb_graph_carries_extended_part_per_instance(pcb, store):
    """``Store.pcb_graph`` must join the Basic-vs-Extended signal
    (``parts.basic``, populated by ``pcb.catalog.normalize_jlcparts_row``)
    into the instance rows it hands to ``PcbIR.inst_extended_part`` — before
    this it was never selected, so ``cost.py``'s ``extended_part_fees``
    always priced every board's JLC Extended-part surcharge at $0, real or
    not (the third instance of the same gap ``rot``/``part_lcsc`` document
    in ``pcb_graph``'s own comments)."""
    store.parts_import(
        [
            catalog.normalize_jlcparts_row(
                {"lcsc": "C2838500", "description": "MCU", "basic": 0}
            ),  # Extended
            catalog.normalize_jlcparts_row(
                {"lcsc": "C1525", "description": "cap", "basic": 1}
            ),  # Basic
            # C25900 (R1) deliberately absent from the catalog: a part
            # with no resolvable Basic/Extended signal must read as "not
            # (known) Extended", never guessed either way.
        ]
    )
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i["extended_part"] for i in graph["instances"]}
    assert by_refdes == {"U1": True, "C1": False, "R1": False}


def test_toc_shows_placement_and_fanout(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    toc = pcb.get(id="sensor-node")
    assert "@10,10" in toc.body  # U1 placement (centroid)
    assert "noisy" in toc.body  # role tag rendered
    # GND + VCC each have fanout 2; the nets section lists them
    assert "GND" in toc.body and "VCC3V3" in toc.body


def test_instance_neighbourhood_hop(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    u1 = pcb.get(id="sensor-node#U1")
    # U1's VDD pin is on VCC3V3 and its neighbour there is C1
    assert "VDD" in u1.body and "VCC3V3" in u1.body
    assert "C1" in u1.body  # neighbour on the power net
    # SCL pin is on I2C_SCL with R1 as a neighbour
    assert "SCL" in u1.body and "R1" in u1.body


def test_net_members(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    net = pcb.get(id="sensor-node@VCC3V3")
    assert "U1" in net.body and "C1" in net.body
    assert "class power" in net.body


def test_put_is_rerunnable_and_extends(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    # re-applying the same design adds nothing (refdes/net names reused)
    again = pcb.put(id="sensor-node", args=_DESIGN)
    assert "+0 part(s)" in again.body and "+0 net(s)" in again.body
    # extending with a new part works
    ext = pcb.put(
        id="sensor-node",
        args={
            "components": [
                {"refdes": "R2", "label": "10k 0402", "pins": [{"name": "1"}]}
            ],
            "connections": [{"net": "I2C_SCL", "refdes": "R2", "pin": "1"}],
        },
    )
    assert "+1 part(s)" in ext.body
    assert "now 4 part(s)" in ext.body


def test_one_net_per_physical_pin(pcb):
    """The UNIQUE(instance,pin) invariant: re-connecting a pin moves it."""
    pcb.put(id="sensor-node", args=_DESIGN)
    # move U1.SCL from I2C_SCL to GND (a re-wire)
    pcb.put(
        id="sensor-node",
        args={"connections": [{"net": "GND", "refdes": "U1", "pin": "SCL"}]},
    )
    scl = pcb.get(id="sensor-node@I2C_SCL")
    assert "U1" not in scl.body  # U1.SCL left I2C_SCL
    gnd = pcb.get(id="sensor-node@GND")
    assert "U1" in gnd.body


def test_put_requires_id(pcb):
    with pytest.raises(BadInput):
        pcb.put(args=_DESIGN)


def test_get_unknown_design_raises(pcb):
    with pytest.raises(NotFound):
        pcb.get(id="does-not-exist")


def test_delete_soft_retires(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    resp = pcb.delete(id="sensor-node")
    assert "retired" in resp.body
    with pytest.raises(NotFound):
        pcb.get(id="sensor-node")


# ── the eyes ───────────────────────────────────────────
# A board with a guaranteed crossing: two signal nets whose airwires form an X.
_CROSSED = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "C", "label": "ic", "x": 0.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "D", "label": "ic", "x": 2.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [
        {"name": "N1", "class": "signal"},
        {"name": "N2", "class": "signal"},
    ],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
}


def test_crossings_view(pcb):
    pcb.put(id="x", args=_CROSSED)
    resp = pcb.get(id="x", view="crossings")
    assert "crossings — 1" in resp.body
    assert "N1" in resp.body and "N2" in resp.body


def test_ratsnest_view_excludes_plane_nets(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    rn = pcb.get(id="sensor-node", view="ratsnest")
    # I2C_SCL is a signal net → an airwire; GND/VCC3V3 are plane → excluded
    assert "I2C_SCL" in rn.body
    assert "GND" not in rn.body and "VCC3V3" not in rn.body


def test_drc_view_before_any_route_run(pcb):
    # Geometric DRC (pcb-guided-place-route Slice 8) checks REALIZED copper
    # (pcb_copper) — before op='route' has ever run there is none, and the
    # view says so rather than reporting a false "clean" or crashing. See
    # tests/test_pcb_drc.py for the engine's own rule/oracle coverage.
    pcb.put(id="sensor-node", args=_DESIGN)
    drc = pcb.get(id="sensor-node", view="drc")
    assert "no realized copper yet" in drc.body


def test_drc_view_via_caveat_shown_even_on_a_clean_board(pcb):
    # No production caller emits `ctype='via'` copper yet (Finding 2) —
    # the caveat must be visible on the "clean" path too, not just
    # alongside real findings. Components sit far apart (unlike _DESIGN's
    # tight 0402 spacing) so the generic courtyard fallback radius doesn't
    # itself manufacture a courtyard-overlap finding here.
    clean_design = {
        "components": [
            {
                "refdes": "U1",
                "label": "mcu",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "R1",
                "label": "r",
                "x": 20.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
        ],
        "nets": [{"name": "N1"}],
        "connections": [
            {"net": "N1", "refdes": "U1", "pin": "1"},
            {"net": "N1", "refdes": "R1", "pin": "1"},
        ],
    }
    pcb.put(id="drc-clean", args=clean_design)
    ref = pcb.store.get_ref(kind="pcb", id="drc-clean")
    board_id = pcb.store.pcb_ensure_board(ref.id)
    net_ids = pcb.store.pcb_net_ids(ref.id)
    pcb.store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["N1"],
                "route_id": None,
                "geom": {
                    # Pad to pad. This used to stop at (1,0) — a 1mm stub
                    # hanging off U1 that reached R1's pad not at all — and
                    # the board still read "no findings", because every
                    # rule then in the module asks how CLOSE copper is and
                    # none asked whether a net's copper is one piece. A
                    # board this test calls clean has to actually be clean.
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [20.0, 0.0]}
                    ],
                    "width_mm": 0.5,
                },
            },
        ],
    )
    # ...and a routed board says it is routed. Copper in the table with no
    # `pcb_routes` row is a half-finished design, which check_unrouted is
    # right to flag; seeding both is what makes this a clean-board case.
    pcb.store.pcb_routes_write(ref.id, board_id, {"N1": {"status": "realized"}})
    drc = pcb.get(id="drc-clean", view="drc")
    assert "no findings" in drc.body, drc.body


def test_drc_view_reports_a_clearance_violation_on_realized_copper(pcb):
    # Seed pcb_copper directly (the pcb_route job's own write path,
    # precis.pcb.session/workers.job_types.pcb_route) rather than running
    # the full optimizer — this test is only exercising the store->drc.py
    # ->handler wiring, not the router itself (see test_pcb_drc.py for the
    # engine's own rule/oracle coverage).
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = pcb.store.get_ref(kind="pcb", id="sensor-node")
    board_id = pcb.store.pcb_ensure_board(ref.id)
    net_ids = pcb.store.pcb_net_ids(ref.id)
    pcb.store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["I2C_SCL"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [1.0, 0.0]}
                    ],
                    "width_mm": 0.2,
                },
            },
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["GND"],
                "route_id": None,
                # 0.02mm edge-to-edge gap -- well under any process's
                # jlc_min trace_spacing_mm.
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.22], "end": [1.0, 0.22]}
                    ],
                    "width_mm": 0.2,
                },
            },
        ],
    )
    drc = pcb.get(id="sensor-node", view="drc")
    assert "error" in drc.body
    assert "clearance" in drc.body


def test_drc_view_warns_on_a_pcb_net_classes_elevated_clearance_requirement(pcb):
    """Gap B (pcb-usb-c-pd-nano-testboard.md): a `pcb_net_classes.rules`
    override must actually change ``view='drc'``'s output, not just
    round-trip — a gap that comfortably clears the GENERIC house default
    but falls short of an authored per-class requirement (e.g. a 20V PD
    rail wanting more room) must WARN."""
    design = {
        "components": [
            {
                "refdes": "U1",
                "label": "buck",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U2",
                "label": "load",
                "x": 20.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U3",
                "label": "gnd",
                "x": 0.0,
                "y": 5.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U4",
                "label": "gnd2",
                "x": 20.0,
                "y": 5.0,
                "pins": [{"name": "1"}],
            },
        ],
        "nets": [
            {"name": "VBUS_20V", "class": "power"},
            {"name": "SIG", "class": "signal"},
        ],
        "connections": [
            {"net": "VBUS_20V", "refdes": "U1", "pin": "1"},
            {"net": "VBUS_20V", "refdes": "U2", "pin": "1"},
            {"net": "SIG", "refdes": "U3", "pin": "1"},
            {"net": "SIG", "refdes": "U4", "pin": "1"},
        ],
        "net_classes": {"power": {"clearance_mm": 0.5}},
    }
    pcb.put(id="pd-board", args=design)
    ref = pcb.store.get_ref(kind="pcb", id="pd-board")
    board_id = pcb.store.pcb_ensure_board(ref.id)
    net_ids = pcb.store.pcb_net_ids(ref.id)
    # 0.3mm edge-to-edge gap: clears the generic 4-layer house default
    # (0.15mm) but falls short of the authored power-class 0.5mm rule.
    pcb.store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["VBUS_20V"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [1.0, 0.0]}
                    ],
                    "width_mm": 0.2,
                },
            },
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["SIG"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.5], "end": [1.0, 0.5]}
                    ],
                    "width_mm": 0.2,
                },
            },
        ],
    )
    drc = pcb.get(id="pd-board", view="drc")
    assert "warn" in drc.body
    assert "clearance" in drc.body


def test_drc_view_reports_npth_clearance_near_a_mounting_hole(pcb):
    """Defect: ``_render_drc`` built its ``check_npth_clearance`` model
    with no ``drills`` key at all, ever -- so the rule (wired into
    ``run_geometric_drc`` and reading ``model.get('drills')``) was
    structurally incapable of firing regardless of design content, on
    every board, every seed. A mounting hole feature sitting right under
    a realized GND track must now trip it."""
    design = {
        "components": [
            {
                "refdes": "U1",
                "label": "ic",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U2",
                "label": "ic",
                "x": 10.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
        ],
        "nets": [{"name": "GND"}],
        "connections": [
            {"net": "GND", "refdes": "U1", "pin": "1"},
            {"net": "GND", "refdes": "U2", "pin": "1"},
        ],
        "features": [
            {"ftype": "mounting_hole", "x": 5.0, "y": 0.0, "geom": {"diameter": 3.2}},
        ],
    }
    pcb.put(id="npth-board", args=design)
    ref = pcb.store.get_ref(kind="pcb", id="npth-board")
    board_id = pcb.store.pcb_ensure_board(ref.id)
    net_ids = pcb.store.pcb_net_ids(ref.id)
    # A track runs straight over (5, 0) -- exactly where the mounting hole
    # sits -- so the copper-to-NPTH gap is unambiguously negative.
    pcb.store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["GND"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [10.0, 0.0]}
                    ],
                    "width_mm": 0.2,
                },
            },
        ],
    )
    pcb.store.pcb_routes_write(ref.id, board_id, {"GND": {"status": "realized"}})
    drc = pcb.get(id="npth-board", view="drc")
    assert "npth_clearance" in drc.body, drc.body


def _many_pins(n: int) -> list[dict[str, Any]]:
    return [{"name": str(i)} for i in range(1, n + 1)]


def test_drc_view_reports_courtyard_overlap_with_real_derived_radii(pcb):
    """Defect: the DRC courtyard check read a flat 1.0mm radius
    (``DEFAULT_COURTYARD_RADIUS_MM``) for EVERY instance regardless of
    its actual size -- smaller than any real multi-pin part's derived
    keep-out, so placement (which uses the real, pad-geometry-derived
    radius) always separated parts further apart than the flat DRC check
    would ever flag. The rule was dormant by construction, on both
    reference fixtures, on every seed.

    Two 12-pin ("dual" package family) parts placed 3.0mm apart: each
    part's real derived courtyard radius is ~2.51mm (pad radius ~1.91mm
    + the same 0.6mm pad-breathing margin placement legality adds), so
    their real courtyards overlap by ~2mm -- but their FLAT 1.0mm nominal
    courtyards do not even touch (sum 2.0mm < 3.0mm separation), which is
    exactly how this defect stayed invisible."""
    design = {
        "components": [
            {
                "refdes": "U1",
                "label": "big1",
                "x": 0.0,
                "y": 0.0,
                "pins": _many_pins(12),
            },
            {
                "refdes": "U2",
                "label": "big2",
                "x": 3.0,
                "y": 0.0,
                "pins": _many_pins(12),
            },
        ],
        "nets": [{"name": "N1"}],
        "connections": [
            {"net": "N1", "refdes": "U1", "pin": "1"},
            {"net": "N1", "refdes": "U2", "pin": "1"},
        ],
    }
    pcb.put(id="big-parts", args=design)
    ref = pcb.store.get_ref(kind="pcb", id="big-parts")
    board_id = pcb.store.pcb_ensure_board(ref.id)
    net_ids = pcb.store.pcb_net_ids(ref.id)
    pcb.store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["N1"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [3.0, 0.0]}
                    ],
                    "width_mm": 0.2,
                },
            },
        ],
    )
    pcb.store.pcb_routes_write(ref.id, board_id, {"N1": {"status": "realized"}})
    drc = pcb.get(id="big-parts", view="drc")
    assert "courtyard_overlap" in drc.body, drc.body


def test_proximity_view(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    # U1@(10,10), C1 unplaced in _DESIGN → proximity needs both placed
    pcb.put(
        id="sensor-node",
        args={
            "components": [
                {
                    "refdes": "C9",
                    "label": "100nF",
                    "x": 13.0,
                    "y": 14.0,
                    "pins": [{"name": "1"}],
                }
            ]
        },
    )
    pr = pcb.get(id="sensor-node", view="proximity", args={"a": "U1", "b": "C9"})
    assert "5 mm" in pr.body  # 3-4-5 triangle from (10,10)→(13,14)


def test_measures_view(pcb):
    pcb.put(
        id="m",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "opamp",
                    "x": 0.0,
                    "y": 0.0,
                    "roles": ["sensitive"],
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "Q1",
                    "label": "FET",
                    "x": 4.0,
                    "y": 0.0,
                    "roles": ["noisy"],
                    "pins": [{"name": "1"}],
                },
            ],
            "measures": [
                {
                    "metric": "separation",
                    "goal": 10.0,
                    "strength": "soft",
                    "operands": [{"role": "sensitive"}, {"role": "noisy"}],
                    "reason": "keep opamp off the FET",
                },
            ],
        },
    )
    mv = pcb.get(id="m", view="measures")
    assert "separation" in mv.body
    assert "VIOLATED" in mv.body  # 4mm < 10mm goal


def test_trace_view(pcb):
    pcb.put(
        id="t",
        args={
            "components": [
                {
                    "refdes": "R1",
                    "label": "4.7k",
                    "pins": [{"name": "1"}, {"name": "2"}],
                },
                {
                    "refdes": "U1",
                    "label": "MCU",
                    "pins": [{"name": "1"}, {"name": "2"}, {"name": "3"}],
                },
            ],
            "nets": [
                {"name": "NET_A", "class": "signal"},
                {"name": "NET_B", "class": "signal"},
            ],
            "connections": [
                {"net": "NET_A", "refdes": "R1", "pin": "1"},
                {"net": "NET_B", "refdes": "R1", "pin": "2"},
                {"net": "NET_B", "refdes": "U1", "pin": "3"},
            ],
        },
    )
    tr = pcb.get(id="t", view="trace", args={"net": "NET_A"})
    assert "NET_A" in tr.body and "NET_B" in tr.body
    assert "via R1" in tr.body


def test_unknown_view_raises(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    with pytest.raises(BadInput):
        pcb.get(id="sensor-node", view="bogus")


# ── auto-place (retired inline alias — pcb-guided-place-route Slice 10) ──
def test_autoplace_alias_enqueues_a_place_job_with_deprecation_note(pcb, store):
    """``args={'autoplace':...}`` no longer computes anything itself — it
    now enqueues the SAME ``pcb_place`` job ``op='place'`` does, and the
    crossing count is UNCHANGED right after (nothing ran inline). See
    ``tests/workers/test_pcb_place.py`` for the job's own placement-quality
    coverage."""
    pcb.put(id="x", args=_CROSSED)  # the X — 1 crossing
    before = pcb.get(id="x", view="crossings")
    assert "crossings — 1" in before.body

    resp = pcb.put(id="x", args={"autoplace": {"iters": 2000, "seed": 1}})
    assert "DEPRECATED" in resp.body
    assert "'op':'place'" in resp.body
    assert "enqueued" in resp.body

    ref = store.get_ref(kind="pcb", id="x")
    assert ref is not None
    with store.pool.connection() as conn:
        job_row = conn.execute(
            "SELECT meta->>'job_type', meta->'params'->>'pcb_ref_id' "
            "FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref.id,),
        ).fetchone()
    assert job_row == ("pcb_place", str(ref.id))

    # no heavy compute ran in the request path — crossings are unchanged.
    after = pcb.get(id="x", view="crossings")
    assert "crossings — 1" in after.body


def test_op_place_enqueues_and_is_idempotent_per_content_hash(pcb, store):
    pcb.put(id="idem-place", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="idem-place")
    assert ref is not None

    first = pcb.put(id="idem-place", args={"op": "place"})
    assert "enqueued" in first.body
    second = pcb.put(id="idem-place", args={"op": "place"})
    # same design state + same params -> the SAME job (dedupe), not a
    # second one — the (design, op, content-hash) idempotency contract.
    assert "existing job" in second.body or "for idem_key=" in second.body

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref.id,),
        ).fetchone()
        assert row is not None
        n = row[0]
    assert n == 1


def test_op_place_never_computes_inline(pcb, monkeypatch):
    """The serve thread-pool starvation lesson (backlog, verbatim): heavy
    compute must never run in the MCP request path. Patches the optimizer
    entry point to explode if called — ``op='place'`` must still succeed by
    only ever enqueuing a job."""
    from precis.pcb import optimize as pcb_optimize

    def _boom(*_a, **_k):
        raise AssertionError("optimize() must never run inline from put()")

    monkeypatch.setattr(pcb_optimize, "optimize", _boom)
    pcb.put(id="op-place-noinline", args=_CROSSED)
    resp = pcb.put(id="op-place-noinline", args={"op": "place"})
    assert "enqueued" in resp.body


def test_op_route_enqueues_a_pcb_route_job(pcb, store):
    pcb.put(id="route-enqueue", args=_CROSSED)
    resp = pcb.put(id="route-enqueue", args={"op": "route"})
    assert "enqueued" in resp.body
    ref = store.get_ref(kind="pcb", id="route-enqueue")
    assert ref is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'job_type' FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref.id,),
        ).fetchone()
    assert row == ("pcb_route",)


def test_op_place_rejects_non_4_layer_stackup(pcb, store):
    """v1 place/route only supports the default 4-layer board (backlog,
    verbatim decision) — a differently-sized stackup is rejected with a
    clear message rather than silently mis-routing a 2-layer board."""
    pcb.put(id="op-2layer", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="op-2layer")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_boards SET stackup = "
            '\'[{"name":"F.Cu","role":"signal"},'
            '{"name":"B.Cu","role":"signal"}]\'::jsonb '
            "WHERE board_id = %s",
            (board_id,),
        )
        conn.commit()
    with pytest.raises(BadInput, match="4-layer"):
        pcb.put(id="op-2layer", args={"op": "place"})


def test_op_unknown_rejected(pcb):
    pcb.put(id="op-bad", args=_CROSSED)
    with pytest.raises(BadInput, match="unknown op"):
        pcb.put(id="op-bad", args={"op": "levitate"})


def test_op_move_sets_position_and_lock(pcb, store):
    pcb.put(id="op-move", args=_CROSSED)
    resp = pcb.put(
        id="op-move",
        args={"op": "move", "refdes": "A", "x": 1.0, "y": 2.0, "fixed": "xy"},
    )
    assert "moved" in resp.body
    ref = store.get_ref(kind="pcb", id="op-move")
    assert ref is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT x, y, fixed FROM pcb_instances WHERE ref_id = %s AND refdes = 'A'",
            (ref.id,),
        ).fetchone()
    assert row == (1.0, 2.0, "xy")


def test_op_move_unknown_instance_not_found(pcb):
    pcb.put(id="op-move-404", args=_CROSSED)
    with pytest.raises(NotFound):
        pcb.put(id="op-move-404", args={"op": "move", "refdes": "NOPE", "x": 1.0})


def test_op_class_rules_sets_rules(pcb, store):
    pcb.put(id="op-classrules", args=_CROSSED)
    pcb.put(
        id="op-classrules",
        args={"op": "class_rules", "name": "power", "rules": {"clearance_mm": 0.3}},
    )
    ref = store.get_ref(kind="pcb", id="op-classrules")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    assert graph["net_classes"]["power"] == {"clearance_mm": 0.3}


def test_op_plane_net_rejects_unknown_layer(pcb):
    pcb.put(id="op-plane-bad", args=_CROSSED)
    with pytest.raises(BadInput, match="not in this board's stackup"):
        pcb.put(
            id="op-plane-bad",
            args={"op": "plane_net", "layer": "Nope.Cu", "net": "N1"},
        )


def test_op_rip_no_route_is_a_noop_response(pcb):
    pcb.put(id="op-rip", args=_CROSSED)
    resp = pcb.put(id="op-rip", args={"op": "rip", "net": "N1"})
    assert "already unrouted" in resp.body


def test_feasibility_view(pcb):
    pcb.put(id="x", args=_CROSSED)
    f = pcb.get(id="x", view="feasibility")
    assert "route feasibility" in f.body
    assert "vias needed" in f.body


# ── boards / net_classes / domain / route-status (pcb-guided-place-route
#    Slice 1, docs/backlog/pcb-guided-place-route.md) ───────────────────


def test_put_creates_default_board(pcb):
    resp = pcb.put(id="sensor-node", args=_DESIGN)
    # the netlist TOC surfaces the board name + stackup layer summary
    assert "board: main" in resp.body
    assert "4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu" in resp.body
    toc = pcb.get(id="sensor-node")
    assert "board: main" in toc.body
    assert "4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu" in toc.body


def test_stackup_default_content(pcb, store):
    from precis.pcb import DEFAULT_STACKUP

    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    design = store.pcb_load(ref.id)
    assert design["board"]["stackup"] == DEFAULT_STACKUP
    assert design["board"]["fold_lines"] == []


def test_pcb_ensure_board_is_idempotent(pcb, store):
    """Simulates the backfill semantics: a design's rows (any created
    before this call) resolve to the SAME default board on repeated calls,
    and the graph/TOC hydration picks it up."""
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    board_id_1 = store.pcb_ensure_board(ref.id)
    board_id_2 = store.pcb_ensure_board(ref.id)
    assert board_id_1 == board_id_2

    graph = store.pcb_graph(ref.id)
    assert graph["board"] is not None
    assert graph["board"]["board_id"] == board_id_1
    assert graph["board"]["name"] == "main"


def test_pcb_ensure_board_already_exists_returns_same_id(pcb, store):
    """Calling ensure twice for a design that already has a board (the
    common get-or-create path) returns the SAME id both times."""
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    first = store.pcb_ensure_board(ref.id)
    second = store.pcb_ensure_board(ref.id)
    assert first == second


def test_pcb_ensure_board_conflict_path_returns_existing(pcb, store, monkeypatch):
    """Simulates the concurrent-insert race pcb_boards_ref_name_key guards
    against: between our SELECT-miss and our INSERT, a concurrent session
    wins and commits the 'main' board first. Our INSERT ... ON CONFLICT
    DO NOTHING must absorb that (not raise UniqueViolation) and the
    get-or-create fallback must resolve to the concurrent winner's row
    (gr — Fix 2 of the pcb-guided-place-route Slice 1 review)."""
    from psycopg.types.json import Jsonb

    from precis.pcb import DEFAULT_STACKUP

    pcb.put(id="race-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="race-node")
    assert ref is not None
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM pcb_boards WHERE ref_id = %s", (ref.id,))
        conn.commit()

    winner: dict[str, int] = {}

    with store.pool.connection() as our_conn:
        real_execute = our_conn.execute
        calls = {"n": 0}

        def spy_execute(query, params=None, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                # our own SELECT (call 1) already came back empty; before
                # our INSERT (this call) runs, a concurrent session wins
                # the race and commits the board first.
                with store.pool.connection() as winner_conn:
                    row = winner_conn.execute(
                        "INSERT INTO pcb_boards (ref_id, name, stackup) "
                        "VALUES (%s, 'main', %s) RETURNING board_id",
                        (ref.id, Jsonb(DEFAULT_STACKUP)),
                    ).fetchone()
                    winner_conn.commit()
                    assert row is not None
                    winner["id"] = int(row[0])
            return real_execute(query, params, **kw)

        monkeypatch.setattr(our_conn, "execute", spy_execute)
        board_id = store._pcb_ensure_board(our_conn, ref.id)

    assert calls["n"] == 3  # SELECT (miss), INSERT ON CONFLICT (absorbed), re-SELECT
    assert board_id == winner["id"]


def test_domain_rejects_non_electrical(pcb):
    with pytest.raises(BadInput, match="electrical nets only"):
        pcb.put(
            id="fluidic-board",
            args={
                "components": [
                    {"refdes": "V1", "label": "valve", "pins": [{"name": "1"}]}
                ],
                "nets": [{"name": "COOLANT_IN", "domain": "fluidic"}],
            },
        )


def test_domain_defaults_electrical_and_accepts_explicit(pcb, store):
    resp = pcb.put(
        id="explicit-electrical",
        args={
            "components": [{"refdes": "U1", "label": "mcu", "pins": [{"name": "1"}]}],
            "nets": [
                {"name": "N1"},
                {"name": "N2", "domain": "electrical"},
            ],
        },
    )
    assert "created" in resp.body
    ref = store.get_ref(kind="pcb", id="explicit-electrical")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    domains = {n["name"]: n["domain"] for n in graph["nets"]}
    assert domains == {"N1": "electrical", "N2": "electrical"}
    # the column itself, not just the read-path default (gr — Fix 3: the
    # write path used to silently drop `domain`, and the DB DEFAULT was
    # indistinguishable from an explicit 'electrical' at this level).
    with store.pool.connection() as conn:
        rows = dict(
            conn.execute(
                "SELECT name, domain FROM pcb_nets WHERE ref_id = %s", (ref.id,)
            ).fetchall()
        )
    assert rows == {"N1": "electrical", "N2": "electrical"}


def test_domain_round_trips_non_default_value(pcb, store):
    """Proves the read path carries the REAL `domain` column value rather
    than a hardcoded 'electrical' — insert a net with domain='fluidic'
    directly via SQL (the handler rejects it at put(), so this is the only
    way to seed one pre-Slice-2) and confirm both domain-projecting reads
    (:meth:`pcb_graph`, :meth:`pcb_route_status`) hydrate it (gr — Fix 3 of
    the pcb-guided-place-route Slice 1 review)."""
    pcb.put(
        id="fluidic-seed",
        args={
            "components": [{"refdes": "V1", "label": "valve", "pins": [{"name": "1"}]}]
        },
    )
    ref = store.get_ref(kind="pcb", id="fluidic-seed")
    assert ref is not None
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pcb_nets (ref_id, name, domain) VALUES (%s, %s, %s)",
            (ref.id, "COOLANT_IN", "fluidic"),
        )
        conn.commit()

    graph = store.pcb_graph(ref.id)
    domains = {n["name"]: n["domain"] for n in graph["nets"]}
    assert domains["COOLANT_IN"] == "fluidic"

    status_rows = {r["name"]: r["domain"] for r in store.pcb_route_status(ref.id)}
    assert status_rows["COOLANT_IN"] == "fluidic"


def test_net_classes_upsert_and_toc_visibility(pcb, store):
    resp = pcb.put(
        id="sensor-node",
        args={
            **_DESIGN,
            "net_classes": {
                "i2c": {"clearance_mm": 0.2, "track_width_mm": 0.25},
            },
        },
    )
    assert "+1 net_class(es)" in resp.body
    assert "net classes" in resp.body
    assert "i2c" in resp.body

    toc = pcb.get(id="sensor-node")
    assert "i2c" in toc.body

    # re-put with different rules upserts (does not duplicate the class)
    again = pcb.put(
        id="sensor-node",
        args={"net_classes": {"i2c": {"clearance_mm": 0.3}}},
    )
    assert "+1 net_class(es)" in again.body
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    assert graph["net_classes"] == {"i2c": {"clearance_mm": 0.3}}


def test_put_net_classes_atomic_with_design_write(pcb, store):
    """A blank net_class name errors out of pcb_upsert_net_classes — but the
    design write from pcb_apply in the SAME put() must not have been
    committed either. Before the fix, pcb_apply ran in its own tx (already
    committed) and pcb_upsert_net_classes ran in a second tx that then
    raised BadInput, leaving a design behind despite the error (gr — Fix 1
    of the pcb-guided-place-route Slice 1 review)."""
    with pytest.raises(BadInput):
        pcb.put(
            id="atomic-fail",
            args={**_DESIGN, "net_classes": {"  ": {"clearance_mm": 0.2}}},
        )
    assert store.get_ref(kind="pcb", id="atomic-fail") is None


def test_route_status_view_all_unrouted(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    resp = pcb.get(id="sensor-node", view="route-status")
    assert "route status" in resp.body
    assert "unrouted" in resp.body
    # every net in _DESIGN shows up
    assert "I2C_SCL" in resp.body and "GND" in resp.body and "VCC3V3" in resp.body


def test_route_status_view_reflects_seeded_routes(pcb, store):
    """Seeds a pcb_routes row directly via SQL (no route-writing op ships in
    this slice) and confirms the view reads real status, not just the
    all-unrouted default."""
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    with store.pool.connection() as conn:
        net_id = conn.execute(
            "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = 'I2C_SCL'",
            (ref.id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO pcb_routes (board_id, net_id, status) VALUES (%s, %s, %s)",
            (board_id, net_id, "sketched"),
        )
        conn.commit()
    resp = pcb.get(id="sensor-node", view="route-status")
    assert "sketched" in resp.body
    assert "1 sketched" in resp.body


def test_pcb_copper_list_excludes_a_retired_net(pcb, store):
    """A copper row on a retired net must not leak into ``view='drc'``'s
    input model (gr — Finding 3: ``pcb_copper_list`` was missing the same
    ``n.retired_at IS NULL`` filter every sibling method in this file
    applies)."""
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    net_id = store.pcb_net_ids(ref.id)["I2C_SCL"]
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_id,
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [1.0, 0.0]}
                    ],
                    "width_mm": 0.25,
                },
            }
        ],
    )
    assert store.pcb_copper_list(board_id)  # present while the net is live
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_nets SET retired_at = now() WHERE net_id = %s", (net_id,)
        )
        conn.commit()
    assert store.pcb_copper_list(board_id) == []


def test_pcb_rip_route_ignores_a_retired_net(pcb, store):
    """A retired net's stale ``pcb_routes`` row must not be rippable by
    name — a later live net that reuses that name (rename/merge) could
    otherwise have ITS route ripped by a rip-up call meant for the retired
    one (gr — Finding 3: ``pcb_rip_route`` was missing the same
    ``n.retired_at IS NULL`` filter every sibling method in this file
    applies)."""
    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    net_id = store.pcb_net_ids(ref.id)["I2C_SCL"]
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pcb_routes (board_id, net_id, status) VALUES (%s, %s, %s)",
            (board_id, net_id, "realized"),
        )
        conn.execute(
            "UPDATE pcb_nets SET retired_at = now() WHERE net_id = %s", (net_id,)
        )
        conn.commit()
    assert store.pcb_rip_route(ref.id, "I2C_SCL") is False


# ── congestion / planes views (pcb-guided-place-route Slice 10) ─────────


def test_congestion_and_planes_views_are_discoverable():
    assert "congestion" in PcbHandler.spec.views
    assert "planes" in PcbHandler.spec.views


def test_congestion_view_before_any_route_run(pcb):
    pcb.put(id="cong-none", args=_CROSSED)
    resp = pcb.get(id="cong-none", view="congestion")
    assert "no route run yet" in resp.body


def test_congestion_view_reads_last_route_meta(pcb, store):
    from psycopg.types.json import Jsonb

    pcb.put(id="cong-seeded", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="cong-seeded")
    assert ref is not None
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "last_route": {
                            "realized": 1,
                            "failed": 1,
                            "warnings": ["gap 0.20 mm between A/B needs 0.30 mm"],
                        }
                    }
                ),
                ref.id,
            ),
        )
        conn.commit()
    resp = pcb.get(id="cong-seeded", view="congestion")
    assert "1 realized" in resp.body
    assert "1 failed" in resp.body
    assert "needs 0.30 mm" in resp.body


def test_planes_view_empty_then_assigned(pcb, store):
    pcb.put(id="planes-x", args=_CROSSED)
    empty = pcb.get(id="planes-x", view="planes")
    assert "no plane assignments" in empty.body

    pcb.put(id="planes-x", args={"op": "plane_net", "layer": "In1.Cu", "net": "N1"})
    resp = pcb.get(id="planes-x", view="planes")
    assert "In1.Cu" in resp.body
    assert "N1" in resp.body


# ── svg view (pcb-svg-render) ────────────────────────────────────────


def test_svg_view_is_discoverable():
    assert "svg" in PcbHandler.spec.views


def test_svg_view_sketch_level_with_no_parts_yet(pcb):
    # Every put() makes a default board, so "no board yet" isn't reachable
    # here — this exercises the emptier "no parts to sketch" guard.
    pcb.put(id="empty-sketch", args={"components": [], "nets": []})
    resp = pcb.get(id="empty-sketch", view="svg", args={"level": "sketch"})
    assert "nothing to sketch" in resp.body


def test_svg_view_board_level_renders_outline_only_before_any_route(pcb):
    pcb.put(id="x", args=_CROSSED)
    resp = pcb.get(id="x", view="svg")
    assert resp.body.strip().startswith("<?xml")
    assert "<svg" in resp.body and "</svg>" in resp.body
    assert "viewBox" in resp.body
    # no op='route' has run yet -> pcb_copper is empty, board render is
    # outline + scale bar only, never an error.
    assert 'class="scale-bar"' in resp.body


def test_svg_view_sketch_level_renders_placed_components(pcb):
    pcb.put(id="x2", args=_CROSSED)
    resp = pcb.get(id="x2", view="svg", args={"level": "sketch"})
    assert "<svg" in resp.body
    assert "A" in resp.body and "B" in resp.body  # refdes labels


def test_svg_view_layers_and_include_args_accepted(pcb):
    pcb.put(id="x3", args=_CROSSED)
    resp = pcb.get(
        id="x3",
        view="svg",
        args={"layers": ["F.Cu"], "include": ["outline"]},
    )
    assert "<svg" in resp.body


def test_svg_view_bad_level_is_bad_input(pcb):
    pcb.put(id="x4", args=_CROSSED)
    with pytest.raises(BadInput):
        pcb.get(id="x4", view="svg", args={"level": "nonsense"})
