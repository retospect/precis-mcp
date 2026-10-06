"""PcbHandler end-to-end against a live store.

Exercises the batch authoring path (put with components/nets/connections),
the netlist TOC, the graph-traversal reads (instance neighbourhood, net
members), re-runnability, and soft-delete. Uses the shared ``store`` fixture.
"""

from __future__ import annotations

import zipfile
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb import catalog
from precis.pcb import drc as pcb_drc

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


def test_pcb_graph_netconns_members_are_sorted(pcb, store):
    """gr296327: ``Store.pcb_graph``'s netconns SELECT (src/precis/store/
    _pcb_ops.py) had no ``ORDER BY``, so a net's ``members`` list order
    tracked Postgres scan order — not insertion order — a trap for any
    consumer reading ``graph["nets"]`` directly instead of through
    ``pcb_session.sorted_graph``. Insert one net's connections in shuffled
    (non-alphabetical) refdes order and confirm ``pcb_graph`` hands back
    members sorted by ``(refdes, pin)`` straight off the SELECT."""
    design = {
        "components": [
            {"refdes": "Z1", "label": "r", "pins": [{"name": "1"}]},
            {"refdes": "A1", "label": "r", "pins": [{"name": "1"}]},
            {"refdes": "M1", "label": "r", "pins": [{"name": "1"}]},
        ],
        "nets": [{"name": "BUS"}],
        "connections": [
            {"net": "BUS", "refdes": "Z1", "pin": "1"},
            {"net": "BUS", "refdes": "A1", "pin": "1"},
            {"net": "BUS", "refdes": "M1", "pin": "1"},
        ],
    }
    pcb.put(id="shuffled-net", args=design)
    ref = store.get_ref(kind="pcb", id="shuffled-net")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    (bus,) = [n for n in graph["nets"] if n["name"] == "BUS"]
    assert [m["refdes"] for m in bus["members"]] == ["A1", "M1", "Z1"]


def test_pcb_graph_round_trips_group_and_pattern_fields(pcb, store):
    """The placement-constraint fields (rigid super-footprint ``group`` +
    ``group_offset``, repeated-tile ``pattern`` + ``pattern_instance``)
    ride ``pcb_instances.meta`` through create and come back HOISTED onto
    the instance dict — the exact top-level shape
    ``precis.pcb.ir.from_graph`` parses. A component without them must
    come back without the keys at all (no ``None`` placeholders), so a
    group-less design's graph is unchanged."""
    design = {
        "components": [
            {
                "refdes": "J1",
                "label": "HDR-1x2",
                "pins": [{"name": "1"}, {"name": "2"}],
                "group": "hdr_pair",
                "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
            },
            {
                "refdes": "J2",
                "label": "HDR-1x2",
                "pins": [{"name": "1"}, {"name": "2"}],
                "group": "hdr_pair",
                "group_offset": {"x": 15.24, "y": 0.0, "rot": 0.0},
            },
            {
                "refdes": "Q1",
                "label": "TO-220",
                "pins": [{"name": "1"}, {"name": "2"}, {"name": "3"}],
                "pattern": "channel",
                "pattern_instance": 0,
            },
            {
                "refdes": "R1",
                "label": "RES-0402",
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [{"name": "GND"}],
        "connections": [
            {"net": "GND", "refdes": "Q1", "pin": "3"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
        ],
    }
    pcb.put(id="grouped-node", args=design)
    ref = store.get_ref(kind="pcb", id="grouped-node")
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i for i in graph["instances"]}
    assert by_refdes["J1"]["group"] == "hdr_pair"
    assert by_refdes["J1"]["group_offset"] == {"x": 0.0, "y": 0.0, "rot": 0.0}
    assert by_refdes["J2"]["group_offset"] == {"x": 15.24, "y": 0.0, "rot": 0.0}
    assert by_refdes["Q1"]["pattern"] == "channel"
    assert by_refdes["Q1"]["pattern_instance"] == 0
    for key in ("group", "group_offset", "pattern", "pattern_instance"):
        assert key not in by_refdes["R1"]


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


# A 3-member signal net on a line, member order chosen so Prim's MST
# (precis.pcb.ratsnest._mst_edges, rooted at members[0]) picks a different
# root — and thus different from/to edge directions — depending on which
# end of the (refdes, pin)-sorted member list the store happens to hand
# back first.
_MST_LINE = {
    "components": [
        {"refdes": "A1", "label": "r", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B1", "label": "r", "x": 5.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "C1", "label": "r", "x": 12.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "BUS", "class": "signal"}],
    "connections": [
        {"net": "BUS", "refdes": "A1", "pin": "1"},
        {"net": "BUS", "refdes": "B1", "pin": "1"},
        {"net": "BUS", "refdes": "C1", "pin": "1"},
    ],
}


def test_ratsnest_view_stable_despite_unsorted_store_member_order(
    pcb, store, monkeypatch
):
    """gr296327: ``PcbHandler.get(view='ratsnest'/'crossings'/'feasibility')``
    (handlers/pcb.py ``_render_view``) reads ``graph["nets"]`` straight off
    ``Store.pcb_graph`` — before the fix this bypassed the ``(refdes, pin)``
    normalization ``pcb_session.sorted_graph``/``build_ir`` applies, so a
    differently-ordered store read (Postgres scan-order nondeterminism)
    could flip which airwire direction the ratsnest MST reports. Simulate
    an out-of-order store read by monkeypatching ``pcb_graph`` to reverse
    every net's members, and confirm the rendered view is byte-identical
    to the natural-order call — i.e. the handler is normalizing via
    ``sorted_graph`` regardless of what the store hands back."""
    pcb.put(id="mst-line", args=_MST_LINE)
    forward = pcb.get(id="mst-line", view="ratsnest").body
    real_pcb_graph = store.pcb_graph

    def reversed_pcb_graph(ref_id: int) -> dict:
        graph = real_pcb_graph(ref_id)
        graph["nets"] = [
            {**n, "members": list(reversed(n["members"]))} for n in graph["nets"]
        ]
        return graph

    monkeypatch.setattr(store, "pcb_graph", reversed_pcb_graph)
    reversed_body = pcb.get(id="mst-line", view="ratsnest").body
    assert reversed_body == forward

    # Twice in a row through the (now-monkeypatched) unsorted store must
    # also agree with itself — the view is stable across calls, not just
    # against the natural-order baseline.
    assert pcb.get(id="mst-line", view="ratsnest").body == reversed_body


def test_drc_view_before_any_route_run_and_with_no_cached_footprint_bails(pcb):
    # Geometric DRC (pcb-guided-place-route Slice 8) checks REALIZED copper
    # (pcb_copper) — before op='route' has ever run there is none. Round 4
    # (docs/backlog/pcb-ewod-multitile.md's decisions log) widened this
    # view to still run a pads-only pass whenever REAL (non-synthesized)
    # pad geometry exists (see the next test) — but _DESIGN's parts are
    # real LCSC numbers with NO `part_footprints` row seeded in this
    # store, so every pad here is a synthesized BOUND, and the old bail
    # is still exactly correct: DRC over a dimensionally-plausible guess
    # is meaningless. See tests/test_pcb_drc.py for the engine's own
    # rule/oracle coverage.
    pcb.put(id="sensor-node", args=_DESIGN)
    drc = pcb.get(id="sensor-node", view="drc")
    assert "no realized copper yet" in drc.body


def test_drc_view_runs_pads_only_before_any_route_when_pads_are_real(pcb):
    """Round-4 contract (docs/backlog/pcb-ewod-multitile.md's decisions
    log): a board with REAL (authored-local or cached) pad geometry but
    no realized copper yet — every net here is fanout-1, so a router
    would never touch it either way, exactly the ``ewod_pad_array``
    motivating case — now gets a full pads-only DRC pass instead of "no
    realized copper yet". The response states the reduced scope
    explicitly so a clean pads-only pass is never mistaken for a full
    one."""
    design = {
        "footprints": [
            {
                "name": "pad1",
                "pads": [
                    {
                        "pin": "1",
                        "shape": "rect",
                        "x": 0.0,
                        "y": 0.0,
                        "w": 1.0,
                        "h": 1.0,
                    }
                ],
            }
        ],
        "components": [
            {
                "refdes": "E1",
                "label": "electrode",
                "footprint": "pad1",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            }
        ],
        "nets": [{"name": "N1"}],
        "connections": [{"net": "N1", "refdes": "E1", "pin": "1"}],
    }
    pcb.put(id="real-pads-no-route", args=design)
    drc = pcb.get(id="real-pads-no-route", view="drc")
    assert "no realized copper yet" not in drc.body
    assert "pads-only DRC" in drc.body


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


def test_outline_corner_radius_mm_rounds_the_parsed_outline(pcb):
    """``_outline_from_features`` is the single authoritative outline
    parse point every view/export reads -- a ``corner_radius_mm`` on the
    outline feature's ``geom`` must round every corner there, once, so
    every consumer (pours, DRC, silk, gerber/SVG render) inherits it for
    free without any of them special-casing a radius."""
    design: dict[str, Any] = {
        "components": [],
        "nets": [],
        "connections": [],
        "features": [
            {
                "ftype": "outline",
                "geom": {
                    "path": [[0, 0], [62, 0], [62, 46], [0, 46], [0, 0]],
                    "corner_radius_mm": 3.0,
                },
            },
        ],
    }
    pcb.put(id="rounded-board", args=design)
    ref = pcb.store.get_ref(kind="pcb", id="rounded-board")
    outline = pcb._outline_from_features(ref.id)
    assert outline is not None
    assert len(outline) > 5
    for x, y in outline:
        assert -1e-9 <= x <= 62.0 + 1e-9
        assert -1e-9 <= y <= 46.0 + 1e-9


def test_outline_without_corner_radius_mm_is_unchanged(pcb):
    """Absent ``corner_radius_mm`` -- today's behaviour, byte for byte."""
    path = [[0, 0], [62, 0], [62, 46], [0, 46], [0, 0]]
    design: dict[str, Any] = {
        "components": [],
        "nets": [],
        "connections": [],
        "features": [{"ftype": "outline", "geom": {"path": path}}],
    }
    pcb.put(id="sharp-board", args=design)
    ref = pcb.store.get_ref(kind="pcb", id="sharp-board")
    outline = pcb._outline_from_features(ref.id)
    assert outline == [[float(x), float(y)] for x, y in path]


def _many_pins(n: int) -> list[dict[str, Any]]:
    return [{"name": str(i)} for i in range(1, n + 1)]


def test_drc_view_reports_courtyard_overlap_with_real_derived_geometry(pcb):
    """Defect: the DRC courtyard check read a flat 1.0mm radius
    (``DEFAULT_COURTYARD_RADIUS_MM``) for EVERY instance regardless of
    its actual size -- smaller than any real multi-pin part's derived
    keep-out, so placement (which uses the real, pad-geometry-derived
    shape) always separated parts further apart than the flat DRC check
    would ever flag. The rule was dormant by construction, on both
    reference fixtures, on every seed.

    Two 12-pin ("dual" package family) parts, offset along the axis their
    footprint is LONG in. Each one's real courtyard
    (:func:`precis.pcb.ir.instance_courtyard_polygon`) measures 2.84 x
    4.18mm -- a tall dual column, not a disc -- so at 3.0mm apart in y
    they overlap by ~1.2mm, while their flat 1.0mm nominal courtyards do
    not even touch (sum 2.0mm < 3.0mm). That gap between the two answers
    is exactly how the defect stayed invisible.

    **Offset in y, not x, and that is the point of the fixture.** The
    same two parts 3.0mm apart in X do not overlap at all (half-width
    1.42mm each), so a radius -- any radius -- gets one of the two
    directions wrong. Only a shape can be right about both."""
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
                "x": 0.0,
                "y": 3.0,
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
                        {"shape": "line", "start": [0.0, 0.0], "end": [0.0, 3.0]}
                    ],
                    "width_mm": 0.2,
                },
            },
        ],
    )
    pcb.store.pcb_routes_write(ref.id, board_id, {"N1": {"status": "realized"}})
    drc = pcb.get(id="big-parts", view="drc")
    assert "courtyard_overlap" in drc.body, drc.body
    # The counterfactual, stated rather than implied: a flat nominal
    # courtyard on both parts would not have reached across this gap,
    # so a passing assertion above really is the derived geometry
    # talking and not the fallback constant.
    assert 2 * pcb_drc.DEFAULT_COURTYARD_RADIUS_MM < 3.0


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


