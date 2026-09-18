"""gripe gr346033 — :func:`precis.pcb.ir.pin_point` called
:func:`precis.pcb.landpattern.rotate_offset` without ``mirrored=``, while
:func:`precis.pcb.padplace._transform_local_point` (the gerber-export
path) negates a bottom instance's local x BEFORE rotating
(:func:`~precis.pcb.padplace.is_bottom_instance`). So for a bottom-mounted
footprint with an off-centre pin, every IR consumer that reaches a pin
through ``pin_point`` (router/DRC/connectivity, all via
:func:`precis.pcb.realize.pads_for_ir`) computed the MIRROR IMAGE of the
pad gerber actually fabricates. Measured on prod: the bottom-mounted
HV507 sink's ``HVOUTn`` goals landed 17mm across the ring from the real
pad, and every EWOD fabric net failed ``no_path``.

These tests pin ``pin_point`` directly against
:func:`precis.pcb.padplace.place_pad_point` -- the ONE gerber-side
transform (mirror -> rotate -> translate) -- for a bottom instance at
rot=0 (mirror alone), a bottom instance at rot=90 (mirror-before-rotate
order matters once rotation is non-trivial), and a top instance
(unchanged: no mirror should apply there either way).
"""

from __future__ import annotations

import pytest

from precis.pcb import ir as ir_mod
from precis.pcb import padplace

#: One instance, one off-centre pin -- the smallest graph `from_graph`
#: will build a real IR from. `from_graph` synthesizes `pin_dx`/`pin_dy`
#: from the package family (`landpattern.offsets_for`), so the pin is
#: seeded via `unconnected` and its offset is overwritten afterwards to
#: the exact off-centre value gr346033 measured -- `PcbIR` is a plain
#: (non-frozen) dataclass of numpy arrays, so per-element mutation after
#: `from_graph` is the ordinary way tests pin an exact geometry.
_PIN_DX = 8.5
_PIN_DY = -8.4


def _build_ir(*, bottom: bool, rot: float) -> ir_mod.PcbIR:
    graph = {
        "instances": [
            {
                "refdes": "U1",
                "layer": "bottom" if bottom else "top",
                "x": 0.0,
                "y": 0.0,
                "rot": rot,
            },
        ],
        "nets": [],
        "unconnected": [{"refdes": "U1", "pin": "P"}],
    }
    ir = ir_mod.from_graph(graph)
    ir.pin_dx[0] = _PIN_DX
    ir.pin_dy[0] = _PIN_DY
    return ir


def _expected(*, bottom: bool, rot: float) -> tuple[float, float]:
    pad = {"x": _PIN_DX, "y": _PIN_DY}
    inst = {"x": 0.0, "y": 0.0, "rot": rot, "layer": "bottom" if bottom else "top"}
    return padplace.place_pad_point(pad, inst)


def test_bottom_instance_pin_point_mirrors_like_gerber_export_at_rot_zero():
    ir = _build_ir(bottom=True, rot=0.0)
    got = ir_mod.pin_point(ir, 0)
    assert got is not None
    assert got == pytest.approx(_expected(bottom=True, rot=0.0))
    # Concrete regression value (gr346033): mirrored, unrotated.
    assert got == pytest.approx((-8.5, -8.4))


def test_bottom_instance_pin_point_mirrors_before_rotating_at_rot_90():
    ir = _build_ir(bottom=True, rot=90.0)
    got = ir_mod.pin_point(ir, 0)
    assert got is not None
    assert got == pytest.approx(_expected(bottom=True, rot=90.0))
    # Mirror-before-rotate, not rotate-before-mirror -- the two orders
    # disagree once rotation is non-trivial, which is exactly why this
    # case (not just rot=0) is required to pin the order gr346033 broke.
    assert got == pytest.approx((-8.4, 8.5))


def test_top_instance_pin_point_is_unchanged_by_the_mirror_fix():
    ir = _build_ir(bottom=False, rot=90.0)
    got = ir_mod.pin_point(ir, 0)
    assert got is not None
    assert got == pytest.approx(_expected(bottom=False, rot=90.0))
