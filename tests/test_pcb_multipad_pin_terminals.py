"""gr339236 — a multi-pad pin's NON-PRIMARY pads are route terminals.

The IR carries one position per pin (the primary pad). gr451276 made a
pin's other pads visible to DRC, connectivity and the router's occupancy
grid, but the maze search still started and ended at the primary pad
only. A pin whose primary pad sits on a layer its net class forbids, with
a drilled sibling pad that lands on the allowed layer, stayed unrouted
though its own copper reaches the allowed layer. The
sibling pad's landing is now an extra terminal, the same seam fixed
copper's island terminals use.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb import realize as pcb_realize
from precis.pcb.ir import from_graph
from precis.pcb.realize import RealizeConfig, realize
from precis.pcb.session import apply_real_pin_offsets

_2LAYER_STACKUP = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "B.Cu", "role": "signal"},
]

#: U1 pin "1" on two pads: an SMD land at the origin (primary, F.Cu only)
#: and a drilled land 2 mm away (spans F.Cu and B.Cu).
_TWO_PAD_FP: dict[str, Any] = {
    "pads": [
        {"number": "1", "shape": "RECT", "x": 0.0, "y": 0.0, "w": 0.6, "h": 0.6},
        {
            "number": "2",
            "shape": "CIRCLE",
            "x": 2.0,
            "y": 0.0,
            "w": 0.8,
            "h": 0.8,
            "drill": 0.4,
        },
    ],
    "pin_map": {"1": {"name": "1"}, "2": {"name": "1"}},
}

_ONE_PAD_FP: dict[str, Any] = {
    "pads": [_TWO_PAD_FP["pads"][0]],
    "pin_map": {"1": {"name": "1"}},
}

_LOCK = {"escape": {"layers": ["B.Cu"]}}


def _graph() -> dict[str, Any]:
    """U1 top-mounted (native F.Cu); U2 bottom-mounted (native B.Cu, no
    footprint). One net ``ESC`` in class ``escape``."""
    return {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 8.0, "y": 0.0, "layer": "bottom"},
        ],
        "nets": [
            {
                "name": "ESC",
                "net_class": "escape",
                "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "U2", "pin": "1"}],
            }
        ],
    }


def _route(
    footprints: dict[str, Any], class_rules: dict[str, Any] | None
) -> pcb_realize.RealizeResult:
    ir = from_graph(_graph(), stackup=_2LAYER_STACKUP)
    apply_real_pin_offsets(ir, footprints)
    return realize(
        ir,
        config=RealizeConfig(router="maze", class_rules=class_rules),
        footprints=footprints,
    )


def test_locked_net_routes_from_a_drilled_sibling_pad() -> None:
    """Failure direction: before the fix this segment stayed unrouted —
    U1's primary pad is F.Cu-only and the search was
    never told its drilled sibling lands on B.Cu."""
    footprints = {"U1": _TWO_PAD_FP}
    result = _route(footprints, _LOCK)
    assert result.unrouted == (), [(r.kind, r.message) for r in result.unrouted_reasons]
    assert result.tracks
    assert {t.layer for t in result.tracks} == {1}, "B.Cu only, as locked"
    assert not result.vias, "the drilled pad is the layer change"
    starts = [t.segments[0]["start"] for t in result.tracks]
    assert any(s == pytest.approx([2.0, 0.0], abs=1e-6) for s in starts), starts
    note = result.island_terminals[0]
    assert "same-pin pad" in note and "B.Cu" in note, note


def test_locked_net_without_the_sibling_pad_stays_unrouted() -> None:
    """Control: the same board with U1's drilled pad removed has no legal
    terminal on B.Cu, so the terminal (not a relaxed lock) is what made
    the test above route."""
    result = _route({"U1": _ONE_PAD_FP}, _LOCK)
    assert result.unrouted == (0,)
    assert not result.tracks


def test_sibling_terminals_carry_pad_layers() -> None:
    """A drilled sibling is a terminal on every signal layer; nothing is
    offered for a single-pad pin."""
    ir = from_graph(_graph(), stackup=_2LAYER_STACKUP)
    apply_real_pin_offsets(ir, {"U1": _TWO_PAD_FP})
    signal = pcb_realize._signal_layers(ir)
    by_pin = pcb_realize._sibling_pad_terminals_by_pin(ir, {"U1": _TWO_PAD_FP}, signal)
    assert list(by_pin) == [("U1", "1")]
    assert [(t.point, t.layer, t.kind, i) for t, i in by_pin[("U1", "1")]] == [
        ((2.0, 0.0), "F.Cu", "pad", 0),
        ((2.0, 0.0), "B.Cu", "pad", 1),
    ]
    assert (
        pcb_realize._sibling_pad_terminals_by_pin(ir, {"U1": _ONE_PAD_FP}, signal) == {}
    )


def test_single_pad_board_routes_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    """A board with no multi-pad pin offers no sibling terminals, so the
    route is the one the router produced before this seam existed."""
    footprints = {"U1": _ONE_PAD_FP}
    with_seam = _route(footprints, None)
    monkeypatch.setattr(
        pcb_realize, "_sibling_pad_terminals_by_pin", lambda *_a, **_k: {}
    )
    without_seam = _route(footprints, None)
    assert with_seam.tracks
    assert with_seam.tracks == without_seam.tracks
    assert with_seam.vias == without_seam.vias
    assert with_seam.island_terminals == without_seam.island_terminals == {}
