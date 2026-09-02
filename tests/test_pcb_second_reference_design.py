"""The motor/power-board reference design — the SECOND reference vehicle.

``docs/backlog/pcb-engine-plan.md`` §"Obligations to the paper" item 3
records a competing explanation for the ESP32-C3 board's defect density
(``tests/fixtures/pcb/esp32c3_reference.json``): "the first run against
**the benchmark**, not the first run against any board, is what drives
it." That is only testable with a SECOND, structurally different
reference design, and nobody had built one before this fixture.

``motor_power_reference.json`` is a 21-part power/motor-driver board —
deliberately different from the MCU/sensor board along the axes it does
not exercise: a buck regulator + H-bridge motor driver with high-current
nets (``est_current_a`` up to 3.5A, forcing IPC-2221 wide traces and
ampacity via GROUPS — board one's via groups are all size 1, so that path
never fires there), through-hole parts (two screw terminals, two
electrolytic caps, a TO-220, an axial diode, a header, a test point —
board one is all-SMD), one authored bottom-side part (C3, ``"layer":
"bottom"``), two mounting-hole features, and a net-degree profile
concentrated onto three power/ground trunks (GND=12, VIN=6, VM=6 of 51
connections) rather than spread across many small nets. The outline is a
realistically-sized 70x50mm, not board one's 300x300mm.

These checks are structural and DB-free, mirroring
``test_pcb_reference_design.py``'s discipline exactly: they pin that the
fixture is a valid, self-consistent netlist, never a routing or DRC
outcome (that is what ``test_pcb_second_reference_end_to_end.py``
measures).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "motor_power_reference.json"


@pytest.fixture(scope="module")
def design() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = json.load(fh)
    return loaded


def test_outline_is_realistically_sized(design: dict[str, Any]) -> None:
    """70x50mm, not board one's 300x300mm-for-44mm-of-parts.

    Board one's plan-file postscript names its own oversized outline as "a
    known sensitivity gap" (§"the acceptance fixture's board is 300x300mm
    and its parts occupy 44mm"). This board does not reproduce it.
    """
    outline = next(f for f in design["features"] if f["ftype"] == "outline")
    xs = [pt[0] for pt in outline["geom"]["path"]]
    ys = [pt[1] for pt in outline["geom"]["path"]]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    assert 40 <= width <= 120
    assert 30 <= height <= 100


def test_fixture_matches_a_small_realistic_power_board(design: dict[str, Any]) -> None:
    """~15-25 components, per the task's own sizing target."""
    assert 15 <= len(design["components"]) <= 25
    assert 10 <= len(design["nets"]) <= 20
    assert len(design["connections"]) >= 40
    assert design["features"], "the outline feature is required"


def test_every_connection_resolves(design: dict[str, Any]) -> None:
    """No connection may name a refdes, pin, or net that does not exist."""
    pins_by_refdes = {
        c["refdes"]: {p["name"] for p in c["pins"]} for c in design["components"]
    }
    net_names = {n["name"] for n in design["nets"]}

    for conn in design["connections"]:
        refdes, pin, net = conn["refdes"], conn["pin"], conn["net"]
        assert refdes in pins_by_refdes, f"connection names unknown refdes {refdes!r}"
        assert pin in pins_by_refdes[refdes], f"{refdes} has no pin {pin!r}"
        assert net in net_names, f"connection names unknown net {net!r}"


def test_power_nets_carry_current_annotations(design: dict[str, Any]) -> None:
    """VIN/GND/VM/OUT_A/OUT_B must declare ``current`` -- without it the
    IPC-2221 width and via-ampacity machinery never fires on this board
    either, same as board one's own guard."""
    annotated = {n["name"] for n in design["nets"] if n.get("current") is not None}
    for rail in ("VIN", "GND", "VM", "OUT_A", "OUT_B"):
        assert rail in annotated, f"{rail} carries no current= annotation"


def test_has_through_hole_parts_and_no_half_supported_bottom_parts(
    design: dict[str, Any],
) -> None:
    """The axis board one never exercises at all: THT labels. This board
    deliberately carries NO bottom-side instance: ``"layer": "bottom"``
    is today only half-honoured (silk mirrors to the bottom film while
    pads/mask/paste/routing all still emit top-side), so an authored
    bottom part is a silk-only lie in the gerbers. C3 was flipped back
    to top in review round 4; full bottom-side support is
    docs/backlog/pcb-review-round4-0901.md item 10, and this assertion
    flips back with it."""
    labels = {c["refdes"]: c["label"] for c in design["components"]}
    tht_refdes = {
        r
        for r, label in labels.items()
        if "THT" in label or "SCREW-TERM" in label or "TO-220" in label
    }
    assert len(tht_refdes) >= 5, f"expected >=5 THT parts, got {tht_refdes}"

    bottom = [c for c in design["components"] if c.get("layer") == "bottom"]
    assert bottom == []


def test_has_a_current_annotation_large_enough_to_force_a_multi_via_group(
    design: dict[str, Any],
) -> None:
    """VM at 3.5A must exceed a single via's conservative ampacity
    (:data:`precis.pcb.rules.VIA_REFERENCE_CAPACITY_A` at
    :data:`precis.pcb.rules.VIA_REFERENCE_DIA_MM`) so
    ``via_count_for_current`` resolves >1 -- the exact condition the plan
    file names as never firing on board one (every via group there is
    size 1)."""
    from precis.pcb.rules import via_count_for_current

    vm_current = next(n["current"] for n in design["nets"] if n["name"] == "VM")
    # A conservative small via (fab floor, ~0.45-0.6mm typical) must need
    # more than one stitched via to carry this net.
    assert via_count_for_current(vm_current, 0.5) > 1


def test_net_degree_profile_concentrates_on_three_power_trunks(
    design: dict[str, Any],
) -> None:
    """Board one spreads fanout across many nets around one MCU hub; this
    board concentrates it onto GND/VIN/VM. Different net-topology shape,
    the axis the task calls for."""
    fanout: dict[str, int] = {}
    for conn in design["connections"]:
        fanout[conn["net"]] = fanout.get(conn["net"], 0) + 1

    assert fanout.get("GND", 0) >= 10
    trunk_total = fanout.get("GND", 0) + fanout.get("VIN", 0) + fanout.get("VM", 0)
    assert trunk_total / len(design["connections"]) >= 0.4


def test_has_a_dangling_test_point_net(design: dict[str, Any]) -> None:
    """One legitimately <2-member net (VM_TP), same pattern board one uses
    for its own test points -- exercises the dangling-net exemption
    without inflating the routed-count denominator dishonestly."""
    fanout: dict[str, int] = {}
    for conn in design["connections"]:
        fanout[conn["net"]] = fanout.get(conn["net"], 0) + 1
    dangling = {name for name, n in fanout.items() if n < 2}
    assert dangling == {"VM_TP"}
