"""Millimetre enclosure parameters to two assembled CAD scenes."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from precis.cad.scene import NodeSpec, SceneSpec
from precis.enclosure_screws import enclosure_screw
from precis.errors import BadInput
from precis.utils.units import format_dsl_number


@dataclass(frozen=True)
class Enclosure:
    params: dict[str, Any]
    parts: dict[str, SceneSpec]
    boss_centers: tuple[tuple[float, float], ...]
    pilot_diameter: float
    clearance_diameter: float
    head_diameter: float
    head_height: float


def _refuse(message: str, remedy: str) -> None:
    raise BadInput(f"enclosure_box: {message}", next=remedy)


def _number(name: str, value: Any, *, zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        _refuse(
            f"{name} must be a finite number",
            f"Set params.{name} to a number in mm (draft_angle in degrees).",
        )
    if value < 0 or (not zero and value == 0):
        _refuse(
            f"{name} must be {'nonnegative' if zero else 'positive'}",
            f"Increase params.{name}.",
        )
    return float(value)


def _distance(px: float, py: float, cx: float, cy: float, radius: float) -> float:
    """Signed distance to a rounded rectangle with core dimensions cx, cy."""
    qx, qy = abs(px) - cx / 2, abs(py) - cy / 2
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius


def _node(
    name: str,
    op: str,
    part: str,
    shape: str,
    dims: dict[str, float],
    loc: tuple[float, float, float] = (0, 0, 0),
    rot: tuple[float, float, float] = (0, 0, 0),
) -> NodeSpec:
    # SceneSpec stores bare SI metres, unlike user-authored DSL quantities.
    config = (
        shape
        + ":"
        + "".join(
            f"{key}{format_dsl_number(value / 1000)}" for key, value in dims.items()
        )
    )
    return NodeSpec(
        name, op, config, part, (loc[0] / 1000, loc[1] / 1000, loc[2] / 1000), rot
    )


def _profile(
    name: str,
    op: str,
    part: str,
    cx: float,
    cy: float,
    rb: float,
    rt: float,
    height: float,
    z0: float,
) -> list[NodeSpec]:
    """Union decomposition of a rounded rectangular frustum.

    Side strips' lower outer corners sit at z0; their inward sloping lower
    faces are covered by the straight core. Their top overrun is deliberate:
    callers clip additive profiles and let cavity cutters run through the rim.
    Every returned node has the same op, so the same decomposition cuts a cavity.
    """
    out = [
        _node(
            name + "-x",
            op,
            part,
            "box",
            {"w": cx + 2 * rb, "d": cy, "h": height},
            (0, 0, z0),
        ),
        _node(
            name + "-y",
            op,
            part,
            "box",
            {"w": cx, "d": cy + 2 * rb, "h": height},
            (0, 0, z0),
        ),
    ]
    if rt > rb:
        angle = math.atan2(rt - rb, height)
        c, s = math.cos(angle), math.sin(angle)
        thickness = rt * c
        for sign in (-1, 1):
            out.append(
                _node(
                    f"{name}-draft-y{sign}",
                    op,
                    part,
                    "box",
                    {"w": cx, "d": thickness, "h": height / c},
                    (
                        0,
                        sign * (cy / 2 + rb - thickness * c / 2),
                        z0 + thickness * s / 2,
                    ),
                    (-sign * angle, 0, 0),
                )
            )
            out.append(
                _node(
                    f"{name}-draft-x{sign}",
                    op,
                    part,
                    "box",
                    {"w": thickness, "d": cy, "h": height / c},
                    (
                        sign * (cx / 2 + rb - thickness * c / 2),
                        0,
                        z0 + thickness * s / 2,
                    ),
                    (0, sign * angle, 0),
                )
            )
    if rt > 0:
        for i, (sx, sy) in enumerate(((-1, -1), (-1, 1), (1, -1), (1, 1))):
            loc = (sx * cx / 2, sy * cy / 2, z0)
            if rb == rt:
                out.append(
                    _node(
                        f"{name}-corner-{i}",
                        op,
                        part,
                        "cyl",
                        {"r": rb, "h": height},
                        loc,
                    )
                )
            elif rb == 0:
                out.append(
                    _node(
                        f"{name}-corner-{i}",
                        op,
                        part,
                        "cone",
                        {"r": rt, "h": height},
                        (loc[0], loc[1], z0 + height),
                        (math.pi, 0, 0),
                    )
                )
            else:
                out.append(
                    _node(
                        f"{name}-corner-{i}",
                        op,
                        part,
                        "tcone",
                        {"rb": rb, "rt": rt, "h": height},
                        loc,
                    )
                )
    return out


def _clip(
    part: str, name: str, width: float, depth: float, height: float, z0: float
) -> NodeSpec:
    return _node(
        name,
        "intersect",
        part,
        "box",
        {"w": width, "d": depth, "h": height},
        (0, 0, z0),
    )


def enclosure_box(
    x: float,
    y: float,
    z: float,
    *,
    wall: float = 2,
    floor: float = 2,
    lid_thickness: float = 3,
    corner_radius: float = 4,
    draft_angle: float = 0,
    lip_height: float = 3,
    lip_clearance: float = 0.3,
    boss_count: int = 4,
    boss_placement: Sequence[Sequence[float]] | None = None,
    boss_outer_diameter: float = 8,
    screw_size: str = "M3",
    counterbore: bool = False,
    ribs: bool = True,
    rib_ratio: float = 0.55,
) -> Enclosure:
    """Return box/lid scenes. x/y are the inner dimensions at the cavity floor.

    boss_placement is centred XY in mm; automatic 2-boss placement uses opposite
    corners. Positive draft opens toward the lid. All validation precedes nodes.
    """
    values = {
        name: _number(name, value, zero=name in {"draft_angle", "corner_radius"})
        for name, value in dict(
            x=x,
            y=y,
            z=z,
            wall=wall,
            floor=floor,
            lid_thickness=lid_thickness,
            corner_radius=corner_radius,
            draft_angle=draft_angle,
            lip_height=lip_height,
            lip_clearance=lip_clearance,
            boss_outer_diameter=boss_outer_diameter,
            rib_ratio=rib_ratio,
        ).items()
    }
    x, y, z, wall, floor = (values[k] for k in ("x", "y", "z", "wall", "floor"))
    if not isinstance(counterbore, bool) or not isinstance(ribs, bool):
        _refuse(
            "counterbore and ribs must be booleans",
            "Use true or false for counterbore and ribs.",
        )
    if (
        isinstance(boss_count, bool)
        or not isinstance(boss_count, int)
        or not 0 <= boss_count <= 4
    ):
        _refuse(
            "boss_count must be an integer from 0 to 4",
            "Set boss_count to 0, 1, 2, 3 or 4.",
        )
    if not 0.5 <= rib_ratio <= 0.6:
        _refuse("rib_ratio must be 0.5–0.6 times wall", "Set rib_ratio=0.55.")
    if draft_angle >= 15:
        _refuse(
            "draft_angle must be below 15 degrees",
            "Choose draft_angle between 0 and 15 degrees, excluding 15.",
        )
    if corner_radius >= min(x, y) / 2:
        _refuse(
            "corner_radius consumes the inner size",
            "Reduce corner_radius below half the smaller inner dimension.",
        )
    if lip_height + lip_clearance >= z:
        _refuse(
            "lip_height plus clearance must be below cavity depth",
            "Reduce lip_height or increase z.",
        )
    screw = enclosure_screw(screw_size)
    if (boss_outer_diameter - screw.pilot) / 2 < wall:
        _refuse(
            "pilot hole leaves less than one wall thickness of boss wall",
            "Increase boss_outer_diameter to at least pilot diameter + 2*wall.",
        )
    if counterbore and lid_thickness <= screw.head_height + 1:
        _refuse(
            "counterbore needs lid thickness greater than head height plus a 1 mm floor",
            f"Set lid_thickness > {screw.head_height + 1:g} mm or counterbore=false.",
        )
    slope = math.tan(math.radians(draft_angle))
    offset = wall / math.cos(math.radians(draft_angle))
    cx, cy = x - 2 * corner_radius, y - 2 * corner_radius
    outer_rb = corner_radius + offset - floor * slope
    if outer_rb <= 0:
        _refuse(
            "floor and draft consume the outer corner radius",
            "Reduce floor or draft_angle, or increase corner_radius.",
        )
    rim = floor + z
    outer_rt = corner_radius + offset + z * slope
    lip_rb = corner_radius + (z - lip_height) * slope - lip_clearance
    lip_rt = corner_radius + z * slope - lip_clearance
    # A sharp inner lip corner is valid when the offset consumes its radius.
    inner_r = max(0.0, lip_rb - offset)
    inner_cx, inner_cy = (
        cx + 2 * (lip_rb - offset - inner_r),
        cy + 2 * (lip_rb - offset - inner_r),
    )
    if lip_rb < 0 or min(inner_cx, inner_cy) <= 0:
        _refuse(
            "lip clearance and wall consume the opening",
            "Increase inner size/corner_radius or reduce wall/lip_clearance.",
        )
    br = boss_outer_diameter / 2
    if boss_placement is None:
        # Leave a full boss radius plus clearance from the curved inner lip.
        bx = min(cx / 2, inner_cx / 2) - br - lip_clearance
        by = min(cy / 2, inner_cy / 2) - br - lip_clearance
        # Center offsets may be smaller than a boss radius (one boss or a
        # diagonal pair). The common disk/spacing checks below decide fit.
        if boss_count and min(bx, by) < 0:
            _refuse(
                "boss outer diameter plus lip clearance does not fit the inner corner region",
                "Increase x/y or reduce boss_outer_diameter, wall or lip_clearance.",
            )
        corners = ((-bx, -by), (bx, by), (-bx, by), (bx, -by))
        centers = corners[:boss_count]
    else:
        if (
            isinstance(boss_placement, str)
            or not isinstance(boss_placement, Sequence)
            or len(boss_placement) != boss_count
        ):
            _refuse(
                "boss_placement must contain boss_count XY pairs",
                "Supply one [x, y] pair in mm for each boss.",
            )
        points = []
        for pair in boss_placement:
            if (
                isinstance(pair, str)
                or not isinstance(pair, Sequence)
                or len(pair) != 2
            ):
                _refuse(
                    "boss_placement entries must be XY pairs",
                    "Supply [x, y] coordinates in mm.",
                )
            for v in pair:
                if (
                    isinstance(v, bool)
                    or not isinstance(v, int | float)
                    or not math.isfinite(v)
                ):
                    _refuse(
                        "boss_placement coordinates must be finite numbers",
                        "Use finite XY coordinates in mm.",
                    )
            points.append((float(pair[0]), float(pair[1])))
        centers = tuple(points)
    for i, (px, py) in enumerate(centers):
        circle = (
            max(screw.clearance, screw.head_diameter + 0.4 if counterbore else 0) / 2
        )
        if _distance(px, py, cx, cy, corner_radius) + circle >= 0:
            _refuse(
                "screw circle must stay inside the walls",
                "Move boss_placement inward or increase x/y.",
            )
        if (
            _distance(px, py, inner_cx, inner_cy, inner_r) + br + lip_clearance > 0
            or _distance(px, py, cx, cy, corner_radius) + br > 0
        ):
            _refuse(
                "lip clears bosses only when boss outer diameter plus lip clearance fits the inner corner region",
                "Move bosses inward, reduce boss_outer_diameter, or increase the inner size.",
            )
        if any(
            math.hypot(px - qx, py - qy) < 2 * br + lip_clearance
            for qx, qy in centers[:i]
        ):
            _refuse(
                "boss placements overlap",
                "Separate the boss centers by at least boss_outer_diameter + lip_clearance.",
            )
    params: dict[str, Any] = {
        **values,
        "boss_count": boss_count,
        "boss_placement": [list(p) for p in centers]
        if boss_placement is not None
        else None,
        "screw_size": screw_size.strip().upper(),
        "counterbore": counterbore,
        "ribs": ribs,
    }
    # Geometry starts only after every parameter and placement has passed.
    box = _profile("shell", "add", "box", cx, cy, outer_rb, outer_rt, rim, 0)
    box.append(
        _clip("box", "shell-height", cx + 2 * outer_rt, cy + 2 * outer_rt, rim, 0)
    )
    # Extend the cavity cutters through the rim, retaining the exact draft slope.
    extra = max(wall, lip_height)
    box.extend(
        _profile(
            "cavity",
            "cut",
            "box",
            cx,
            cy,
            corner_radius,
            corner_radius + (z + extra) * slope,
            z + extra,
            floor,
        )
    )
    lip_z = rim - lip_height
    lid = _profile("lip", "add", "lid", cx, cy, lip_rb, lip_rt, lip_height, lip_z)
    lid.append(
        _clip("lid", "lip-height", cx + 2 * lip_rt, cy + 2 * lip_rt, lip_height, lip_z)
    )
    lid.extend(
        _profile(
            "lip-opening",
            "cut",
            "lid",
            inner_cx,
            inner_cy,
            inner_r,
            inner_r + lip_height * slope,
            lip_height,
            lip_z,
        )
    )
    lid.extend(
        _profile("plate", "add", "lid", cx, cy, outer_rt, outer_rt, lid_thickness, rim)
    )
    pilots: list[NodeSpec] = []
    for i, (px, py) in enumerate(centers, 1):
        box.append(
            _node(f"boss-{i}", "add", "box", "cyl", {"r": br, "h": z}, (px, py, floor))
        )
        if ribs:
            # Four cardinal wall distances; these endpoints lie on the rounded cavity.
            dx = cx / 2 + math.sqrt(
                max(0, corner_radius**2 - max(abs(py) - cy / 2, 0) ** 2)
            )
            dy = cy / 2 + math.sqrt(
                max(0, corner_radius**2 - max(abs(px) - cx / 2, 0) ** 2)
            )
            distance, axis, sign = min(
                (dx - px, 0, 1), (dx + px, 0, -1), (dy - py, 1, 1), (dy + py, 1, -1)
            )
            rh = min(z - lip_height - lip_clearance, wall / (2 * slope) if slope else z)
            length = distance + wall / 2
            loc = [px, py, floor]
            loc[axis] += sign * length / 2
            dims = {
                "w": length if axis == 0 else rib_ratio * wall,
                "d": length if axis == 1 else rib_ratio * wall,
                "h": rh,
            }
            box.append(
                _node(f"rib-{i}", "add", "box", "box", dims, (loc[0], loc[1], loc[2]))
            )
        pilots.append(
            _node(
                f"pilot-{i}",
                "cut",
                "box",
                "cyl",
                {"r": screw.pilot / 2, "h": z + extra},
                (px, py, floor),
            )
        )
        lid.append(
            _node(
                f"clearance-{i}",
                "cut",
                "lid",
                "cyl",
                {"r": screw.clearance / 2, "h": lid_thickness + lip_height + extra},
                (px, py, lip_z),
            )
        )
        if counterbore:
            lid.append(
                _node(
                    f"counterbore-{i}",
                    "cut",
                    "lid",
                    "cyl",
                    {
                        "r": (screw.head_diameter + 0.4) / 2,
                        "h": screw.head_height + extra,
                    },
                    (px, py, rim + lid_thickness - screw.head_height),
                )
            )
    # A later rib can cross an earlier boss; drill after every additive feature.
    box.extend(pilots)
    parts = {
        name: SceneSpec(
            nodes=nodes,
            components=[name],
            meta={
                "units": "mm",
                "enclosure": dict(params),
                "screw_source": screw.source,
            },
        )
        for name, nodes in (("box", box), ("lid", lid))
    }
    return Enclosure(
        params,
        parts,
        tuple(centers),
        screw.pilot,
        screw.clearance,
        screw.head_diameter,
        screw.head_height,
    )


def lid_overlap_mm3(enclosure: Enclosure, *, chord_mm: float = 0.01) -> float:
    """Intersect the produced, seated solids; zero volume means no interference.

    Surface contact at the rim/boss tops is intentional. This checks physical
    interference, not manufacturing accuracy or the chosen process tolerance.
    """
    from precis.cad.fit import tessellate_component

    chord = _number("chord_mm", chord_mm) / 1000
    box = tessellate_component(enclosure.parts["box"], "box", chord)
    lid = tessellate_component(enclosure.parts["lid"], "lid", chord)
    if box is None or lid is None:
        raise ValueError("enclosure generated an empty part")
    return float((box ^ lid).volume()) * 1e9