def test_op_route_dedup_key_carries_the_route_code_version(pcb, store, monkeypatch):
    """Re-putting op='route' on an unchanged board dedupes to the same job;
    a route-code version bump enqueues a NEW one (the old job's result was
    produced by older code)."""
    from precis.workers.job_types import pcb_route as pcb_route_job

    pcb.put(id="idem-route", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="idem-route")
    assert ref is not None

    def _n_jobs() -> int:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
                (ref.id,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    pcb.put(id="idem-route", args={"op": "route"})
    pcb.put(id="idem-route", args={"op": "route"})
    assert _n_jobs() == 1

    monkeypatch.setattr(pcb_route_job, "CODE_VERSION", pcb_route_job.CODE_VERSION + 1)
    pcb.put(id="idem-route", args={"op": "route"})
    assert _n_jobs() == 2


def test_op_place_never_computes_inline(pcb, store, monkeypatch):
    """The serve thread-pool starvation lesson (backlog, verbatim): heavy
    compute must never run in the MCP request path. Patches the optimizer
    entry point to explode if called — ``op='place'`` must still succeed by
    only ever enqueuing a job."""
    from precis.pcb import optimize as pcb_optimize

    # Import the job module BEFORE patching: the registry imports it lazily
    # and it binds `optimize` into its own globals at import time — if the
    # first import happens while the patch is live (this test's enqueue),
    # _boom is captured into pcb_place's namespace permanently, outliving
    # monkeypatch teardown and exploding every later pcb_place drain in
    # this worker. Patch both bindings so teardown restores both.
    from precis.workers.job_types import pcb_place as pcb_place_job

    def _boom(*_a, **_k):
        raise AssertionError("optimize() must never run inline from put()")

    monkeypatch.setattr(pcb_optimize, "optimize", _boom)
    monkeypatch.setattr(pcb_place_job, "optimize", _boom)
    pcb.put(id="op-place-noinline", args=_CROSSED)
    resp = pcb.put(id="op-place-noinline", args={"op": "place"})
    assert "enqueued" in resp.body

    # This test only asserts optimize() never runs inline — the enqueued
    # job itself is doomed (the patched optimize() explodes the moment
    # anything actually drains it) and this test never drains it. Left
    # queued, it strands in the shared per-worker test DB for the next
    # test file's drain helper to claim and misread as ITS OWN failure
    # (gr295496) — delete it rather than leave an orphan behind.
    ref = store.get_ref(kind="pcb", id="op-place-noinline")
    assert ref is not None
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM refs WHERE kind = 'job' AND parent_id = %s", (ref.id,)
        )
        conn.commit()


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


@pytest.mark.parametrize("negotiate", [0, 10, 100])
def test_op_route_negotiate_enqueues_and_dedupes_exact_params(pcb, store, negotiate):
    pcb.put(id="op-negotiate-valid", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="op-negotiate-valid")
    assert ref is not None
    args = {"op": "route", "seed": 0, "iters": 3000, "negotiate": negotiate}
    first = pcb.put(id="op-negotiate-valid", args=args)
    assert "enqueued" in first.body
    pcb.put(id="op-negotiate-valid", args=args)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->'params' FROM refs WHERE kind='job' AND parent_id=%s",
            (ref.id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == {
        "pcb_ref_id": ref.id,
        "seed": 0,
        "iters": 3000,
        "negotiate": negotiate,
    }
    # An omitted knob remains the existing off-by-default contract, and
    # must not dedupe to an enabled negotiation job.
    pcb.put(id="op-negotiate-valid", args={"op": "route", "seed": 0, "iters": 3000})
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->'params' FROM refs WHERE kind='job' AND parent_id=%s",
            (ref.id,),
        ).fetchall()
    assert len(rows) == 2
    assert any("negotiate" not in r[0] for r in rows)


@pytest.mark.parametrize("negotiate", [-1, 101])
def test_op_route_negotiate_out_of_range_is_bad_input_not_a_long_job(pcb, negotiate):
    pcb.put(id="op-negotiate", args=_CROSSED)
    with pytest.raises(BadInput, match="negotiate"):
        pcb.put(id="op-negotiate", args={"op": "route", "negotiate": negotiate})


def test_op_unknown_rejected(pcb):
    pcb.put(id="op-bad", args=_CROSSED)
    with pytest.raises(BadInput, match="unknown op"):
        pcb.put(id="op-bad", args={"op": "levitate"})


def test_op_move_sets_position_and_lock(pcb, store):
    pcb.put(id="op-move", args=_CROSSED)
    resp = pcb.put(
        id="op-move",
        args={"op": "move", "refdes": "A", "x": 10.0, "y": 12.0, "fixed": "xy"},
    )
    assert "moved" in resp.body
    ref = store.get_ref(kind="pcb", id="op-move")
    assert ref is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT x, y, fixed FROM pcb_instances WHERE ref_id = %s AND refdes = 'A'",
            (ref.id,),
        ).fetchone()
    assert row == (10.0, 12.0, "xy")


