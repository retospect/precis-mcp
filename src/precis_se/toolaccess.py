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

The hand holding the tool is modelled for **screwdrivers only**, and only
by the insertion-path check below (:func:`insertion_path`, rule
``fastener_insertion_path``, docs/backlog/se-mechanical-drc.md ruling 3): a
``hand`` record on a ``bit_tools`` row adds one provisional cylinder behind
the handle end. :func:`access` itself, and every tool without a ``hand``
record (hex keys, ratchet, T-handle), still treat the tool as a bare
object. Still deliberately not modelled: approach at an angle (every tool
here is coaxial with the screw); ratchet arc, i.e. a handle that only needs
a *sector* rather than a full circle — a real escape for a tight joint, and
the reason this check warns rather than refuses.

**Can it get there?** (:func:`insertion_path`.) :func:`access` asks whether
a driver can TURN an already-seated screw; the insertion-path check asks
whether the screw can REACH its seat and its tool operate there, against
the FULLY ASSEMBLED design (ruling 1) — straight along the screw axis only
(ruling 4). Body first, tools only if the body clears.
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
from precis_se.validate import (
    ValidationIssue,
    _aabb_clear,
    _is_ancestor,
    _posed_component,
    is_realized,
)

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
    #: Provisional gripping-hand cylinder behind the handle end (screwdrivers
    #: only; 0 = no hand modelled). ``hand_source`` is the data file's own
    #: provenance sentence for the numbers.
    hand_radius_m: float = 0.0
    hand_length_m: float = 0.0
    hand_source: str = ""


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
                hand_radius_m=float((spec.get("hand") or {}).get("radius_mm", 0.0))
                / 1000.0,
                hand_length_m=float((spec.get("hand") or {}).get("length_mm", 0.0))
                / 1000.0,
                hand_source=str((spec.get("hand") or {}).get("source") or ""),
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
            # Same rule as validate.envelope_overlaps: a block that is an
            # ancestor of the fastener is containment, not an obstacle — a
            # driver reaching into a bolt inside its own grouping module's
            # bounding envelope has not hit anything.
            if _is_ancestor(tree, name, fastener):
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


# ── insertion path (rule ``fastener_insertion_path``) ────────────────────


@dataclass
class InsertionOutcome:
    """What :func:`insertion_path` found for one screw. ``findings`` is
    empty when the path is clear; ``body_blocked`` is true when the screw
    itself cannot reach its seat (by a hard or an ancestor blocker), which
    is the caller's cue to drop the ``no_tool_access`` finding — turning a
    screw that cannot get there is not the useful message."""

    findings: list[ValidationIssue]
    body_blocked: bool


class _BudgetExhausted(Exception):
    """The narrow-phase budget ran out: report nothing, not a guess."""


@dataclass
class _Hit:
    blocker: str
    #: True when the blocker is a material ancestor of the screw — a block
    #: whose hole the fasten pass does not stamp, so the verdict is "path
    #: unverified" rather than "path blocked".
    soft: bool


def _swept_cyl(
    design: CadDesign,
    name: str,
    radius_m: float,
    height_m: float,
    base: list[float],
    direction: list[float],
) -> tuple[Any, dict[str, Any]] | None:
    """One swept cylinder (base on ``base``, rising along ``direction``)
    as a design component plus its world-frame render record."""
    envelope = f"cyl:r{radius_m:.9f}h{height_m:.9f}"
    try:
        prim = cad_dsl.build(cad_dsl.parse(envelope))
    except (cad_dsl.DslError, ValueError):  # pragma: no cover — generated
        return None
    rot = _rot_to(direction)
    design.add_component(
        name, design.prim(name, prim, cad_pose(cad_as_vec3(base), rot))
    )
    record = {
        "envelope": envelope,
        "pose": [float(v) for v in base],
        "rot": [float(v) for v in rot],
    }
    return design.components[name], record


