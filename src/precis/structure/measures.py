"""Evaluate persisted eyes + measures against the live Scene (ADR 0043 §6.8/§7).

A :class:`~precis.structure.scene.Measure` is *declared intent* — the anchor atom
labels, an optional goal, a purpose. Its **current** value (a distance in Å, an
angle in °, a coordination count) and its **verdict** (ok / warn / fail) are
derived from the present geometry, so they refresh after every edit or relax.
This module is the single place that derivation lives; the store snapshots it on
save and the read surfaces (handler view + web viewer) recompute it on load.

Eyes carry no goal — they are navigation handles, so their "value" is just the
support set + what it currently touches (the §6.6 embodiment readout).
"""

from __future__ import annotations

from typing import Any

from . import probe
from .scene import Measure, Scene

#: Measure kinds and the exact operand (atom-label) count each needs.
_MEASURE_ARITY: dict[str, int] = {
    "distance": 2,
    "bond_length": 2,
    "angle": 3,
    "coordination": 1,
}
_MEASURE_KINDS = frozenset(_MEASURE_ARITY)
EYE = "eye"


def is_eye(m: Measure) -> bool:
    return m.kind == EYE


def _missing(scene: Scene, labels: list[str]) -> list[str]:
    return [la for la in labels if la not in scene.atoms]


def _verdict(
    value: float, direction: str | None, goal: dict[str, float] | None
) -> str | None:
    """Grade a scalar against a goal. None when there is nothing to grade."""
    if not goal or direction is None:
        return None
    if direction == "target":
        target = goal.get("target")
        if target is None:
            return None
        tol = goal.get("tol", 0.0)
        return "ok" if abs(value - target) <= tol else "fail"
    if direction == "min":
        floor = goal.get("min", goal.get("target"))
        return None if floor is None else ("ok" if value >= floor else "fail")
    if direction == "max":
        ceil = goal.get("max", goal.get("target"))
        return None if ceil is None else ("ok" if value <= ceil else "fail")
    return None


def evaluate(scene: Scene, m: Measure) -> tuple[dict[str, Any], str | None]:
    """Return ``(value_derived, verdict)`` for one marker against ``scene``.

    A dangling anchor (an operand atom the design no longer has) yields a
    ``{'error': 'missing atoms …'}`` value and a ``'dangling'`` verdict rather
    than raising — a marker outliving its atoms is a legible state, not a crash.
    """
    missing = _missing(scene, m.operands)
    if missing:
        return {"error": f"missing atoms: {', '.join(missing)}"}, "dangling"

    if is_eye(m):
        pov = probe.pov(scene, m.operands, reach=m.reach or 3.0)
        return {
            "support": pov.i_include,
            "touch": [{"label": la, "distance": round(d, 3)} for la, d in pov.i_touch],
        }, None

    if m.kind in ("distance", "bond_length"):
        val = probe.distance(scene, m.operands[0], m.operands[1])
    elif m.kind == "angle":
        val = probe.angle(scene, m.operands[0], m.operands[1], m.operands[2])
    elif m.kind == "coordination":
        val = float(probe.coordination(scene, m.operands[0]))
    else:  # pragma: no cover — guarded at op-validation time
        return {"error": f"unknown measure kind {m.kind!r}"}, None

    verdict = _verdict(val, m.direction, m.goal)
    # a soft constraint downgrades a failure to a warning; hard/gauge keep it
    if verdict == "fail" and m.strength == "soft":
        verdict = "warn"
    unit = {"distance": "Å", "bond_length": "Å", "angle": "°", "coordination": ""}[
        m.kind
    ]
    return {"value": round(val, 4), "unit": unit}, verdict


__all__ = ["EYE", "_MEASURE_ARITY", "_MEASURE_KINDS", "evaluate", "is_eye"]
