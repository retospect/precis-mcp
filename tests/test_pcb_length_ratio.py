"""gr451277 -- per-net routed length vs pad-to-pad spanning-tree bound."""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

import pytest

from precis.pcb.length_ratio import net_lengths


def _track(net: str, *pts: tuple[float, float]) -> dict[str, Any]:
    return {
        "ctype": "track",
        "layer": "F.Cu",
        "net": net,
        "width_mm": 0.15,
        "segments": [
            {"shape": "line", "start": list(a), "end": list(b)}
            for a, b in pairwise(pts)
        ],
    }


def _pad(net: str, x: float, y: float) -> dict[str, Any]:
    return {"net": net, "layer": "F.Cu", "x": x, "y": y}


def test_direct_route_is_ratio_one_and_detour_is_large() -> None:
    pads = [_pad("A", 0, 0), _pad("A", 10, 0), _pad("B", 0, 5), _pad("B", 10, 5)]
    copper = [
        _track("A", (0, 0), (10, 0)),
        # B excurses 20 mm out and back: 20 + 10 + 20 = 50 mm for a 10 mm bound
        _track("B", (0, 5), (0, 25), (10, 25), (10, 5)),
    ]
    got = net_lengths(copper, pads)
    assert got["A"].ratio == pytest.approx(1.0)
    assert got["B"].routed_mm == pytest.approx(50.0)
    assert got["B"].bound_mm == pytest.approx(10.0)
    assert got["B"].ratio == pytest.approx(5.0)


def test_duplicate_pad_positions_and_missing_bound() -> None:
    # A through-hole pad listed on two layers is one terminal; one terminal
    # has no bound, so no ratio is invented.
    pads = [_pad("C", 1, 1), _pad("C", 1, 1)]
    got = net_lengths([_track("C", (1, 1), (4, 1))], pads)
    assert got["C"].bound_mm == 0.0
    assert got["C"].ratio is None


def test_arc_counts_true_length() -> None:
    # Half circle of radius 5 about the origin, (5,0) -> (-5,0) counter-clockwise.
    arc = {
        "ctype": "track",
        "layer": "F.Cu",
        "net": "D",
        "segments": [
            {
                "shape": "arc",
                "start": [5.0, 0.0],
                "end": [-5.0, 0.0],
                "center": [0.0, 0.0],
                "cw": False,
            }
        ],
    }
    got = net_lengths([arc], [_pad("D", 5, 0), _pad("D", -5, 0)])
    assert got["D"].routed_mm == pytest.approx(math.pi * 5)
    assert got["D"].ratio == pytest.approx(math.pi / 2)


def test_pour_net_is_skipped() -> None:
    pour = {"ctype": "pour", "layer": "In1.Cu", "net": "GND"}
    pads = [_pad("GND", 0, 0), _pad("GND", 10, 0)]
    got = net_lengths([_track("GND", (0, 0), (10, 0)), pour], pads)
    assert "GND" not in got


def test_malformed_segment_is_skipped() -> None:
    item = _track("E", (0, 0), (3, 4))
    item["segments"] += [{"shape": "line"}, {"start": None, "end": [1, 1]}]
    got = net_lengths([item], [_pad("E", 0, 0), _pad("E", 3, 4)])
    assert got["E"].routed_mm == pytest.approx(5.0)