def _hole_cutter(design: CadDesign, index: int, hole: Any) -> Any:
    """The stamped hole as a subtractive primitive in world frame — the
    same ``cyl:``/``cone:`` shapes :mod:`precis_se.printsolid` cuts. A hex
    nut pocket is cut as its inscribed circle (under-subtracting: the
    conservative direction for an obstacle test)."""
    if hole.diameter_m <= 0.0 or hole.depth_m <= 0.0:
        return None
    radius = hole.diameter_m / 2.0
    if hole.kind == "nut-pocket" and hole.across_flats_m:
        radius = hole.across_flats_m / 2.0
    alias = "cone" if hole.kind == "countersink" else "cyl"
    try:
        prim = cad_dsl.build(cad_dsl.parse(f"{alias}:r{radius:.9f}h{hole.depth_m:.9f}"))
    except (cad_dsl.DslError, ValueError):  # pragma: no cover — generated
        return None
    xform = cad_pose(cad_as_vec3(hole.origin), _rot_to([float(v) for v in hole.axis]))
    return design.prim(f"__hole{index}__", prim, xform)


def _insertion_obstacles(
    design: CadDesign, tree: SeTree, fastener: str, holes: list[Any]
) -> list[tuple[str, tuple[Any, Any], bool]]:
    """Final-state obstacles, ``(name, aabb, is_material_ancestor)``,
    non-ancestors first (sorted) then material ancestors (sorted) — so a
    real blocker is always preferred over a gap in our own hole stamping.

    Every block except the screw and its PURE-GROUPING ancestors: an
    ancestor is material when it has a mode or a binding
    (:func:`precis_se.validate.is_realized`). The holes the fasten pass
    stamped for this joint are subtracted from their member."""
    cutters: dict[str, list[Any]] = {}
    for i, hole in enumerate(holes):
        cutter = _hole_cutter(design, i, hole)
        if cutter is not None:
            cutters.setdefault(hole.block, []).append(cutter)
    found: list[tuple[str, tuple[Any, Any], bool]] = []
    for name, node in sorted(tree.blocks.items()):
        if name == fastener:
            continue
        ancestor = _is_ancestor(tree, name, fastener)
        if ancestor and not is_realized(node):
            continue
        env = effective_envelope(tree, node)
        if not env:
            continue
        expr = _posed_component(design, name, env, node)
        if expr is None:
            continue
        if name in cutters:
            design.add_component(name, design.subtract(expr, *cutters[name]))
            expr = design.components[name]
        found.append((name, cad_bulk.expr_aabb(design, expr), ancestor))
    found.sort(key=lambda row: (row[2], row[0]))
    return found


def _first_hit(
    design: CadDesign,
    solid: str,
    obstacles: list[tuple[str, tuple[Any, Any], bool]],
    *,
    plane_origin: list[float],
    plane_dir: list[float],
    budget: list[int],
) -> _Hit | None:
    """The first obstacle the swept solid ``solid`` interpenetrates, or
    ``None``. Same three phases as :func:`access` — a half-space cull
    behind ``plane_origin``, the AABB broad phase, then exact SDF clearance
    — and the same per-screw narrow-phase ``budget`` (shared across every
    solid of one screw's check; a one-element list so it is spent in
    place)."""
    box_solid = cad_bulk.expr_aabb(design, design.components[solid])
    for name, box, ancestor in obstacles:
        if _behind_the_drive_face(box, plane_origin, plane_dir):
            continue
        if _aabb_clear(box_solid, box, _TOUCH_M):
            continue
        if budget[0] <= 0:
            raise _BudgetExhausted
        budget[0] -= 1
        if cad_relate.clearance(design, solid, name).gap < -_TOUCH_M:
            return _Hit(blocker=name, soft=ancestor)
    return None


def _geometry(
    role: str, pieces: list[dict[str, Any]], blocker: str | None
) -> list[dict[str, Any]]:
    return [{"role": role, **piece, "blocker": blocker} for piece in pieces]


