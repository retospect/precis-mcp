"""gr346033, routing level: the maze router's terminal for a bottom-mounted
instance's off-centre pin must be the pad gerber fabricates.

``tests/test_pcb_ir_pin_point_mirror.py`` pins :func:`precis.pcb.ir.
pin_point` against :func:`precis.pcb.padplace.place_pad_point` directly.
This module is the layer above it: run the real router on a two-instance
board where U2 is bottom-mounted with a pin 3 mm off its centre, and check
the copper it draws ends at the exported pad (x = 10 - 3) and never at the
pre-fix mirror image (x = 10 + 3). On prod that 2x offset was 17 mm across
an EWOD ring for every ``HVOUTn`` -- 59 nets ``no_path`` (gr339236).
"""

from __future__ import annotations

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import PcbIR, from_graph
from precis.pcb.padplace import place_pad_point
from precis.pcb.realize import RealizeConfig, pads_for_ir, realize

_U2_X = 10.0
_PIN_DX = 3.0
_CONFIG = RealizeConfig(router="maze")


def _board() -> PcbIR:
    graph = {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": _U2_X, "y": 0.0, "layer": "bottom"},
        ],
        "nets": [
            {
                "name": "N1",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    u2_pins = [
        p for p in range(ir.n_pins) if ir.instance_refdes[ir.pin_instance[p]] == "U2"
    ]
    assert len(u2_pins) == 1
    ir.pin_dx[u2_pins[0]] = _PIN_DX
    ir.pin_dy[u2_pins[0]] = 0.0
    return ir


def _endpoints(result) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for t in result.tracks:
        for seg in t.segments:
            pts.append((float(seg["start"][0]), float(seg["start"][1])))
            pts.append((float(seg["end"][0]), float(seg["end"][1])))
    pts.extend((v.x, v.y) for v in result.vias)
    return pts


def _near(pt: tuple[float, float], x: float, y: float, tol: float) -> bool:
    return abs(pt[0] - x) <= tol and abs(pt[1] - y) <= tol


def test_router_terminal_is_the_exported_pad_not_its_mirror_image():
    ir = _board()
    exported = place_pad_point(
        {"x": _PIN_DX, "y": 0.0},
        {"x": _U2_X, "y": 0.0, "rot": 0.0, "layer": "bottom"},
    )
    assert exported == (_U2_X - _PIN_DX, 0.0)
    # The gerber-side pad list the router must agree with.
    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    u2_pads = [p for p in pads_for_ir(ir, layers) if p["refdes"] == "U2"]
    assert len(u2_pads) == 1
    assert (u2_pads[0]["x"], u2_pads[0]["y"]) == exported

    result = realize(ir, config=_CONFIG)
    assert result.unrouted == (), (
        f"N1 failed: {result.unrouted!r} / {result.unrouted_reasons!r}"
    )
    pts = _endpoints(result)
    tol = _CONFIG.pitch_mm  # grid snap
    assert any(_near(p, exported[0], exported[1], tol) for p in pts), (
        f"no copper reaches the exported pad {exported}; endpoints={pts!r}"
    )
    mirror_x = _U2_X + _PIN_DX
    assert not any(_near(p, mirror_x, 0.0, tol) for p in pts), (
        f"copper drawn to the pre-fix mirror image ({mirror_x}, 0.0); endpoints={pts!r}"
    )
