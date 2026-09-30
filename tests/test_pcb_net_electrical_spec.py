"""Per-net electrical spec — `docs/backlog/pcb-missing-constraint-classes.md`
§E-1 and the datasheet ``NetAnnotation`` half of the same survey.

Migration 0171 gave ``pcb_nets`` a working voltage, an edge rate, an
impedance and a function hint. Two consumers that already existed and were
wired to nothing now read them:

* ``drc.check_clearance`` folds IPC-2221B Table 6-1 spacing for
  ``|V_a - V_b|`` into the same ``max`` as the per-net clearance floors —
  PAIRWISE, because a 20 V net beside another 20 V net needs nothing
  special and the same net beside ground needs the full spacing;
* ``cost._annotation`` resolves the stored annotation instead of always
  returning ``annotation_for(None)``.

The load-bearing negative in both halves: a board that annotates nothing
must behave EXACTLY as it did before 0171.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import cost as pcb_cost
from precis.pcb import drc as pcb_drc
from precis.pcb import objectives as obj
from precis.pcb.capabilities import capability_for, conductor_spacing_mm

# Two parallel tracks on F.Cu, 0.4 mm apart edge-to-edge: comfortably clear
# of JLC's 4-layer spacing floor, and comfortably INSIDE IPC-2221B's
# requirement for a 48 V difference on an external layer. So the same board
# is clean unannotated and dirty annotated — which is the whole point.
_GAP_MM = 0.4
_WIDTH_MM = 0.2


def _two_track_model(*, layers: list[str] | None = None) -> dict[str, Any]:
    centre_to_centre = _GAP_MM + _WIDTH_MM
    return {
        "layers": layers or ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        "outline": [[-5.0, -5.0], [15.0, -5.0], [15.0, 5.0], [-5.0, 5.0]],
        "copper": [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "HV",
                "width_mm": _WIDTH_MM,
                "segments": [
                    {"shape": "line", "start": [0.0, 0.0], "end": [10.0, 0.0]}
                ],
            },
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "GND",
                "width_mm": _WIDTH_MM,
                "segments": [
                    {
                        "shape": "line",
                        "start": [0.0, centre_to_centre],
                        "end": [10.0, centre_to_centre],
                    }
                ],
            },
        ],
        "pads": [],
        "drills": [],
        "silkscreen": {"top": [], "bottom": []},
    }


@pytest.fixture
def capability():
    return capability_for("4layer")


# ── the pairwise voltage term ────────────────────────────────────────────
def test_unannotated_board_is_unchanged_by_the_voltage_term(capability):
    """The load-bearing negative: no annotations => byte-identical findings
    to the pre-0171 call, whether the map is absent or empty."""
    model = _two_track_model()
    before = pcb_drc.check_clearance(model, capability)
    empties: tuple[dict[str, float] | None, ...] = (None, {})
    for empty in empties:
        after = pcb_drc.check_clearance(model, capability, net_voltages=empty)
        assert [f.to_row() for f in after] == [f.to_row() for f in before]
    assert [f.rule for f in before] == []


def test_voltage_difference_makes_a_clean_gap_a_finding(capability):
    """48 V beside 0 V at a gap the fab floor alone passes."""
    model = _two_track_model()
    findings = pcb_drc.check_clearance(
        model, capability, net_voltages={"HV": 48.0, "GND": 0.0}
    )
    clearance = [f for f in findings if f.rule == "clearance"]
    assert len(clearance) == 1
    assert clearance[0].margin_mm is not None
    assert clearance[0].margin_mm < 0
    # The threshold that fired is the table's, not the fab floor's.
    required = conductor_spacing_mm(48.0, layer="external", coated=False)
    assert required > _GAP_MM
    assert f"{required:.3f}" in clearance[0].detail or "0.4" in clearance[0].detail


def test_equal_voltages_demand_nothing_extra(capability):
    """The pairwise point: two nets at the SAME potential are not a
    spacing problem, however high that potential is."""
    findings = pcb_drc.check_clearance(
        _two_track_model(), capability, net_voltages={"HV": 48.0, "GND": 48.0}
    )
    assert [f for f in findings if f.rule == "clearance"] == []


def test_zero_volts_is_an_annotation_not_a_missing_one(capability):
    """``working_voltage_v: 0`` (a net tied to chassis) must reach the
    pair computation as a real 0, not read as unannotated — otherwise the
    one pair the rule exists for is exactly the one it skips."""
    findings = pcb_drc.check_clearance(
        _two_track_model(), capability, net_voltages={"HV": 48.0, "GND": 0.0}
    )
    assert [f.rule for f in findings if f.rule == "voltage_spacing_unknown"] == []
    assert any(f.rule == "clearance" for f in findings)


def test_half_annotated_pair_is_reported_not_guessed(capability):
    """A missing annotation is not 0 V. The pair falls back to the per-net
    floors AND the run says so — silence would be the dangerous way to be
    wrong here."""
    findings = pcb_drc.check_clearance(
        _two_track_model(), capability, net_voltages={"HV": 48.0}
    )
    assert [f for f in findings if f.rule == "clearance"] == []
    unknown = [f for f in findings if f.rule == "voltage_spacing_unknown"]
    assert len(unknown) == 1
    assert "GND" in unknown[0].detail
    assert unknown[0].severity == "warn"


def test_voltage_past_the_table_is_reported_not_raised(capability):
    """``conductor_spacing_mm`` refuses above its 500 V top band rather
    than extrapolating. Inside DRC that refusal must become a finding, not
    a ValueError that takes the whole board report down."""
    findings = pcb_drc.check_clearance(
        _two_track_model(), capability, net_voltages={"HV": 900.0, "GND": 0.0}
    )
    out_of_table = [f for f in findings if f.rule == "voltage_spacing_out_of_table"]
    assert len(out_of_table) == 1
    assert "HV" in out_of_table[0].detail


def test_inner_layer_pair_reads_the_looser_internal_column(capability):
    """IPC-2221B's B1 (internal) column is looser than B2 (external
    uncoated) — inner copper is never exposed. The SAME geometry and the
    same 48 V difference must therefore fire on F.Cu and stay clean on an
    inner layer; a single-column implementation would fire on both."""
    external = conductor_spacing_mm(48.0, layer="external", coated=False)
    internal = conductor_spacing_mm(48.0, layer="internal", coated=False)
    assert internal < _GAP_MM < external

    inner = _two_track_model()
    for item in inner["copper"]:
        item["layer"] = "In1.Cu"
    voltages = {"HV": 48.0, "GND": 0.0}
    assert [
        f
        for f in pcb_drc.check_clearance(inner, capability, net_voltages=voltages)
        if f.rule == "clearance"
    ] == []
    assert [
        f
        for f in pcb_drc.check_clearance(
            _two_track_model(), capability, net_voltages=voltages
        )
        if f.rule == "clearance"
    ]


# ── the stored NetAnnotation ─────────────────────────────────────────────
def _ir_with_nets(**arrays):
    """A stand-in carrying only the three annotation arrays `_annotation`
    reads — it indexes them and nothing else."""

    class _Stub:
        net_impedance_ohm: Any
        net_edge_rate_v_per_ns: Any
        net_function_hint: Any

    stub = _Stub()
    stub.net_impedance_ohm = arrays.get("impedance", np.array([np.nan]))
    stub.net_edge_rate_v_per_ns = arrays.get("edge_rate", np.array([np.nan]))
    hints = arrays.get("hints", [""])
    arr = np.empty(len(hints), dtype=object)
    for i, h in enumerate(hints):
        arr[i] = h
    stub.net_function_hint = arr
    return stub


def test_unannotated_net_keeps_the_conservative_unknown_default():
    got = pcb_cost._annotation(0, _ir_with_nets(), pcb_cost.CostConfig())
    assert got == obj.annotation_for(None)


def test_function_hint_selects_the_fallback_library_entry():
    got = pcb_cost._annotation(
        0, _ir_with_nets(hints=["crystal"]), pcb_cost.CostConfig()
    )
    assert got == obj.annotation_for("crystal")
    assert got != obj.annotation_for(None)


def test_stored_columns_beat_the_hint_field_by_field():
    """A partial annotation must not invent the half it does not carry:
    the authored impedance wins, the missing edge rate falls back to the
    hint's, never to zero."""
    ir = _ir_with_nets(impedance=np.array([50.0]), hints=["switcher_sw"])
    got = pcb_cost._annotation(0, ir, pcb_cost.CostConfig())
    assert got.impedance_ohm == 50.0
    assert (
        got.edge_rate_v_per_ns == obj.annotation_for("switcher_sw").edge_rate_v_per_ns
    )