def test_op_move_onto_another_part_is_refused_naming_the_rule_and_part(pcb, store):
    """docs/backlog/pcb-always-valid-board-invariant.md acceptance: a move
    that would overlap another part's courtyard is refused, and the board
    keeps the old pose."""
    pcb.put(id="op-move-bad", args=_CROSSED)
    with pytest.raises(BadInput, match="courtyard_overlap with B"):
        pcb.put(
            id="op-move-bad", args={"op": "move", "refdes": "A", "x": 2.0, "y": 2.0}
        )
    ref = store.get_ref(kind="pcb", id="op-move-bad")
    assert ref is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT x, y FROM pcb_instances WHERE ref_id = %s AND refdes = 'A'",
            (ref.id,),
        ).fetchone()
    assert row == (0.0, 0.0)


def test_op_move_to_a_clear_spot_is_not_refused(pcb):
    """The negative control: without it the refusal could be vacuous."""
    pcb.put(id="op-move-ok", args=_CROSSED)
    resp = pcb.put(
        id="op-move-ok", args={"op": "move", "refdes": "A", "x": 20.0, "y": 20.0}
    )
    assert "moved" in resp.body


def test_op_move_onto_a_mounting_hole_is_refused(pcb):
    pcb.put(id="op-move-hole", args=_CROSSED)
    pcb.put(
        id="op-move-hole",
        args={
            "features": [
                {
                    "ftype": "mounting_hole",
                    "x": 30.0,
                    "y": 30.0,
                    "geom": {"diameter": 3.2},
                }
            ]
        },
    )
    with pytest.raises(BadInput, match="courtyard_hole with hole @ \\(30, 30\\)"):
        pcb.put(
            id="op-move-hole", args={"op": "move", "refdes": "A", "x": 30.5, "y": 30.0}
        )


def _two_parts(ax: float, ay: float, bx: float = 10.0, by: float = 10.0):
    return {
        "components": [
            {"refdes": "A", "label": "ic", "x": ax, "y": ay, "pins": [{"name": "1"}]},
            {"refdes": "B", "label": "ic", "x": bx, "y": by, "pins": [{"name": "1"}]},
        ],
        "nets": [],
        "connections": [],
    }


def _put_standing(pcb, store, slug, design):
    """Author ``design`` unplaced, then write its poses straight to the store.
    ``put`` refuses to create a violation, so this is how a test builds a
    board that already carries one."""
    poses = {
        c["refdes"]: (c["x"], c["y"], float(c.get("rot") or 0.0))
        for c in design["components"]
        if "x" in c
    }
    bare = {
        **design,
        "components": [
            {k: v for k, v in c.items() if k not in ("x", "y")}
            for c in design["components"]
        ],
    }
    pcb.put(id=slug, args=bare)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    store.pcb_set_pose(ref.id, poses)


def _move_a(pcb, slug, x, y, **extra):
    return pcb.put(id=slug, args={"op": "move", "refdes": "A", "x": x, "y": y, **extra})


_STANDING = "pre-existing DRC error"


def test_op_move_delta_courtyard_overlap_standing_may_shrink_never_grow(pcb, store):
    """A standing courtyard overlap is repairable: a move that shrinks it
    passes and is reported, one that grows it is refused with the depth,
    one that clears it passes with no report."""
    slug = "mv-delta-pair"
    _put_standing(pcb, store, slug, _two_parts(0.0, 0.0, 0.3, 0.0))
    with pytest.raises(BadInput, match=r"courtyard_overlap with B \(worse: "):
        _move_a(pcb, slug, 0.2, 0.0)
    assert _xy(store, slug, "A") == (0.0, 0.0)
    resp = _move_a(pcb, slug, -0.1, 0.0)
    assert _STANDING in resp.body and "courtyard_overlap with B" in resp.body
    resp = _move_a(pcb, slug, 20.0, 20.0)
    assert "moved" in resp.body and _STANDING not in resp.body


def test_op_move_delta_list_form_reports_standing_overlap(pcb, store):
    slug = "mv-delta-pair-list"
    _put_standing(pcb, store, slug, _two_parts(0.0, 0.0, 0.3, 0.0))
    resp = pcb.put(
        id=slug,
        args={"op": "move", "moves": [{"refdes": "A", "x": -0.1, "y": 0.0}]},
    )
    assert _STANDING in resp.body and "courtyard_overlap: A with B" in resp.body
    with pytest.raises(BadInput, match="worse"):
        pcb.put(
            id=slug,
            args={"op": "move", "moves": [{"refdes": "A", "x": 0.1, "y": 0.0}]},
        )


def test_op_move_delta_outline_part_outside_may_move_in_never_further_out(pcb, store):
    slug = "mv-delta-outline"
    design = _two_parts(0.2, 20.0)
    design["features"] = [
        {
            "ftype": "outline",
            "geom": {"path": [[0, 0], [40, 0], [40, 40], [0, 40], [0, 0]]},
        }
    ]
    _put_standing(pcb, store, slug, design)
    with pytest.raises(BadInput, match=r"outline with board outline \(worse: "):
        _move_a(pcb, slug, -0.3, 20.0)
    resp = _move_a(pcb, slug, 0.35, 20.0)
    assert _STANDING in resp.body and "outline with board outline" in resp.body
    resp = _move_a(pcb, slug, 20.0, 20.0)
    assert "moved" in resp.body and _STANDING not in resp.body
    # negative control: from fully inside, a move partly out is NEW, refused
    with pytest.raises(BadInput, match="outline with board outline"):
        _move_a(pcb, slug, 0.2, 20.0)


def test_op_move_delta_mounting_hole_standing_may_shrink_never_grow(pcb, store):
    slug = "mv-delta-hole"
    pcb.put(id=slug, args=_two_parts(31.5, 30.0))
    # put would refuse a hole under a placed part; plant it in the store.
    store.pcb_apply(
        slug=slug,
        title=slug,
        components=[],
        nets=[],
        connections=[],
        features=[
            {
                "ftype": "mounting_hole",
                "x": 30.0,
                "y": 30.0,
                "geom": {"diameter": 3.2},
            }
        ],
    )
    with pytest.raises(
        BadInput, match=r"courtyard_hole with hole @ \(30, 30\) \(worse: "
    ):
        _move_a(pcb, slug, 31.0, 30.0)
    resp = _move_a(pcb, slug, 31.8, 30.0)
    assert _STANDING in resp.body and "courtyard_hole with hole @ (30, 30)" in resp.body
    resp = _move_a(pcb, slug, 20.0, 20.0)
    assert "moved" in resp.body and _STANDING not in resp.body


def test_op_move_delta_new_overlap_with_an_untouched_part_still_refused(pcb, store):
    """Negative control: a standing overlap A/B does not excuse a NEW one
    with C."""
    slug = "mv-delta-new"
    design = _two_parts(0.0, 0.0, 0.3, 0.0)
    design["components"].append(
        {"refdes": "C", "label": "ic", "x": -6.0, "y": 0.0, "pins": [{"name": "1"}]}
    )
    _put_standing(pcb, store, slug, design)
    with pytest.raises(BadInput, match="courtyard_overlap with C"):
        _move_a(pcb, slug, -6.1, 0.0)


def test_op_move_unknown_instance_not_found(pcb):
    pcb.put(id="op-move-404", args=_CROSSED)
    with pytest.raises(NotFound):
        pcb.put(id="op-move-404", args={"op": "move", "refdes": "NOPE", "x": 1.0})


def _xy(store, slug, refdes):
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return _poses(store, ref.id)[refdes][:2]


def test_op_move_list_swaps_two_parts_neither_can_reach_alone(pcb, store):
    """Reto's ruling 2 (pcb-always-valid-board-invariant.md): a swap needs no
    parking spot. A and B sit 2.0 mm apart; each single move onto the
    other's slot overlaps the other, the joint move is legal."""
    pcb.put(id="mv-swap", args=_CROSSED)
    for rd, x, y in (("A", 2.0, 2.0), ("B", 0.0, 0.0)):
        with pytest.raises(BadInput, match="courtyard_overlap"):
            pcb.put(id="mv-swap", args={"op": "move", "refdes": rd, "x": x, "y": y})
    resp = pcb.put(
        id="mv-swap",
        args={
            "op": "move",
            "moves": [
                {"refdes": "A", "x": 2.0, "y": 2.0},
                {"refdes": "B", "x": 0.0, "y": 0.0},
            ],
        },
    )
    assert "A" in resp.body and "B" in resp.body
    assert _xy(store, "mv-swap", "A") == (2.0, 2.0)
    assert _xy(store, "mv-swap", "B") == (0.0, 0.0)


def test_op_move_list_with_one_illegal_target_moves_nothing(pcb, store):
    """Negative control: A's target is clear, B's lands on C; the whole list
    is refused naming B and C, and A did not move either."""
    pcb.put(id="mv-swap-bad", args=_CROSSED)
    with pytest.raises(BadInput, match="courtyard_overlap: B with C"):
        pcb.put(
            id="mv-swap-bad",
            args={
                "op": "move",
                "moves": [
                    {"refdes": "A", "x": 20.0, "y": 20.0},
                    {"refdes": "B", "x": 0.0, "y": 2.0},
                ],
            },
        )
    assert _xy(store, "mv-swap-bad", "A") == (0.0, 0.0)
    assert _xy(store, "mv-swap-bad", "B") == (2.0, 2.0)