def insertion_path(
    tree: SeTree,
    *,
    fastener: str,
    subject: str,
    origin: list[float],
    axis: list[float],
    length_m: float,
    head_radius_m: float,
    head_height_m: float,
    countersunk: bool,
    engagement_m: float,
    drive_type: str | None,
    drive_size_mm: float | None,
    holes: list[Any],
) -> InsertionOutcome | None:
    """Can the screw reach its seat, and can a tool operate it there, with
    every other block present (rule ``fastener_insertion_path``)?

    ``origin``/``axis`` are the screw's head bearing face (the SEATED head
    plane) and its head→thread direction. Three straight axial sweeps
    (ruling 4), each a ``cyl:`` so the existing SDF clearance is reused:

    - **body** — head radius (plus the shank clearance allowance, folded
      into ``head_radius_m`` by the caller) from the seated head plane back
      along ``−axis`` by the screw length plus the head height (catalog
      length is under the head; a countersunk head's includes it). The
      start of insertion is "tip at the seat plane", so the length is a
      *minimum*, not a generous
      allowance; the shank's own travel inside the members lies in the
      stamped ``holes``, which are subtracted from them.
    - **tool** — each candidate's shaft and swing disc (as in
      :func:`access`), lengthened axially by ``engagement_m``. ASSUMPTION:
      the screw is finger-started and the tool drives only the engaged
      part of the thread, so the tool rides the head in by the thread
      engagement and no further. A captive or recessed screw that cannot
      be finger-started — the tool has to carry it the whole way — is the
      case this misses.
    - **hand** — screwdriver tools only (a ``hand`` record in the data):
      a cylinder behind the tool's handle end, same lengthening.

    Body first; tools only if the body clears. A blocker that is a
    non-ancestor block is an ``error``; a blocker that is a MATERIAL
    ancestor (mode or binding) yields only ``material_parent_not_walked``
    (``warn``): the fasten pass stamps no hole in a parent, so the path
    through it is unverified, not wrong.

    ``None`` when there is nothing to check (no length/radius) or the
    narrow-phase budget (:data:`_MAX_NARROW_PHASE`, per screw) ran out —
    an unchecked screw and a blocked one must not read the same."""
    if length_m <= 0.0 or head_radius_m <= 0.0:
        return None
    back = [-a for a in axis]  # the tool's side: away from the work
    seat = [float(v) for v in origin]
    design = CadDesign()
    obstacles = _insertion_obstacles(design, tree, fastener, holes)
    budget = [_MAX_NARROW_PHASE]
    try:
        return _insertion_outcome(
            design,
            obstacles,
            budget,
            fastener=fastener,
            subject=subject,
            seat=seat,
            axis=axis,
            back=back,
            length_m=length_m,
            head_radius_m=head_radius_m,
            head_height_m=head_height_m,
            countersunk=countersunk,
            engagement_m=engagement_m,
            drive_type=drive_type,
            drive_size_mm=drive_size_mm,
        )
    except _BudgetExhausted:
        return None


