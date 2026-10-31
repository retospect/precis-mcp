"""se flagellar motor — slice 1 of docs/backlog/se-flagellar-motor.md.

A rotary motor is two blocks (stator, rotor) ``connect``ed port-to-port
with ``joint={"class": "revolute", ...}`` — existing grammar, nothing
new. The motor's physics is a set of ``add_measure`` rows on the stator
block naming new measurands: ``stator-count``, ``proton-motive-force``,
``stall-torque``, ``zero-load-speed``, ``operating-speed`` (inputs) and
``output-torque`` (the one realised value, through
:data:`precis_se.properties.COMPUTERS`).

The model is the linear torque-speed curve
``torque(w) = stall_torque * (1 - w / zero_load_speed)``
(:func:`torque_at_speed`). ``stator-count`` and ``proton-motive-force``
are recorded here but not read yet — later slices (non-linear knee,
Na+-motive force, CheY-P switching) consume them; declaring the six
measurands now keeps the measure schema stable across those slices.
"""

from __future__ import annotations

from typing import Any

from precis_se.properties.computers import _Computer


def torque_at_speed(stall_torque: float, zero_load_speed: float, speed: float) -> float:
    """The linear torque-speed curve: ``stall_torque`` at ``speed == 0``,
    ``0`` at ``speed == zero_load_speed``."""
    return stall_torque * (1.0 - speed / zero_load_speed)


def _block_measure(tree: Any, block: str, measurand: str) -> float | None:
    """The declared value of the first measure on ``block`` carrying
    ``measurand`` — ``None`` when no such measure is declared."""
    for m in tree.measures:
        if m.block == block and m.measurand == measurand:
            return m.value
    return None


def _output_torque(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    block = measure.block
    stall = _block_measure(tree, block, "stall-torque")
    if stall is None:
        return None, f"block {block!r} has no declared stall-torque measure"
    zero_load = _block_measure(tree, block, "zero-load-speed")
    if zero_load is None:
        return None, f"block {block!r} has no declared zero-load-speed measure"
    speed = _block_measure(tree, block, "operating-speed")
    if speed is None:
        return None, f"block {block!r} has no declared operating-speed measure"
    if zero_load == 0:
        return None, "zero-load-speed is 0 — the linear torque-speed curve is undefined"
    return torque_at_speed(stall, zero_load, speed), None


#: measurand slug -> computer, registered in
#: :data:`precis_se.properties.COMPUTERS` (se-flagellar-motor.md slice 1).
MOTOR_COMPUTERS: dict[str, Any] = {
    "output-torque": _Computer(_output_torque),
}