def test_op_move_list_with_a_generator_member_moves_its_group_and_copper(pcb, store):
    ref, board_id = _gen_board(pcb, "mv-list-grp")
    poses0 = _poses(store, ref.id)
    fixed0 = _fixed_snapshot(store, board_id)
    resp = pcb.put(
        id="mv-list-grp",
        args={
            "op": "move",
            "moves": [
                {"refdes": "ARR1", "x": 20.0, "y": 15.0},
                {"refdes": "P2", "x": 330.0, "y": 300.0},
            ],
        },
    )
    poses1 = _poses(store, ref.id)
    sinks = [r for r in poses0 if r.startswith("ARR1_SINK_")]
    assert len(sinks) == 4
    for name in ["ARR1", *sinks]:
        assert name in resp.body
        assert (
            poses1[name][0] - poses0[name][0],
            poses1[name][1] - poses0[name][1],
        ) == (pytest.approx((20.0, 15.0), abs=1e-9))
    assert poses1["P2"][:2] == (330.0, 300.0)
    assert poses1["P1"] == poses0["P1"]
    assert _fixed_snapshot(store, board_id) == _shifted(fixed0, 20.0, 15.0)


def test_op_move_list_refuses_a_part_twice_and_two_entries_of_one_group(pcb, store):
    ref, board_id = _gen_board(pcb, "mv-list-dup")
    poses0 = _poses(store, ref.id)
    fixed0 = _fixed_snapshot(store, board_id)
    with pytest.raises(BadInput, match="P2 is listed twice"):
        pcb.put(
            id="mv-list-dup",
            args={
                "op": "move",
                "moves": [
                    {"refdes": "P2", "x": 330.0, "y": 300.0},
                    {"refdes": "P2", "x": 340.0, "y": 300.0},
                ],
            },
        )
    with pytest.raises(BadInput, match="ARR1_SINK_0 and ARR1 .*share a generator"):
        pcb.put(
            id="mv-list-dup",
            args={
                "op": "move",
                "moves": [
                    {"refdes": "ARR1", "x": 20.0, "y": 15.0},
                    {"refdes": "ARR1_SINK_0", "x": 5.0, "y": 5.0, "rot": 90.0},
                ],
            },
        )
    assert _poses(store, ref.id) == poses0
    assert _fixed_snapshot(store, board_id) == fixed0


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
    # Negative control: no class names "layers", so no layer-lock section.
    assert "layer lock" not in f.body


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


def _set_last_route(store, slug: str, last_route: dict) -> None:
    from psycopg.types.json import Jsonb

    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (Jsonb({"last_route": last_route}), ref.id),
        )
        conn.commit()


def test_congestion_view_gives_no_tick_when_nets_failed(pcb, store):
    """ewod-dogfood-6 printed "(no over-capacity gaps ✓)" under 40 failed."""
    pcb.put(id="cong-failed", args=_CROSSED)
    _set_last_route(store, "cong-failed", {"realized": 15, "failed": 40})
    resp = pcb.get(id="cong-failed", view="congestion")
    assert "✓" not in resp.body
    assert "another cause" in resp.body
    _set_last_route(store, "cong-failed", {"realized": 55, "failed": 0})
    assert "✓" in pcb.get(id="cong-failed", view="congestion").body


def test_congestion_view_flags_a_digest_whose_nets_were_ripped_since(pcb, store):
    """heater-base-test: every net was ripped after a 66-realized route and
    view='congestion' still reported the 66 with no hint the copper was
    gone."""
    pcb.put(id="cong-ripped", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="cong-ripped")
    assert ref is not None
    assert store.pcb_pin_topology(ref.id, "N1", "A.1", "B.1", 1)
    _set_last_route(store, "cong-ripped", {"realized": 2, "failed": 0})
    assert "✓" in pcb.get(id="cong-ripped", view="congestion").body

    pcb.put(id="cong-ripped", args={"op": "rip", "net": "N1"})
    body = pcb.get(id="cong-ripped", view="congestion").body
    assert "STALE — 1 net(s) ripped since that run" in body
    assert "stored now: 0 realized, 2 unrouted" in body
    assert "✓" not in body

    # A fresh route run overwrites the digest, clearing the stale mark.
    _set_last_route(store, "cong-ripped", {"realized": 2, "failed": 0})
    assert "STALE" not in pcb.get(id="cong-ripped", view="congestion").body


def test_rip_leaves_meta_alone_when_no_route_ran(pcb, store):
    pcb.put(id="rip-nometa", args=_CROSSED)
    ref = store.get_ref(kind="pcb", id="rip-nometa")
    assert ref is not None
    assert store.pcb_pin_topology(ref.id, "N1", "A.1", "B.1", 1)
    pcb.put(id="rip-nometa", args={"op": "rip", "net": "N1"})
    after = store.get_ref(kind="pcb", id="rip-nometa")
    assert after is not None
    assert "last_route" not in (after.meta or {})


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


# ── fiducials span the whole stack (all copper layers + both mask films) ──


def test_gerber_bundle_fiducial_flashes_span_inner_and_bottom_copper(pcb, tmp_path):
    """A fiducial is a fab-wide registration mark: on the DEFAULT_STACKUP
    4-layer board (F.Cu/In1.Cu/In2.Cu/B.Cu) the gerber bundle must carry a
    copper flash for it on EVERY layer, not just F.Cu -- an inner layer
    (In1.Cu) and B.Cu each get a ``D03`` flash the same way F.Cu always
    did, or a fab has nothing to register those films against."""
    design: dict[str, Any] = {
        "components": [],
        "nets": [],
        "connections": [],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [60, 0], [60, 40], [0, 40]]},
            },
        ],
    }
    pcb.put(id="fidspan", args=design)
    resp = pcb.get(id="fidspan", view="gerber", args={"dir": str(tmp_path)})
    assert "exported fidspan" in resp.body
    with zipfile.ZipFile(tmp_path / "fidspan-fab.zip") as zf:
        in1_cu = zf.read("fidspan-In1_Cu.gbr").decode("utf-8")
        b_cu = zf.read("fidspan-B_Cu.gbr").decode("utf-8")
    assert "D03*" in in1_cu  # a fiducial flash on an INNER copper layer
    assert "D03*" in b_cu  # ... and on the bottom copper layer


def test_gerber_bundle_b_mask_gains_the_fiducial_opening(pcb, tmp_path):
    """The bottom soldermask film must open over each fiducial too --
    ``soldermask_gerber`` derives a film's openings from ``model["pads"]``
    entries on that side's OUTER copper layer, so a B.Cu fiducial pad
    (the test above) is what makes this true, the same existing mechanism
    the top film has always used, never a second one invented for the
    bottom side."""
    design: dict[str, Any] = {
        "components": [],
        "nets": [],
        "connections": [],
        "features": [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [60, 0], [60, 40], [0, 40]]},
            },
        ],
    }
    pcb.put(id="fidmask", args=design)
    pcb.get(id="fidmask", view="gerber", args={"dir": str(tmp_path)})
    with zipfile.ZipFile(tmp_path / "fidmask-fab.zip") as zf:
        f_mask = zf.read("fidmask-F_Mask.gbr").decode("utf-8")
        b_mask = zf.read("fidmask-B_Mask.gbr").decode("utf-8")
    assert "D03*" in f_mask  # unchanged: the top opening always existed
    assert "D03*" in b_mask  # new: the bottom opening this task adds


# ── `_polarized_refdes` -- the polarity determination behind the R/C/L/FB
# pin-1 policy (precis.pcb.silk's own "Not every part needs one"). The IR
# `build_silk` works from has refdes but no labels, so this has to happen
# here, off the raw design's `instances` list.
def test_polarized_refdes_infers_from_an_electrolytic_label(pcb):
    """The live case: a label carrying "ELEC" (the nano fixture's C1,
    ``"CAP-ELEC-16V-100uF-THT"``) is inferred polarized with no explicit
    flag at all."""
    design = {"instances": [{"refdes": "C1", "label": "CAP-ELEC-16V-100uF-THT"}]}
    assert pcb._polarized_refdes(design) == frozenset({"C1"})


def test_polarized_refdes_a_generic_passive_label_is_not_polarized(pcb):
    """A plain 0603 cap label carries no ELEC/TANT/POL abbreviation --
    not polarized, so its pin-1 mark is subject to the R/C/L/FB policy."""
    design = {"instances": [{"refdes": "C2", "label": "CAP-0603-100nF"}]}
    assert pcb._polarized_refdes(design) == frozenset()


def test_polarized_refdes_honors_an_explicit_flag_even_with_no_matching_label(pcb):
    """``"polarized": true`` on the instance is authoritative on its own,
    independent of what the label says."""
    design = {
        "instances": [
            {"refdes": "L3", "label": "INDUCTOR-SHIELDED-4.7uH", "polarized": True}
        ]
    }
    assert pcb._polarized_refdes(design) == frozenset({"L3"})


