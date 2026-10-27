"""Netlist motifs recognize declared pin classes and report the broken edge.

gr521601: the wrong buck rail is still a connected, valid net; connectivity
alone cannot catch it. These checks need no footprint, placement or server.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from precis_geom.pcb import Board, CheckFinding, Max
from precis_geom.pcb_motifs import MOTIFS
from tests.workspaces._script import load_module

_BOARDS = Path(__file__).resolve().parents[2] / "examples" / "boards"


def _nano() -> Board:
    return load_module(_BOARDS / "nano_usb_pd" / "main.py", "motif_nano").build()


def _motif_findings(board: Board) -> list[CheckFinding]:
    return [
        finding
        for finding in board.check().findings
        if finding.code.startswith("motif.") or finding.code == "led_without_limit"
    ]


def _move_pin(board: Board, refdes: str, pin: str, net: str) -> None:
    # Deliberately mutate one connection in a freshly built example, keeping
    # its original source site and every other topology fact intact.
    (connection,) = [
        row
        for row in board._connections
        if row["refdes"] == refdes and row["pin"] == pin
    ]
    connection["net"] = net


def _ldo(*, output_cap: bool, classes: str = "functions") -> Board:
    board = Board("ldo-motif")
    vin = board.net("INPUT", cls="power", voltage=5.0)
    vout = board.net("OUTPUT", cls="power", voltage=3.3)
    gnd = board.net("GROUND", cls="ground", voltage=0.0)
    names = ("VIN", "VOUT", "GND") if classes == "labels" else ("1", "2", "3")
    regulator = board.part("U1", roles=["ldo"] if classes == "etypes" else [])
    if classes == "functions":
        regulator.pin_functions({"1": ["vin"], "2": ["vout"], "3": ["gnd"]})
    elif classes == "etypes":
        regulator[names[0]].etype("power_in")
        regulator[names[1]].etype("power_out")
        regulator.pin_functions({names[2]: ["GND"]})
    cin = board.part("X_IN", roles=["capacitor"])
    vin.tie(regulator[names[0]], cin["1"])
    vout.tie(regulator[names[1]])
    gnd.tie(regulator[names[2]], cin["2"])
    if output_cap:
        cout = board.part("X_OUT", roles=["capacitor"])
        vout.tie(cout["1"])
        gnd.tie(cout["2"])
    return board


def test_nano_buck_motifs_are_eligible_and_clean() -> None:
    board = _nano()
    buck = next(motif for motif in MOTIFS if motif.name == "buck")
    assert all(
        buck.eligibility(board._parts[name]) for name in ("U_B15", "U_B9", "U_B5")
    )
    assert _motif_findings(board) == []


@pytest.mark.parametrize("wrong_net", ["V9", "VBUS", "GND"])
def test_buck_inductor_wrong_output_rail_names_the_moved_pin(wrong_net: str) -> None:
    board = _nano()
    _move_pin(board, "L15", "2", wrong_net)

    (finding,) = _motif_findings(board)
    assert finding.code == "motif.buck"
    assert (finding.refdes, finding.pin, finding.net) == ("L15", "2", wrong_net)
    assert "L15.2" in finding.message and wrong_net in finding.message
    assert finding.not_checked is False
    assert finding.site and "main.py:" in finding.site


def test_buck_inductor_must_reach_switch_pin() -> None:
    board = _nano()
    _move_pin(board, "L15", "1", "VBUS")

    (finding,) = _motif_findings(board)
    assert finding.code == "motif.buck"
    assert finding.pin is not None and finding.net is not None
    assert finding.not_checked is False


def test_buck_feedback_must_reach_ground_through_resistor() -> None:
    board = _nano()
    _move_pin(board, "R_FBB15", "2", "VBUS")

    (finding,) = _motif_findings(board)
    assert finding.code == "motif.buck"
    assert (finding.refdes, finding.pin, finding.net) == ("U_B15", "FB", "FB15")
    assert "FB" in finding.message and "FB15" in finding.message


@pytest.mark.parametrize("classes", ["functions", "labels", "etypes"])
def test_ldo_missing_output_cap_names_output_pin(classes: str) -> None:
    board = _ldo(output_cap=False, classes=classes)

    (finding,) = _motif_findings(board)
    pin = "VOUT" if classes == "labels" else "2"
    assert finding.code == "motif.ldo"
    assert (finding.refdes, finding.pin, finding.net) == ("U1", pin, "OUTPUT")
    assert f"U1.{pin}" in finding.message and "OUTPUT" in finding.message
    assert finding.not_checked is False


@pytest.mark.parametrize("classes", ["functions", "labels", "etypes"])
def test_ldo_input_and_output_caps_to_ground_pass(classes: str) -> None:
    board = _ldo(output_cap=True, classes=classes)
    ldo = next(motif for motif in MOTIFS if motif.name == "ldo")
    assert ldo.eligibility(board._parts["U1"])
    assert _motif_findings(board) == []


def test_ldo_capacitor_to_input_is_not_output_decoupling() -> None:
    board = _ldo(output_cap=True)
    _move_pin(board, "X_OUT", "2", "INPUT")

    (finding,) = _motif_findings(board)
    assert finding.code == "motif.ldo"
    assert (finding.refdes, finding.pin, finding.net) == ("U1", "2", "OUTPUT")


def test_unclassified_numeric_pins_are_not_eligible() -> None:
    board = _ldo(output_cap=False, classes="unclassified")
    part = board._parts["U1"]

    assert all(not motif.eligibility(part) for motif in MOTIFS)
    assert _motif_findings(board) == []


def test_declared_functions_override_semantic_pin_label() -> None:
    board = _ldo(output_cap=False, classes="labels")
    part = board._parts["U1"]
    part.pin_functions({"VOUT": ["GPIO"]})

    ldo = next(motif for motif in MOTIFS if motif.name == "ldo")
    assert not ldo.eligibility(part)
    assert _motif_findings(board) == []


def test_declared_functions_override_ldo_etype_fallback() -> None:
    board = _ldo(output_cap=False, classes="etypes")
    part = board._parts["U1"]
    part.pin_functions({"2": ["GPIO"]})

    ldo = next(motif for motif in MOTIFS if motif.name == "ldo")
    assert not ldo.eligibility(part)
    assert _motif_findings(board) == []


def test_led_without_limit_keeps_exact_finding_shape() -> None:
    board = Board("led-motif")
    driver, led = board.part("U1"), board.part("D1")
    led.pin_functions({"A": ["LED"]})
    board.net("LED_N").tie(driver["PA1"], led["A"], current=Max("20mA"))
    board.net("GND").tie(led["K"])

    (finding,) = board.check().findings
    assert asdict(finding) == {
        "code": "led_without_limit",
        "message": "D1.A (LED) sits on a current path with no series current-limit part",
        "refdes": "D1",
        "net": "LED_N",
        "pin": "A",
        "site": led["A"].site,
        "not_checked": False,
    }


def test_led_with_series_limiter_has_no_motif_finding() -> None:
    board = Board("limited-led-motif")
    driver, resistor, led = board.part("U1"), board.part("R1"), board.part("D1")
    led.pin_functions({"A": ["LED"]})
    resistor["1"].current_role("series")
    board.net("DRIVE").tie(driver["PA1"], resistor["1"], current=Max("20mA"))
    board.net("LED_N").tie(resistor["2"], led["A"])
    board.net("GND").tie(led["K"])

    assert _motif_findings(board) == []