def _insertion_outcome(
    design: CadDesign,
    obstacles: list[tuple[str, tuple[Any, Any], bool]],
    budget: list[int],
    *,
    fastener: str,
    subject: str,
    seat: list[float],
    axis: list[float],
    back: list[float],
    length_m: float,
    head_radius_m: float,
    head_height_m: float,
    countersunk: bool,
    engagement_m: float,
    drive_type: str | None,
    drive_size_mm: float | None,
) -> InsertionOutcome | None:
    # Tip at the seat plane puts the head's top one screw length plus one
    # head height back: catalog length is measured under the head, except
    # for a countersunk head, whose length already includes it.
    body_len = length_m + (0.0 if countersunk else head_height_m)
    body_base = [s + body_len * b for s, b in zip(seat, back, strict=True)]
    body = _swept_cyl(design, "__body__", head_radius_m, body_len, body_base, axis)
    if body is None:  # pragma: no cover — generated source always parses
        return None
    hit = _first_hit(
        design, "__body__", obstacles, plane_origin=seat, plane_dir=back, budget=budget
    )
    if hit is not None:
        geometry = _geometry("body", [body[1]], hit.blocker)
        what = (
            f"the screw body ({head_radius_m * 2000:.1f} mm Ø swept "
            f"{body_len * 1000:.1f} mm straight back along the screw axis from "
            "the seat plane)"
        )
        return InsertionOutcome(
            [_insertion_issue(subject, fastener, what, hit, geometry)], True
        )

    candidates = tools_for(drive_type, drive_size_mm)
    if not candidates:
        return InsertionOutcome([], False)
    drive_offset = 0.0 if countersunk else -head_height_m
    face = [s + drive_offset * a for s, a in zip(seat, axis, strict=True)]
    records: list[tuple[Tool, str, _Hit, list[dict[str, Any]]]] = []
    for i, tool in enumerate(candidates):
        pieces: list[dict[str, Any]] = []
        parts: list[Any] = []
        for j, (radius, height, offset) in enumerate(
            (
                (tool.shaft_radius_m, tool.axial_m + engagement_m, 0.0),
                (
                    tool.swing_radius_m,
                    tool.arm_thickness_m + engagement_m,
                    tool.swing_height_m,
                ),
            )
        ):
            base = [f + offset * b for f, b in zip(face, back, strict=True)]
            made = _swept_cyl(design, f"__tool{i}_{j}__", radius, height, base, back)
            if made is None:  # pragma: no cover — generated
                return InsertionOutcome([], False)
            parts.append(made[0])
            pieces.append(made[1])
        design.add_component(f"__tool{i}__", design.merge(*parts))
        hit = _first_hit(
            design,
            f"__tool{i}__",
            obstacles,
            plane_origin=face,
            plane_dir=back,
            budget=budget,
        )
        if hit is not None:
            records.append((tool, "tool", hit, pieces))
            continue
        if tool.hand_radius_m <= 0.0 or tool.hand_length_m <= 0.0:
            return InsertionOutcome([], False)  # a bare tool clears: fine
        top = max(tool.axial_m, tool.swing_height_m + tool.arm_thickness_m)
        hand_base = [f + top * b for f, b in zip(face, back, strict=True)]
        hand = _swept_cyl(
            design,
            f"__hand{i}__",
            tool.hand_radius_m,
            tool.hand_length_m + engagement_m,
            hand_base,
            back,
        )
        if hand is None:  # pragma: no cover — generated
            return InsertionOutcome([], False)
        hit = _first_hit(
            design,
            f"__hand{i}__",
            obstacles,
            plane_origin=face,
            plane_dir=back,
            budget=budget,
        )
        if hit is None:
            return InsertionOutcome([], False)
        records.append((tool, "hand", hit, [*pieces, hand[1]]))
    # Every tool (+hand) is blocked. The focus is what the finding is
    # about: a soft (ancestor-only) record decides a warning, else the
    # tool whose own solid clears and only the hand hits, else the
    # shop's preferred tool.
    focus = next(
        (r for r in records if r[2].soft),
        next((r for r in records if r[1] == "hand"), records[0]),
    )
    tool, part, hit, pieces = focus
    geometry = _geometry("tool", pieces[:2], hit.blocker if part == "tool" else None)
    if part == "hand":
        geometry += _geometry("hand", pieces[2:], hit.blocker)
    others = "; ".join(
        f"{t.tool_id}: the {p} hits {h.blocker!r}" for t, p, h, _ in records
    )
    what = (
        f"every tool for the {drive_type} drive ({others}); closest is "
        f"{tool.title} — its {part} hits the blocker"
    )
    return InsertionOutcome(
        [_insertion_issue(subject, fastener, what, hit, geometry, reached=True)],
        False,
    )


def _insertion_issue(
    subject: str,
    fastener: str,
    what: str,
    hit: _Hit,
    geometry: list[dict[str, Any]],
    *,
    reached: bool = False,
) -> ValidationIssue:
    if hit.soft:
        return ValidationIssue(
            rule="material_parent_not_walked",
            subject=subject,
            detail=(
                f"the insertion path of {fastener!r} — {what} — crosses "
                f"{hit.blocker!r}, the screw's own parent, which is made or "
                "bought (it has a mode or a binding). The fasten pass stamps "
                "no hole in a parent block, so whether the screw can pass "
                "is UNVERIFIED, not failed; not reported as an error until "
                "parent holes are stamped (se-container-block-is-not-first-"
                "class)"
            ),
            severity="warn",
            geometry=geometry,
        )
    verb = (
        f"{fastener!r} reaches its seat but no tool can drive it there"
        if reached
        else f"{fastener!r} cannot reach its seat"
    )
    return ValidationIssue(
        rule="fastener_insertion_path",
        subject=subject,
        detail=(
            f"{verb}: {what} is blocked by {hit.blocker!r} with every other "
            "block in place (the fully assembled state). Move the screw, "
            "move that block, or change the head/drive so the path is clear"
        ),
        severity="error",
        geometry=geometry,
    )
