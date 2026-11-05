"""Post-route widening preserves the route's export-quantization reserve."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest

from precis.pcb import drc, gerber
from precis.pcb.capabilities import capability_for
from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.realize import RealizedTrack, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace.realise import widen_power


@pytest.mark.parametrize("target, expected", [(0.200051, 0.15), (0.18, 0.18)])
def test_widening_retains_reserve_through_gerber_apertures(target, expected) -> None:
    """A rounded aperture must not consume the DRC tolerance after widening."""
    capability = capability_for("4layer")
    track = RealizedTrack(
        seg_id=0,
        net_id=1,
        layer=0,
        segments=({"shape": "line", "start": [5.7499355, 4], "end": [5.7499355, 6]},),
        length_mm=2,
        blocked_by=None,
        width_mm=0.15,
    )
    peer = dataclasses.replace(
        track,
        net_id=2,
        segments=({"shape": "line", "start": [20, 4], "end": [20, 6]},),
    )
    original = RealizeResult((track, peer), (), ())
    rules = {
        name: NetRules(track_width_mm=0.15, clearance_mm=0.15, min_clearance_mm=0.15)
        for name in ("N0", "N1", "N2")
    }

    def model_for(result: RealizeResult) -> dict[str, Any]:
        return {
            "layers": ["F.Cu", "B.Cu"],
            "pads": [
                {
                    "refdes": "U1",
                    "pin": "1",
                    "net": "N0",
                    "layer": "F.Cu",
                    "shape": "rect",
                    "x": 5.0,
                    "y": 5.0,
                    "w": 1.0,
                    "h": 1.0,
                }
            ],
            "copper": [
                {
                    "ctype": "track",
                    "net": f"N{t.net_id}",
                    "layer": "F.Cu",
                    "width_mm": t.width_mm,
                    "segments": list(t.segments),
                }
                for t in result.tracks
            ],
        }

    seen: list[tuple[float, ...]] = []

    def findings(result: RealizeResult) -> list[drc.DrcFinding]:
        seen.append(tuple(t.width_mm for t in result.tracks))
        return [
            f
            for f in drc.check_clearance(model_for(result), capability, net_rules=rules)
            if f.severity == "error"
        ]

    accepted, widths = widen_power(
        original, {1}, targets={1: target}, drc_findings=findings
    )
    assert findings(original) == []
    assert findings(accepted) == []
    model = model_for(accepted)
    raw = gerber.copper_gerber(model, "F.Cu")
    apertures = {float(w) for w in re.findall(r"%ADD\d+C,([\d.]+)\*%", raw)}
    emitted_width = round(accepted.tracks[0].width_mm, 4)
    assert emitted_width in apertures
    exported = dataclasses.replace(
        accepted,
        tracks=(dataclasses.replace(accepted.tracks[0], width_mm=emitted_width), peer),
    )
    assert findings(exported) == []
    assert widths[1] == expected
    assert accepted.tracks[0].width_mm == expected
    assert accepted.tracks[1] is peer
    assert original.tracks[0] is track and track.width_mm == 0.15
    assert (target + 2 * FAB_QUANTUM_MM, peer.width_mm) in seen