def test_explicit_config_override_still_wins():
    override = obj.annotation_for("adc_input")
    config = pcb_cost.CostConfig(net_annotations={0: override})
    ir = _ir_with_nets(impedance=np.array([50.0]), hints=["switcher_sw"])
    assert pcb_cost._annotation(0, ir, config) is override


def test_ir_without_the_arrays_still_resolves():
    """A hand-built PcbIR predating 0171 defaults these arrays empty; the
    reader size-checks rather than indexing off the end."""
    ir = _ir_with_nets(impedance=np.zeros(0), edge_rate=np.zeros(0), hints=[])
    assert pcb_cost._annotation(0, ir, pcb_cost.CostConfig()) == obj.annotation_for(
        None
    )


# ── the store round trip ─────────────────────────────────────────────────
_DESIGN = {
    "components": [
        {"refdes": "U1", "label": "driver", "pins": [{"name": "OUT"}, {"name": "GND"}]}
    ],
    "nets": [
        {"name": "HV", "voltage": 48.0, "function": "switcher_sw"},
        {"name": "GND", "working_voltage_v": 0.0},
    ],
    "connections": [
        {"net": "HV", "refdes": "U1", "pin": "OUT"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
    ],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _nets_by_name(pcb, slug):
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    return {n["name"]: n for n in pcb.store.pcb_graph(ref.id)["nets"]}


def test_spec_fields_round_trip_through_put(pcb):
    pcb.put(id="spec-1", args=_DESIGN)
    nets = _nets_by_name(pcb, "spec-1")
    assert nets["HV"]["working_voltage_v"] == 48.0
    assert nets["HV"]["function_hint"] == "switcher_sw"
    # 0 V is stored as 0, not collapsed to NULL by an `or` chain.
    assert nets["GND"]["working_voltage_v"] == 0.0


def test_reput_patches_spec_onto_an_existing_net(pcb):
    """The workflow this exists for: the board is authored (or imported)
    first, and only then does someone read the datasheet."""
    pcb.put(id="spec-2", args=_DESIGN)
    pcb.put(id="spec-2", args={"nets": [{"name": "HV", "impedance": 50.0}]})
    hv = _nets_by_name(pcb, "spec-2")["HV"]
    assert hv["impedance_ohm"] == 50.0
    # Presence-based: the patch named only the impedance, so the voltage and
    # the hint set at authoring time survive untouched.
    assert hv["working_voltage_v"] == 48.0
    assert hv["function_hint"] == "switcher_sw"


def test_reput_without_spec_fields_changes_nothing(pcb):
    pcb.put(id="spec-3", args=_DESIGN)
    before = _nets_by_name(pcb, "spec-3")["HV"]
    pcb.put(id="spec-3", args={"nets": [{"name": "HV", "note": "ignored"}]})
    assert _nets_by_name(pcb, "spec-3")["HV"] == before
