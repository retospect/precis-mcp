"""Can you actually reach the screw? (rung 3b — tool access.)

The one genuinely new *geometric* check the fastening engine still owed.
Everything else in :mod:`precis_se.fasten` asks about the screw; this asks
about the **tool**, and the tool is bigger than the screw by an order of
magnitude: a hex key turning an M3 sweeps a 66 mm circle, which is the
radius that decides whether a bracket can sit where the designer drew it.

**Model.** One tool is two solids standing on the drive face, together the
swept volume of a full turn: a *shaft* (cylinder, ``shaft_radius`` ×
``axial``) and an *arm* (a disc of ``swing_radius``, ``arm_thickness``
thick, at ``swing_height``). Data — `precis/data/driver_envelopes.json`,
ISO 2936 for the keys, nominal bench geometry for the bit tools, marked as
such there.

**Method.** Three phases, narrowing hard, because
:func:`precis.cad.relate.clearance` costs ~2.3 s a pair and this runs
inside a synchronous read. First the **drive-face cull**: a driver lives
on one side of the head and the members it clamps live on the other, so
every block whose AABB is wholly behind the drive plane is gone without
arithmetic — in an ordinary stack that is all of them. Then the AABB
broad phase `validate`'s interpenetration scan uses. Only what survives
both reaches the SDF, and a hard call budget turns a pathological design
into "not checked" rather than a two-minute view.

**What it answers.** Not "is there access" but *which tool* — "long-arm
hex key only" is an instruction, "no access" alone is an argument. Tools
are tried in the shop's preference order and the first that clears wins;
the finding fires only when **none** do, and names the block that blocked
the best one.

Deliberately not modelled: the hand holding the tool; approach at an angle
(every tool here is coaxial with the screw); ratchet arc, i.e. a handle
that only needs a *sector* rather than a full circle — a real escape for a
tight joint, and the reason this check warns rather than refuses.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from precis.cad import bulk as cad_bulk
from precis.cad import dsl as cad_dsl
from precis.cad import relate as cad_relate
from precis.cad.graph import Design as CadDesign
from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis_se.ops import SeTree, effective_envelope
from precis_se.validate import ValidationIssue, _aabb_clear, _posed_component

_PACKAGED_DATA = "precis.data"
_FILE = "driver_envelopes.json"

#: Clearance below which the driver is *touching* rather than fitting.
#: Absolute, like `fasten._MIN_MEMBER_M` and for the same reason: a hand
#: tool is a physical object of known size, not something that scales with
#: the drawing (metres).
_TOUCH_M = 1e-4

#: Hardest cap on narrow-phase work per screw. `clearance` costs ~2.3 s a
#: pair, and this runs inside a synchronous MCP read, so a pathological
#: design must degrade to "not checked" rather than to a two-minute view.
#: Reached only after the drive-face cull and the AABB filter have both
#: failed to remove a pair, which in an ordinary stack never happens.
_MAX_NARROW_PHASE = 12


@dataclass(frozen=True)
class Tool:
    """One driver's swept envelope, in metres, standing on the drive
    face and rising *away* from the work."""

    tool_id: str
    title: str
    axial_m: float
    shaft_radius_m: float
    swing_radius_m: float
    swing_height_m: float
    arm_thickness_m: float
    note: str = ""


@dataclass
class AccessResult:
    """Which tools fit this screw. ``fits`` is in preference order, best
    first; ``blocked`` maps a tool that didn't fit to the block that stops
    it, so the finding can name something actionable."""

    subject: str
    fastener: str
    drive_type: str | None
    candidates: list[str]
    fits: list[Tool]
    blocked: dict[str, str]


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    text = resources.files(_PACKAGED_DATA).joinpath(_FILE).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(text)
    return parsed


@lru_cache(maxsize=1)
def _bit_tools() -> dict[str, dict[str, Any]]:
    return {str(t["id"]): t for t in _data().get("bit_tools") or []}


def _hex_key(across_flats_mm: float) -> dict[str, Any] | None:
    """The key row for this socket size — exact match only. A 4.5 mm key
    is not a 4 mm key, and rounding to the nearest listed size is how a
    tool that does not exist ends up in a finding."""
    for row in (_data().get("hex_keys") or {}).get("sizes") or []:
        if abs(float(row["across_flats"]) - across_flats_mm) < 1e-9:
            return row
    return None


def tools_for(drive_type: str | None, drive_size_mm: float | None) -> list[Tool]:
    """Every tool that can turn this drive, in the shop's preference
    order. Empty when the drive type is unknown — a drive nobody has
    listed a tool for gets no finding, because "no tool fits" and "no tool
    is known" are different facts and only one of them is about the
    design."""
    order = (_data().get("drive_tools") or {}).get(str(drive_type or "").lower())
    if not isinstance(order, list):
        return []
    out: list[Tool] = []
    for tool_id in order:
        if tool_id.startswith("hex-key"):
            if drive_size_mm is None:
                continue
            row = _hex_key(drive_size_mm)
            if row is None:
                continue
            short = float(row["short_arm"])
            long_arm = float(row["long_arm"])
            thickness = float(row["across_flats"])
            if tool_id == "hex-key-short":
                # Short arm in the socket: the long arm sweeps a wide
                # circle just above the head. The torque position.
                axial, swing, height = thickness, long_arm, thickness / 2.0
            else:
                # Long arm in the socket: a tall thin column with the
                # short arm sweeping at the top. Reaches into a recess.
                axial, swing, height = long_arm, short, long_arm
            out.append(
                Tool(
                    tool_id=tool_id,
                    title=(
                        f"{thickness:g} mm hex key, "
                        + (
                            "short arm in (torque)"
                            if tool_id == "hex-key-short"
                            else "long arm in (reach)"
                        )
                    ),
                    axial_m=axial / 1000.0,
                    shaft_radius_m=thickness / 2000.0,
                    swing_radius_m=swing / 1000.0,
                    swing_height_m=height / 1000.0,
                    arm_thickness_m=thickness / 1000.0,
                )
            )
            continue
        spec = _bit_tools().get(tool_id)
        if spec is None:
            continue
        out.append(
            Tool(
                tool_id=tool_id,
                title=str(spec["title"]),
                axial_m=float(spec["axial_mm"]) / 1000.0,
                shaft_radius_m=float(spec["shaft_radius_mm"]) / 1000.0,
                swing_radius_m=float(spec["swing_radius_mm"]) / 1000.0,
                swing_height_m=float(spec["swing_height_mm"]) / 1000.0,
                arm_thickness_m=float(spec["arm_thickness_mm"]) / 1000.0,
                note=str(spec.get("note") or ""),
            )
        )
    return out


def access(
    tree: SeTree,
    *,
    fastener: str,
    subject: str,
    drive_origin: list[float],
    drive_axis: list[float],
    drive_type: str | None,
    drive_size_mm: float | None,
) -> AccessResult | None:
    """Which of this drive's tools clear the assembly.

    ``drive_origin``/``drive_axis`` are the drive face and the direction
    the tool comes *from* (the screw's ``−z``, already reversed by the
    caller, which owns the screw's frame). The fastener's own block is
    excluded — a driver touches the head it turns.

    ``None`` when there is nothing to check: no tool known for the drive,
    or no other block with an envelope."""
    candidates = tools_for(drive_type, drive_size_mm)
    if not candidates:
        return None
    budget = _MAX_NARROW_PHASE
    result = AccessResult(
        subject=subject,
        fastener=fastener,
        drive_type=drive_type,
        candidates=[t.tool_id for t in candidates],
        fits=[],
        blocked={},
    )
    for tool in candidates:
        design = CadDesign()
        driver = _driver_component(design, tool, drive_origin, drive_axis)
        if driver is None:  # pragma: no cover — generated source always parses
            return None
        box_driver = cad_bulk.expr_aabb(design, driver)
        blocker: str | None = None
        for name, node in sorted(tree.blocks.items()):
            if name == fastener:
                continue
            env = effective_envelope(tree, node)
            if not env:
                continue
            expr = _posed_component(design, name, env, node)
            if expr is None:
                continue
            box = cad_bulk.expr_aabb(design, expr)
            if _behind_the_drive_face(box, drive_origin, drive_axis):
                continue
            if _aabb_clear(box_driver, box, _TOUCH_M):
                continue
            if budget <= 0:
                # Out of narrow-phase budget: report nothing rather than
                # a guess. An unchecked screw and a blocked one must not
                # read the same.
                return None
            budget -= 1
            if cad_relate.clearance(design, "__driver__", name).gap < -_TOUCH_M:
                blocker = name
                break
        if blocker is None:
            # Preference order, so the first tool that clears IS the
            # answer — and stopping here keeps the common case to one
            # driver's worth of SDF work instead of four.
            result.fits.append(tool)
            break
        result.blocked[tool.tool_id] = blocker
    return result


def _behind_the_drive_face(
    box: tuple[Any, Any], origin: list[float], axis: list[float]
) -> bool:
    """True when every corner of ``box`` lies behind the drive face.

    The cull that makes this check affordable. A driver lives entirely on
    one side of the head; the members it clamps live on the other, and in
    an ordinary stack that is *every other block in the design*. An AABB
    contains its solid, so a box wholly behind the plane proves the solid
    is — no SDF, no 2.3-second narrow phase, for the pairs that could
    never have interfered.
    """
    lo, hi = box
    for i in range(8):
        corner = [float(hi[k]) if (i >> k) & 1 else float(lo[k]) for k in range(3)]
        along = sum((c - o) * a for c, o, a in zip(corner, origin, axis, strict=True))
        if along > _TOUCH_M:
            return False
    return True


def _driver_component(
    design: CadDesign, tool: Tool, origin: list[float], axis: list[float]
) -> Any:
    """Add the tool as the component ``__driver__``: the shaft standing on
    the drive face and the arm disc above it, both posed so their local
    ``+z`` runs along ``axis``."""
    rot = _rot_to(axis)
    parts: list[Any] = []
    for spec_text, offset in (
        (f"cyl:r{tool.shaft_radius_m:.9f}h{tool.axial_m:.9f}", 0.0),
        (
            f"cyl:r{tool.swing_radius_m:.9f}h{tool.arm_thickness_m:.9f}",
            tool.swing_height_m,
        ),
    ):
        try:
            prim = cad_dsl.build(cad_dsl.parse(spec_text))
        except (cad_dsl.DslError, ValueError):  # pragma: no cover — generated
            return None
        at = cad_as_vec3([o + offset * a for o, a in zip(origin, axis, strict=True)])
        parts.append(design.prim(f"__driver__{len(parts)}", prim, cad_pose(at, rot)))
    design.add_component("__driver__", design.merge(*parts))
    return design.components["__driver__"]


def _rot_to(axis: list[float]) -> Any:
    """Euler angles (world-frame v1, the pose convention) that take ``+z``
    onto ``axis``. Yaw is free about the tool's own axis — a driver is a
    solid of revolution — so only the tilt matters."""
    x, y, z = (float(v) for v in axis)
    norm = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / norm, y / norm, z / norm
    pitch = math.acos(max(-1.0, min(1.0, z)))
    yaw = math.atan2(y, x)
    # Rz(yaw)·Ry(pitch) takes +z onto the axis; the pose helper takes
    # (rx, ry, rz) and applies them in that order, so the tilt goes in ry.
    return cad_as_vec3([0.0, pitch, yaw])


def finding(result: AccessResult) -> ValidationIssue | None:
    """The finding for a screw **no** tool can turn, or ``None``. One
    finding per screw, naming the tool that came closest and what stopped
    it — a designer can move one block, not a list of them."""
    if result.fits or not result.blocked:
        return None
    first = result.candidates[0]
    blocker = result.blocked.get(first) or next(iter(result.blocked.values()))
    return ValidationIssue(
        rule="no_tool_access",
        subject=result.subject,
        detail=(
            f"nothing can turn {result.fastener!r}: every "
            f"{result.drive_type} driver "
            f"({', '.join(result.candidates)}) hits another block — the "
            f"preferred one is stopped by {blocker!r}. Move the screw, move "
            f"that block, or change the head so a different tool reaches. "
            "(A ratchet needing only a sector of its swing is not modelled, "
            "so a tight joint may still be buildable in practice.)"
        ),
        severity="warn",
    )
