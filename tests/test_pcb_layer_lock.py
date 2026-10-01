"""precis.pcb.layer_lock — pins a net-class layer lock forbids, split by
whether authored copper bridges them to an allowed layer
(docs/backlog/pcb-pre-routing-estimates-are-green-on-a-board-that-fails.md)."""

from __future__ import annotations

from precis.pcb.connectivity import FixedTerminal
from precis.pcb.layer_lock import layer_locked_pins

_ESCAPE = {"escape": {"layers": ["B.Cu"]}}


def _pad(refdes: str, pin: str, net: str, layer: str = "F.Cu") -> dict[str, str]:
    return {"refdes": refdes, "pin": pin, "net": net, "layer": layer}


def _via(layer: str) -> FixedTerminal:
    return FixedTerminal(point=(0.0, 0.0), layer=layer, kind="via")


def test_forbidden_pad_with_an_authored_via_is_bridged_without_one_stranded():
    pads = [_pad("ARR1", "E1", "esc1"), _pad("ARR1", "E2", "esc2")]
    terminals = {("ARR1", "E1"): (_via("F.Cu"), _via("B.Cu"))}
    report = layer_locked_pins(
        pads, {"esc1": "escape", "esc2": "escape"}, _ESCAPE, terminals
    )
    assert report.bridged == (("esc1", "ARR1", "E1"),)
    assert report.stranded == (("esc2", "ARR1", "E2"),)
    assert report.forced == 2


def test_authored_copper_only_on_the_forbidden_layer_does_not_bridge():
    pads = [_pad("ARR1", "E1", "esc1")]
    terminals = {("ARR1", "E1"): (_via("F.Cu"),)}
    report = layer_locked_pins(pads, {"esc1": "escape"}, _ESCAPE, terminals)
    assert report.stranded == (("esc1", "ARR1", "E1"),)


def test_pad_already_on_an_allowed_layer_is_not_forced():
    pads = [_pad("U1", "3", "esc1", layer="B.Cu")]
    report = layer_locked_pins(pads, {"esc1": "escape"}, _ESCAPE, {})
    assert report.forced == 0


def test_no_locked_class_reports_nothing():
    """Negative control: the term must not fire on every board."""
    pads = [_pad("U1", "1", "sig"), _pad("U1", "2", "")]
    report = layer_locked_pins(
        pads, {"sig": "default"}, {"default": {"track_mm": 0.2}}, {}
    )
    assert report.forced == 0
    assert layer_locked_pins(pads, {"sig": None}, None, {}).forced == 0