def test_polarized_refdes_matches_tant_and_pol_case_insensitively(pcb):
    design = {
        "instances": [
            {"refdes": "C9", "label": "cap-tant-10v-22uf"},
            {"refdes": "C10", "label": "CAP-POL-radial"},
            {"refdes": "R7", "label": "RES-0402"},
        ]
    }
    assert pcb._polarized_refdes(design) == frozenset({"C9", "C10"})


# ── op='move' of a generator member: the group moves rigidly ─────────────
# Reto's ruling 2026-10-02 (docs/backlog/pcb-always-valid-board-invariant.md,
# "Reto's rulings" item 1): a part's own footprint copper always moves with
# it; routed copper on a moved pad is ripped, never kept dangling.


def _gen_board(pcb, slug, *, p1_from_via=None):
    """ARR1 (6x6 + 4 bottom sinks, at the origin: the generator emits its
    fixed copper in the array-local frame, so only x=y=0 is self-consistent)
    plus two plain parts P1/P2 on their own net NX, authored in a second put
    so P1 can be placed relative to the array's own first via
    (``p1_from_via`` = an offset from it; default: far away)."""
    pcb.put(
        id=slug,
        args={
            "generators": [
                {
                    "name": "ARR1",
                    "generator": "ewod_pad_array",
                    "params": {
                        "grid": [6, 6],
                        "sink_grid": {
                            "part": "C639448",
                            "channels_per_sink": 8,
                            "channel_pins": [f"OUT{i}" for i in range(16)],
                            "top_plate_pin": "CPLT",
                            "power": {"VDD": "VCC_HV", "GND": "GND"},
                        },
                    },
                }
            ]
        },
    )
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    board_id = int(pcb.store.pcb_graph(ref.id)["board"]["board_id"])
    p1_xy = (300.0, 300.0)
    pcb.put(
        id=slug,
        args={
            "footprints": [
                {
                    "name": "padp",
                    "pads": [
                        {
                            "pin": "1",
                            "shape": "rect",
                            "x": 0.0,
                            "y": 0.0,
                            "w": 1.0,
                            "h": 1.0,
                        }
                    ],
                }
            ],
            "components": [
                {
                    "refdes": "P1",
                    "label": "p",
                    "footprint": "padp",
                    "x": p1_xy[0],
                    "y": p1_xy[1],
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "P2",
                    "label": "p",
                    "footprint": "padp",
                    "x": 320.0,
                    "y": 300.0,
                    "pins": [{"name": "1"}],
                },
            ],
            "nets": [{"name": "NX", "class": "signal"}],
            "connections": [
                {"net": "NX", "refdes": "P1", "pin": "1"},
                {"net": "NX", "refdes": "P2", "pin": "1"},
            ],
        },
    )
    if p1_from_via is not None:
        # put refuses to create a violation, so the standing one is planted
        # straight in the store.
        via = next(
            r for r in pcb.store.pcb_fixed_copper_list(board_id) if r["ctype"] == "via"
        )
        pcb.store.pcb_set_pose(
            ref.id,
            {"P1": (via["x"] + p1_from_via[0], via["y"] + p1_from_via[1], 0.0)},
        )
    return ref, board_id


def _poses(store, ref_id):
    with store.pool.connection() as conn:
        return {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT refdes, x, y, rot FROM pcb_instances "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        }


def _fixed_snapshot(store, board_id):
    """Every active fixed-copper row as (kind, net, flat rounded coords),
    sorted — order-free, so an UPDATE that reshuffles the heap is fine."""
    out = []
    for r in store.pcb_fixed_copper_list(board_id):
        if r["ctype"] == "via":
            coords = (round(r["x"], 4), round(r["y"], 4))
        else:
            coords = tuple(
                round(v, 4)
                for s in r["segments"]
                for p in (s["start"], s["end"])
                for v in p
            )
        out.append((r["ctype"], r["net"], coords))
    return sorted(out, key=repr)


def _shifted(rows, dx, dy):
    out = []
    for kind, net, coords in rows:
        out.append(
            (
                kind,
                net,
                tuple(
                    round(v + (dx if i % 2 == 0 else dy), 4)
                    for i, v in enumerate(coords)
                ),
            )
        )
    return sorted(out, key=repr)


def test_op_move_generator_member_carries_the_whole_group_and_its_copper(pcb, store):
    ref, board_id = _gen_board(pcb, "mv-grp")
    poses0 = _poses(store, ref.id)
    fixed0 = _fixed_snapshot(store, board_id)
    assert any(k == "via" for k, _, _ in fixed0)  # the control is not vacuous
    assert any(k == "track" for k, _, _ in fixed0)
    sinks = [r for r in poses0 if r.startswith("ARR1_SINK_")]
    assert len(sinks) == 4

    resp = pcb.put(
        id="mv-grp", args={"op": "move", "refdes": "ARR1", "x": 20.0, "y": 15.0}
    )

    for name in ["ARR1", *sinks]:
        assert name in resp.body  # the response names every member that moved
    poses1 = _poses(store, ref.id)
    for name in ["ARR1", *sinks]:
        x0, y0, _ = poses0[name]
        x1, y1, _ = poses1[name]
        assert (x1 - x0, y1 - y0) == pytest.approx((20.0, 15.0), abs=1e-9)
    assert poses1["P1"] == poses0["P1"] and poses1["P2"] == poses0["P2"]
    assert _fixed_snapshot(store, board_id) == _shifted(fixed0, 20.0, 15.0)


def test_op_move_generator_member_rotation_turns_copper_about_the_moved_origin(
    pcb, store
):
    ref, board_id = _gen_board(pcb, "mv-rot")
    vias0 = {
        r["net"]: (r["x"], r["y"])
        for r in store.pcb_fixed_copper_list(board_id)
        if r["ctype"] == "via"
    }
    pcb.put(id="mv-rot", args={"op": "move", "refdes": "ARR1", "rot": 90.0})
    vias1 = {
        r["net"]: (r["x"], r["y"])
        for r in store.pcb_fixed_copper_list(board_id)
        if r["ctype"] == "via"
    }
    assert vias1.keys() == vias0.keys() and vias0
    # Board rot is clockwise: 90 deg takes the offset (dx, dy) to (dy, -dx).
    for net, (x0, y0) in vias0.items():
        dx, dy = x0, y0
        assert vias1[net] == pytest.approx((dy, -dx), abs=1e-6)
    poses = _poses(store, ref.id)
    assert poses["ARR1"][2] == 90.0
    assert all(poses[f"ARR1_SINK_{i}"][2] == 90.0 for i in range(4))


def test_op_move_a_sink_moves_the_array_it_belongs_to(pcb, store):
    ref, _board_id = _gen_board(pcb, "mv-sink")
    before = _poses(store, ref.id)
    pcb.put(
        id="mv-sink",
        args={
            "op": "move",
            "refdes": "ARR1_SINK_0",
            "x": before["ARR1_SINK_0"][0] + 10.0,
            "y": before["ARR1_SINK_0"][1],
        },
    )
    after = _poses(store, ref.id)
    assert after["ARR1"][0] == pytest.approx(before["ARR1"][0] + 10.0)
    assert after["ARR1"][1] == pytest.approx(before["ARR1"][1])


def test_op_move_a_non_generator_part_leaves_fixed_copper_untouched(pcb, store):
    """Negative control: P2 has no generator, so only P2 moves."""
    ref, board_id = _gen_board(pcb, "mv-plain")
    poses0 = _poses(store, ref.id)
    fixed0 = _fixed_snapshot(store, board_id)
    resp = pcb.put(
        id="mv-plain", args={"op": "move", "refdes": "P2", "x": 330.0, "y": 310.0}
    )
    assert "moved" in resp.body and "generator group" not in resp.body
    poses1 = _poses(store, ref.id)
    assert poses1["P2"][:2] == (330.0, 310.0)
    assert {k: v for k, v in poses1.items() if k != "P2"} == {
        k: v for k, v in poses0.items() if k != "P2"
    }
    assert _fixed_snapshot(store, board_id) == fixed0


def test_op_move_group_whose_via_lands_on_another_parts_pad_is_refused(pcb, store):
    # P1's land sits exactly where a carried via will be after a +30 mm move.
    ref, board_id = _gen_board(pcb, "mv-bad", p1_from_via=(30.0, 0.0))
    poses0 = _poses(store, ref.id)
    fixed0 = _fixed_snapshot(store, board_id)
    with pytest.raises(BadInput, match=r"clearance: via\[.*pad\[P1/1"):
        pcb.put(
            id="mv-bad",
            args={"op": "move", "refdes": "ARR1", "x": 30.0, "y": 0.0},
        )
    assert _poses(store, ref.id) == poses0
    assert _fixed_snapshot(store, board_id) == fixed0


def test_op_move_rips_the_nets_on_a_moved_pad_and_keeps_routed_copper_elsewhere(
    pcb, store
):
    ref, board_id = _gen_board(pcb, "mv-rip")
    nets = store.pcb_net_ids(ref.id)
    arr_net = next(
        r["net"] for r in store.pcb_fixed_copper_list(board_id) if r["ctype"] == "via"
    )
    store.pcb_routes_write(
        ref.id,
        board_id,
        {arr_net: {"status": "realized"}, "NX": {"status": "realized"}},
    )
    track = {
        "segments": [{"shape": "line", "start": [400.0, 400.0], "end": [405.0, 400.0]}],
        "width_mm": 0.2,
    }
    store.pcb_copper_replace(
        board_id,
        [
            {"ctype": "track", "layer": "B.Cu", "net_id": nets[arr_net], "geom": track},
            {"ctype": "track", "layer": "B.Cu", "net_id": nets["NX"], "geom": track},
        ],
    )
    assert store.pcb_nets_with_router_copper(board_id) == {arr_net, "NX"}

    resp = pcb.put(
        id="mv-rip", args={"op": "move", "refdes": "ARR1", "x": 20.0, "y": 15.0}
    )

    assert f"ripped 1 net(s): {arr_net}" in resp.body
    assert "re-route with op='route'" in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == {"NX"}
    status = {r["name"]: r["status"] for r in store.pcb_route_status(ref.id)}
    assert status[arr_net] == "unrouted"
    assert status["NX"] == "realized"


def test_op_move_rips_a_router_net_whose_copper_the_moved_group_lands_on(pcb, store):
    """Not on a moved pad, but a carried via now sits on its track: it
    yields (ripped) instead of leaving a short on the board."""
    ref, board_id = _gen_board(pcb, "mv-rip2")
    nets = store.pcb_net_ids(ref.id)
    via = next(r for r in store.pcb_fixed_copper_list(board_id) if r["ctype"] == "via")
    vx, vy = via["x"] + 20.0, via["y"] + 15.0  # where that via lands
    store.pcb_routes_write(ref.id, board_id, {"NX": {"status": "realized"}})
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "B.Cu",
                "net_id": nets["NX"],
                "geom": {
                    "segments": [
                        {
                            "shape": "line",
                            "start": [vx - 2.0, vy],
                            "end": [vx + 2.0, vy],
                        }
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )
    resp = pcb.put(
        id="mv-rip2", args={"op": "move", "refdes": "ARR1", "x": 20.0, "y": 15.0}
    )
    assert "ripped 1 net(s): NX (collides with the moved group" in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == set()


# ── group move: the delta rule keys findings by object identity ──────────


def _vias(store, board_id):
    return [r for r in store.pcb_fixed_copper_list(board_id) if r["ctype"] == "via"]


def _via_geom(via):
    skip = ("ctype", "layer", "net", "fixed", "generator_name", "envelope", "fixed_id")
    return {k: v for k, v in via.items() if k not in skip}


def _plant_via_on(store, ref, board_id, via, net):
    """A second ARR1-owned via of ``net`` exactly on ``via`` — a standing
    violation between two members of the generator group."""
    geom = _via_geom(via)
    store.pcb_fixed_copper_put(
        ref.id,
        board_id,
        "ARR1",
        "ewod_pad_array",
        "t",
        [{"ctype": "via", "layer": via["layer"], "net": net, "geom": geom}],
    )


def test_op_move_group_with_an_internal_standing_violation_moves_and_reports_it(
    pcb, store
):
    ref, board_id = _gen_board(pcb, "mv-int")
    vias = _vias(store, board_id)
    other_net = next(v["net"] for v in vias if v["net"] != vias[0]["net"])
    _plant_via_on(store, ref, board_id, vias[0], other_net)
    poses0 = _poses(store, ref.id)

    resp = pcb.put(
        id="mv-int", args={"op": "move", "refdes": "ARR1", "x": 20.0, "y": 15.0}
    )

    assert _poses(store, ref.id)["ARR1"][:2] == (20.0, 15.0) != poses0["ARR1"][:2]
    assert "board still has" in resp.body
    assert "pre-existing DRC error(s) the move did not add" in resp.body
    assert "clearance: via[" in resp.body


def test_op_move_group_creating_a_second_collision_of_the_same_rule_is_refused(
    pcb, store
):
    """P1's land sits on via V1 (standing). The move takes V1 off it but
    lands a DIFFERENT via on it: same rule, same part, a new pair."""
    ref, board_id = _gen_board(pcb, "mv-rep", p1_from_via=(0.0, 0.0))
    vias = _vias(store, board_id)
    v1 = vias[0]
    v2 = next(v for v in vias if (v["x"], v["y"]) != (v1["x"], v1["y"]))
    poses0 = _poses(store, ref.id)
    with pytest.raises(BadInput, match=r"pad\[P1/1"):
        pcb.put(
            id="mv-rep",
            args={
                "op": "move",
                "refdes": "ARR1",
                "x": v1["x"] - v2["x"],
                "y": v1["y"] - v2["y"],
            },
        )
    assert _poses(store, ref.id) == poses0


def test_op_move_group_deepening_a_standing_overlap_is_refused_shallower_passes(
    pcb, store
):
    """A group via planted 0.6 mm left of P1's centre, far from every
    courtyard, so only the carried-copper rule is in play."""
    ref, board_id = _gen_board(pcb, "mv-deep")
    via = _vias(store, board_id)[0]
    store.pcb_fixed_copper_put(
        ref.id,
        board_id,
        "ARR1",
        "ewod_pad_array",
        "t",
        [
            {
                "ctype": "via",
                "layer": via["layer"],
                "net": via["net"],
                "geom": {**_via_geom(via), "x": 299.4, "y": 300.0},
            }
        ],
    )
    poses0 = _poses(store, ref.id)
    # Closer by 0.2 mm: same via/pad pair, more negative margin.
    with pytest.raises(BadInput, match=r"worse: .* before the move"):
        pcb.put(id="mv-deep", args={"op": "move", "refdes": "ARR1", "x": 0.2, "y": 0.0})
    assert _poses(store, ref.id) == poses0
    # Same pose: equal margin passes, and the standing error is reported.
    resp = pcb.put(
        id="mv-deep", args={"op": "move", "refdes": "ARR1", "x": 0.0, "y": 0.0}
    )
    assert "pre-existing DRC error(s)" in resp.body
    # Away by 0.2 mm: shallower passes.
    pcb.put(id="mv-deep", args={"op": "move", "refdes": "ARR1", "x": -0.2, "y": 0.0})
    assert _poses(store, ref.id)["ARR1"][:2] == (-0.2, 0.0)


def test_route_status_headers_split_dangling_from_routed(pcb, store):
    """A dangling net is stored 'realized' (so route_complete is never
    wedged) but both summaries -- the route-status view header and the
    board's default `## route status:` line -- count it as dangling, not
    routed, and say 'routed' for the stored 'realized'."""
    from precis.pcb import DANGLING_NET_NOTE

    pcb.put(id="sensor-node", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="sensor-node")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    store.pcb_routes_write(
        ref.id,
        board_id,
        {
            "VCC3V3": {"status": "realized"},
            "GND": {"status": "failed"},
            "I2C_SCL": {"status": "realized", "note": DANGLING_NET_NOTE},
        },
    )
    want = (
        "3 net(s): 1 routed, 1 failed, 1 dangling (fewer than 2 pins, nothing to route)"
    )
    status_view = pcb.get(id="sensor-node", view="route-status").body
    assert f"# route status — {want}" in status_view
    # the per-net table keeps the stored status column
    assert "realized (dangling net" in status_view
    board_view = pcb.get(id="sensor-node").body
    assert f"## route status: {want}" in board_view


# ── always-valid board: batch put and class_rules are judged ─────────────
_PADP = {
    "name": "padp",
    "pads": [
        {"pin": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
    ],
}


def _judge_board(pcb, store, slug, *, p2_x=10.0, routed=False):
    """P1 (net A) at the origin and P2 (net B) at ``(p2_x, 0)``, both 1 mm
    pads of an authored footprint, both nets in class ``sig``. ``routed``
    stores a realized router track of net A 0.4 mm clear of P2's pad."""
    pcb.put(
        id=slug,
        args={
            "footprints": [_PADP],
            "components": [
                {
                    "refdes": "P1",
                    "label": "p",
                    "footprint": "padp",
                    "x": 0.0,
                    "y": 0.0,
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "P2",
                    "label": "p",
                    "footprint": "padp",
                    "x": p2_x,
                    "y": 0.0,
                    "pins": [{"name": "1"}],
                },
            ],
            "nets": [{"name": "A", "class": "sig"}, {"name": "B", "class": "sig"}],
            "connections": [
                {"net": "A", "refdes": "P1", "pin": "1"},
                {"net": "B", "refdes": "P2", "pin": "1"},
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    if routed:
        nets = store.pcb_net_ids(ref.id)
        store.pcb_routes_write(ref.id, board_id, {"A": {"status": "realized"}})
        x = p2_x + 0.5 + 0.4 + 0.1  # pad edge + 0.4 mm gap + half the width
        store.pcb_copper_replace(
            board_id,
            [
                {
                    "ctype": "track",
                    "layer": "F.Cu",
                    "net_id": nets["A"],
                    "geom": {
                        "segments": [
                            {"shape": "line", "start": [x, -3.0], "end": [x, 3.0]}
                        ],
                        "width_mm": 0.2,
                    },
                }
            ],
        )
    return ref, board_id


def _class_rules(pcb, slug, **rules):
    return pcb.put(
        id=slug, args={"op": "class_rules", "name": "sig", "rules": dict(rules)}
    )


def _stored_rules(store, ref_id):
    return store.pcb_graph(ref_id)["net_classes"]


def test_class_rules_tightening_rips_the_router_net_it_violates(pcb, store):
    ref, board_id = _judge_board(pcb, store, "jr-rip", routed=True)
    assert store.pcb_nets_with_router_copper(board_id) == {"A"}
    resp = _class_rules(pcb, "jr-rip", clearance_mm=0.8)
    assert "A ripped: clearance after this change" in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == set()
    status = {r["name"]: r["status"] for r in store.pcb_route_status(ref.id)}
    assert status["A"] != "realized"
    assert _stored_rules(store, ref.id)["sig"] == {"clearance_mm": 0.8}


def test_class_rules_tightening_between_two_pads_is_a_margin_not_a_refusal(pcb, store):
    """Pads never yield, but a net class only sets the HOUSE margin: a pad
    pair short of it is a warning (the fab floor is the error), so tightening
    a class over two pads is not refused, but the new shortfall is listed."""
    ref, _ = _judge_board(pcb, store, "jr-pads", p2_x=2.0)
    resp = _class_rules(pcb, "jr-pads", clearance_mm=2.0)
    assert "now visible (class requirement, not refused): clearance" in resp.body
    assert "ripped" not in resp.body
    assert _stored_rules(store, ref.id)["sig"] == {"clearance_mm": 2.0}


def test_batch_put_hole_under_a_placed_part_is_refused(pcb, store):
    ref, _ = _judge_board(pcb, store, "jr-hole")
    with pytest.raises(BadInput, match="courtyard_hole") as exc:
        pcb.put(
            id="jr-hole",
            args={
                "features": [
                    {
                        "ftype": "mounting_hole",
                        "x": 0.0,
                        "y": 0.0,
                        "geom": {"diameter": 3.2},
                    }
                ]
            },
        )
    assert "Nothing was changed" in (exc.value.next or "")
    assert "op':'place'" in (exc.value.next or "")
    assert store.pcb_features_list(ref.id) == []


def test_legal_class_rules_and_batch_put_are_not_refused_or_ripped(pcb, store):
    """Negative control: without it the refusal and the rip could both be
    vacuously always-on."""
    ref, board_id = _judge_board(pcb, store, "jr-ok", routed=True)
    resp = _class_rules(pcb, "jr-ok", clearance_mm=0.2)
    assert "ripped" not in resp.body and "standing" not in resp.body
    resp = pcb.put(id="jr-ok", args={"nets": [{"name": "C", "class": "sig"}]})
    assert "ripped" not in resp.body and "standing" not in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == {"A"}
    assert _stored_rules(store, ref.id)["sig"] == {"clearance_mm": 0.2}
    assert "C" in store.pcb_net_ids(ref.id)


def test_standing_overlap_does_not_refuse_an_unrelated_legal_put(pcb, store):
    ref, _ = _judge_board(pcb, store, "jr-stand")
    store.pcb_set_pose(ref.id, {"P2": (0.3, 0.0, 0.0)})  # planted: overlaps P1
    resp = pcb.put(id="jr-stand", args={"nets": [{"name": "C", "class": "sig"}]})
    assert "standing finding(s) not caused by this change" in resp.body
    n = int(resp.body.split(" standing finding(s)")[0].split()[-1])
    assert n >= 1


def test_judged_tx_reads_its_own_writes_and_rolls_back(pcb, store):
    ref, _ = _judge_board(pcb, store, "jr-tx")
    with pytest.raises(RuntimeError, match="boom"):
        with store.pcb_judged_tx() as conn:
            store.pcb_upsert_net_classes(
                ref.id, {"zz": {"clearance_mm": 0.3}}, conn=conn
            )
            assert "zz" in store.pcb_graph(ref.id)["net_classes"]
            # a plain pool connection (what a read outside the tx opens)
            # cannot see the uncommitted row
            with store.pool.connection() as other:
                (n,) = other.execute(
                    "SELECT count(*) FROM pcb_net_classes WHERE ref_id = %s "
                    "AND name = 'zz'",
                    (ref.id,),
                ).fetchone()
            assert n == 0
            raise RuntimeError("boom")
    assert "zz" not in store.pcb_graph(ref.id)["net_classes"]


def test_unplaced_netlist_is_never_judged(pcb, store):
    """Netlist-first authoring must stay storable: no x/y, no geometry."""
    resp = pcb.put(
        id="jr-unplaced",
        args={
            "components": [
                {"refdes": "U1", "label": "ic", "pins": [{"name": "1"}]},
                {"refdes": "U2", "label": "ic", "pins": [{"name": "1"}]},
            ],
            "nets": [{"name": "N"}],
            "connections": [
                {"net": "N", "refdes": "U1", "pin": "1"},
                {"net": "N", "refdes": "U2", "pin": "1"},
            ],
        },
    )
    assert "created" in resp.body
    assert "standing" not in resp.body and "ripped" not in resp.body


def test_validity_findings_wall_time(pcb, store, capsys):
    import time

    ref, _ = _judge_board(pcb, store, "jr-time", routed=True)
    t0 = time.perf_counter()
    for _ in range(5):
        pcb._validity_findings(ref.id)
    per = (time.perf_counter() - t0) / 5
    with capsys.disabled():
        print(f"\n_validity_findings: {per * 1000:.0f} ms/call (2 parts, 1 track)")


# ── always-valid board: op='footprint' (ruling ewod-pcb-4) ───────────────
_FP_P1, _FP_P2 = "C990001", "C990002"


def _fp_small(**extra):
    return {
        "pads": [{"pin": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}],
        **extra,
    }


def _author_fp(pcb, slug, lcsc, footprint):
    return pcb.put(
        id=slug,
        args={"op": "footprint", "part": lcsc, "footprint": footprint},
    )


def _fp_board(pcb, store, slug, *, routed=False):
    """P1/P2 are catalog parts whose cached footprints are 1 mm pads; P2 sits
    10 mm from P1. ``routed`` stores a router track of net A at x=11, 0.4 mm
    right of P2's pad edge."""
    pcb.put(id="fp-scratch", args={"nets": [{"name": "S"}]})
    for lcsc in (_FP_P1, _FP_P2):
        _author_fp(pcb, "fp-scratch", lcsc, _fp_small())
    comps = [
        {
            "refdes": r,
            "label": "p",
            "part": lcsc,
            "x": x,
            "y": 0.0,
            "pins": [{"name": "1"}],
        }
        for r, lcsc, x in (("P1", _FP_P1, 0.0), ("P2", _FP_P2, 10.0))
    ]
    pcb.put(
        id=slug,
        args={
            "components": comps,
            "nets": [{"name": "A"}, {"name": "B"}],
            "connections": [
                {"net": "A", "refdes": "P1", "pin": "1"},
                {"net": "B", "refdes": "P2", "pin": "1"},
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    if routed:
        nets = store.pcb_net_ids(ref.id)
        store.pcb_routes_write(ref.id, board_id, {"A": {"status": "realized"}})
        store.pcb_copper_replace(
            board_id,
            [
                {
                    "ctype": "track",
                    "layer": "F.Cu",
                    "net_id": nets["A"],
                    "geom": {
                        "segments": [
                            {"shape": "line", "start": [11.0, -3.0], "end": [11.0, 3.0]}
                        ],
                        "width_mm": 0.2,
                    },
                }
            ],
        )
    return ref, board_id


def test_footprint_bigger_pads_rip_router_copper_and_store_the_footprint(pcb, store):
    ref, board_id = _fp_board(pcb, store, "fpj-rip", routed=True)
    assert store.pcb_nets_with_router_copper(board_id) == {"A"}
    resp = _author_fp(
        pcb,
        "fpj-rip",
        _FP_P2,
        {"pads": [{"pin": "1", "shape": "rect", "x": 0, "y": 0, "w": 3.0, "h": 1.0}]},
    )
    assert "A ripped: clearance after this footprint change — re-route" in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == set()
    row = store.part_footprint_get(_FP_P2)
    assert row is not None and row["pads"][0]["w"] == 3.0
    assert "now visible" not in resp.body


def test_footprint_bigger_courtyard_is_not_refused_and_is_listed(pcb, store):
    """The courtyard is the hull of the pads + 0.4 mm, so the new pad sits
    0.4 mm clear of P1's pad (legal) while the courtyards now overlap."""
    _fp_board(pcb, store, "fpj-court")
    resp = _author_fp(
        pcb,
        "fpj-court",
        _FP_P2,
        {"pads": [{"pin": "1", "shape": "rect", "x": -8.6, "y": 0, "w": 1, "h": 1}]},
    )
    assert "now visible (real footprint): courtyard_overlap" in resp.body
    assert "standing until a re-place" in resp.body
    assert "ripped" not in resp.body
    row = store.part_footprint_get(_FP_P2)
    assert row is not None and row["pads"][0]["x"] == -8.6


def test_footprint_that_changes_nothing_illegal_is_silent(pcb, store):
    _, board_id = _fp_board(pcb, store, "fpj-ok", routed=True)
    resp = _author_fp(pcb, "fpj-ok", _FP_P2, _fp_small(note="same pads"))
    assert "ripped" not in resp.body
    assert "now visible" not in resp.body
    assert store.pcb_nets_with_router_copper(board_id) == {"A"}


def test_footprint_fetch_runs_outside_the_transaction(pcb, store, monkeypatch):
    from precis.pcb import footprint as pcb_footprint_mod
    from precis.store._pcb_ops import _AMBIENT_CONN

    _fp_board(pcb, store, "fpj-fetch")
    events: list[str] = []

    def fake_fetch(lcsc):
        events.append(f"fetch:{'in-tx' if _AMBIENT_CONN.get() else 'no-tx'}")
        return {
            "pads": [
                {
                    "number": "1",
                    "shape": "RECT",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 1.0,
                    "h": 1.0,
                    "rot": 0.0,
                    "layer": "F.Cu",
                    "drill": None,
                }
            ],
            "pin_map": {"1": {"name": "1", "tags": []}},
            "courtyard": {"bbox": [-0.5, -0.5, 0.5, 0.5]},
            "centroid": {"x": 0.0, "y": 0.0},
            "source": "easyeda:test",
            "raw": {},
        }

    real_put = store.part_footprint_put

    def spy_put(lcsc, data):
        events.append(f"put:{'in-tx' if _AMBIENT_CONN.get() else 'no-tx'}")
        return real_put(lcsc, data)

    monkeypatch.setattr(pcb_footprint_mod, "_easyeda_fetch", fake_fetch)
    monkeypatch.setattr(store, "part_footprint_put", spy_put)
    pcb.put(id="fpj-fetch", args={"op": "footprint", "part": _FP_P2, "force": True})
    assert events == ["fetch:no-tx", "put:in-tx"]
    row = store.part_footprint_get(_FP_P2)
    assert row is not None and row["source"] == "easyeda:test"


def test_footprint_names_other_designs_using_the_part(pcb, store):
    _fp_board(pcb, store, "fpj-a")
    _fp_board(pcb, store, "fpj-b")
    resp = _author_fp(pcb, "fpj-a", _FP_P2, _fp_small())
    assert (
        f"1 other design(s) use {_FP_P2}: fpj-b — check view='drc' there" in resp.body
    )


# ── always-valid board: outline containment is a delta with real identity ──
_OUTLINE_FEATURE = {
    "ftype": "outline",
    "geom": {"path": [[-5.0, -3.0], [8.0, -3.0], [8.0, 3.0], [-5.0, 3.0]]},
}
_SMALL_PATH = [[-5.0, -5.0], [10.0, -5.0], [10.0, 2.0], [-5.0, 2.0]]
_SMALL_OUTLINE = {"ftype": "outline", "geom": {"path": _SMALL_PATH}}


def _outline_board(pcb, store, slug, *, poses):
    """P1/P2 (1 mm pads) inside an outline of x -5..8, y -3..3; ``poses`` is
    then planted straight into the store (a put refuses to create a violation)."""
    ref, board_id = _judge_board(pcb, store, slug, p2_x=3.0)
    pcb.put(id=slug, args={"features": [_OUTLINE_FEATURE]})
    store.pcb_set_pose(ref.id, {k: (x, y, 0.0) for k, (x, y) in poses.items()})
    return ref, board_id


def _put_poses(pcb, store, slug, **poses):
    """Move parts through the judge (a batch put never re-poses an existing
    part, so the pose write is the mutation handed to ``_judged_mutation``)."""
    ref = store.get_ref(kind="pcb", id=slug)

    def apply(conn):
        for refdes, (x, y) in poses.items():
            store.pcb_move_instance(ref.id, refdes, x=x, y=y, conn=conn)

    return pcb._judged_mutation(ref.id, apply)[1]


def test_outline_shrunk_under_router_track_rips_the_net(pcb, store):
    _, board_id = _judge_board(pcb, store, "oc-rip", p2_x=3.0, routed=True)
    # the track sits at x = 3 + 0.5 + 0.4 + 0.1 = 4.0, y -3..3
    resp = pcb.put(id="oc-rip", args={"features": [_SMALL_OUTLINE]})
    assert "A ripped: " in resp.body  # edge clearance may name it first
    assert store.pcb_nets_with_router_copper(board_id) == set()
    # the outline finding alone carries the router tag (the fixed defect)
    from precis.pcb import drc

    (f,) = drc.check_outline_containment(
        {
            "copper": [
                {
                    "ctype": "track",
                    "layer": "F.Cu",
                    "net": "A",
                    "derived": True,
                    "segments": [
                        {"shape": "line", "start": [4.0, -3.0], "end": [4.0, 3.0]}
                    ],
                    "width_mm": 0.2,
                }
            ]
        },
        outline=_SMALL_PATH,
    )
    assert f.objects[0]["derived"] is True


def test_outline_shrunk_under_authored_copper_is_refused(pcb, store):
    ref, board_id = _judge_board(pcb, store, "oc-fixed", p2_x=3.0)
    store.pcb_fixed_copper_put(
        ref.id,
        board_id,
        "g",
        "g",
        "1",
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "A",
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [4.0, -3.0], "end": [4.0, 3.0]}
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )
    with pytest.raises(BadInput, match="outline_containment"):
        pcb.put(id="oc-fixed", args={"features": [_SMALL_OUTLINE]})


def test_outline_swap_of_which_part_overhangs_is_refused_naming_the_new_one(pcb, store):
    _outline_board(pcb, store, "oc-swap", poses={"P1": (-5.0, 0.0), "P2": (3.0, 0.0)})
    with pytest.raises(BadInput, match=r"outline_containment: part P2"):
        _put_poses(pcb, store, "oc-swap", P1=(0.0, 0.0), P2=(8.0, 0.0))


def test_outline_overhang_deepening_is_refused_as_worse(pcb, store):
    _outline_board(pcb, store, "oc-deep", poses={"P1": (-5.0, 0.0), "P2": (3.0, 0.0)})
    with pytest.raises(BadInput, match=r"outline_containment: part P1.*worse"):
        _put_poses(pcb, store, "oc-deep", P1=(-5.5, 0.0))


def test_outline_overhang_shrinking_is_standing_not_refused(pcb, store):
    _outline_board(pcb, store, "oc-less", poses={"P1": (-5.0, 0.0), "P2": (3.0, 0.0)})
    report = _put_poses(pcb, store, "oc-less", P1=(-4.8, 0.0))
    assert report.standing >= 1 and not report.visible
    assert _xy(store, "oc-less", "P1")[0] == -4.8


def test_outline_partial_overhang_margin_is_a_negative_depth_in_mm():
    from precis.pcb import drc

    model = {
        "pads": [
            {
                "refdes": "U1",
                "pin": "1",
                "net": "N",
                "layer": "F.Cu",
                "shape": "rect",
                "x": 0.0,
                "y": 0.0,
                "w": 2.0,
                "h": 2.0,
            }
        ]
    }
    (f,) = drc.check_outline_containment(
        model, outline=[[-5, -5], [0.5, -5], [0.5, 5], [-5, 5]]
    )
    assert f.margin_mm == pytest.approx(-0.5)
    assert f.objects[0]["refdes"] == "U1" and f.objects[0]["pin"] == "1"


def test_footprint_judge_crash_still_stores_the_footprint(pcb, store, monkeypatch):
    _fp_board(pcb, store, "fpj-crash")

    def boom(_ref_id):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(pcb, "_validity_findings", boom)
    resp = _author_fp(
        pcb,
        "fpj-crash",
        _FP_P2,
        {"pads": [{"pin": "1", "shape": "rect", "x": 0, "y": 0, "w": 3.0, "h": 1.0}]},
    )
    assert (
        "could not judge this design after the footprint change: "
        "RuntimeError: judge exploded" in resp.body
    )
    row = store.part_footprint_get(_FP_P2)
    assert row is not None and row["pads"][0]["w"] == 3.0
