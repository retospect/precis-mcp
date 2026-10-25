"""gr514251 — rebuild se:unicycle-c1 with consistent geometry.

Prod's authored envelopes disagree with each other in three places:

  (a) the axle (cyl h0.170, base y +0.085, axis −y) spans |y| ≤ 0.085 —
      it stops 5 mm short of both cranks, whose envelopes cover
      |y| 0.090..0.110. The axle must reach into each crank boss.
  (b) bearing_right mirrors nothing: base y −0.065 with the same +90°
      rotation as bearing_left, so its cylinder spans −0.079..−0.065 and
      lands partly outside fork_right's leg. bearing_left spans
      +0.051..+0.065, seated in fork_left.
  (c) both pedals sit at x +0.170 — a real unicycle's pedals are 180°
      apart about the axle.

This script replays the FULL prod design as one ``put(kind='se')`` ops
list — every block, port, connect, mode, binding, load, measure, BOM
line and note verbatim from prod — with only the defect sites
re-derived from the named parameters:

  * AXLE_HALF = CR_Y + CRANK_T/2 — the axle spans y ±AXLE_HALF, flush
    with each crank slab's outer face (the crank envelopes cover
    |y| CR_Y±CRANK_T/2).
  * bearing_right is bearing_left mirrored across y=0: same |y| seat,
    opposite axial direction (rot −90° vs +90°), so it spans
    −BRG_FACE_Y..−(BRG_FACE_Y−BRG_W) inside fork_right.
  * pedal_right is pedal_left rotated 180° about the wheel axis:
    x −CRANK (vs +CRANK), y −PED_Y (vs +PED_Y), mirrored rotation.
  * Each crank's pinch bolt moves onto the boss ear (x −BOLT_EAR_X in
    the crank frame — the convention the tests/workspaces unicycle
    fixture already uses). Prod authored it at x=0, on the shaft axis:
    a transverse pinch bolt cannot cross the shaft it clamps around,
    and an axle that actually reaches the boss would interpenetrate its
    envelope. The shift is part of making (a) land without new
    undeclared-interpenetration warnings.

The envelope convention matters for every pose below: a ``cyl:rXhY``
block's base sits AT its pose and extends local +z by h; rot=[+90°]
about x maps that axis to −y, rot=[−90°] to +y (verified against prod's
own numbers: the wheel at base y +0.0225 rot +90° spans ±0.0225).

``--broken`` rebuilds prod's CURRENT (defective) geometry verbatim —
same ops shape, the pre-fix literals — so a dev run can print a real
before/after against the fixed build.

Usage:
    scripts/dev uv run --no-sync python scripts/_gr514251_rebuild_unicycle_c1.py
    scripts/dev uv run --no-sync python scripts/_gr514251_rebuild_unicycle_c1.py --broken
    scripts/dev uv run --no-sync python scripts/_gr514251_rebuild_unicycle_c1.py --apply
    scripts/_prod-run uv run python scripts/_gr514251_rebuild_unicycle_c1.py --apply

Dry run by default (read-only): prints the derived geometry table.
``--apply`` writes via ``precis.tools.core.put`` — a full replace of the
same slug, so re-running is idempotent. ``--emit-ops`` dumps the JSON
payload instead. Database URL from ``PRECIS_DATABASE_URL`` (scripts/dev
exports the dev DSN; scripts/_prod-run exports prod's).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

SLUG = "unicycle-c1"
DESCRIPTION = (
    "20-inch-wheel unicycle for an 80 kg rider, designed to be built mostly "
    "on a desktop FDM printer (frame legs, crown, cranks, seatpost head in "
    "fdm/asa) with off-the-shelf ball bearings, ISO fasteners, and a handful "
    "of purchased bicycle subassemblies (wheel, saddle, pedals) that are "
    "cheaper and stronger bought than printed. Load path: rider weight -> "
    "saddle -> seatpost (stock aluminium tube) -> crown -> two fork legs -> "
    "two 6204-2RS bearings -> keyed axle -> wheel hub -> tire contact patch. "
    "Pedalling path: pedal -> crank (fdm/asa, pinch-clamped to the axle) -> "
    "axle -> wheel, both cranks and the wheel rotating together as one rigid "
    "group (no freewheel, as on a real unicycle) while the two bearings let "
    "that group spin inside the stationary fork."
)

# ── named parameters (SI metres) ─────────────────────────────────────
#: axle height above ground = wheel radius; all block z poses hang off it.
Z0 = 0.254
FORK_X = -0.015  # fork legs sit 15 mm ahead of the wheel axis (caster)
FORK_Y = 0.050  # fork leg centre |y|
FORK_D = (
    0.045  # fork leg slab thickness — seat check: BRG_FACE_Y ± within ±FORK_Y±FORK_D/2
)
BRG_FACE_Y = 0.065  # bearing outboard face |y|
BRG_W = 0.014  # 6204-2RS width
BRG_R = 0.0235  # 6204-2RS outer radius (47 mm OD)
CR_Y = 0.100  # crank boss centre plane |y|
CRANK_T = 0.020  # crank arm slab thickness (d0.02)
CRANK_L = 0.170  # axle axis → pedal spindle offset
PED_Y = 0.130  # pedal spindle base |y|
PED_R = 0.007
PED_H = 0.090
AXLE_R = 0.010  # Ø20 axle
BOLT_EAR_X = -0.016  # crank pinch bolt off the bore axis, through the boss ear

#: The axle's base sits at +AXLE_HALF and its cylinder extends −y, so the
#: rod spans |y| ≤ AXLE_HALF. Reaching INTO each crank means overlapping
#: the crank slab |y| CR_Y±CRANK_T/2 — take the half-length to the
#: slab's outer face so the shaft fills the whole pinch boss.
AXLE_HALF = CR_Y + CRANK_T / 2  # 0.110

ROTM = [1.5707963267948966, 0.0, 0.0]  # +90° about x: cyl axis maps +z → −y
ROTP = [-1.5707963267948966, 0.0, 0.0]  # −90° about x: cyl axis maps +z → +y
FLIP = [3.14159265, 0.0, 0.0]  # prod's stored π approximation, verbatim

# ── the defect sites, prod values verbatim (for --broken) ────────────
_BROKEN = {
    "axle_pose_y": 0.085,
    "axle_h": 0.170,
    "bearing_right_rot": ROTM,
    "pedal_right_x": CRANK_L,
    "pedal_right_rot": ROTM,
    "crank_bolt_x": 0.0,
}


def _vec(v: list[float] | None, unit: str) -> str | None:
    """Canonical trailing-unit vector; an all-zero pose/rot is the op default."""
    if v is None or not any(v):
        return None
    return f"[{' '.join(format(c, '.17g') for c in v)}] {unit}"


def _blocks(*, broken: bool) -> list[dict[str, Any]]:
    """The 20 prod blocks, dependency-ordered (parent before child).

    (name, parent, pose, rot, envelope, desc, use, mode, bound_design)
    — bound_design None means no set_binding op. Only the three defect
    sites (and the crank-bolt ear shift (a) forces) differ between the
    ``broken`` (prod-verbatim) and fixed builds.
    """
    b = (
        _BROKEN
        if broken
        else {
            "axle_pose_y": AXLE_HALF,
            "axle_h": 2 * AXLE_HALF,
            "bearing_right_rot": ROTP,
            "pedal_right_x": -CRANK_L,
            "pedal_right_rot": ROTP,
            "crank_bolt_x": BOLT_EAR_X,
        }
    )
    rows = [
        (
            "unicycle",
            None,
            [0, 0, 0],
            None,
            "box:w0.55d0.45h1.05",
            "20-inch desktop-FDM unicycle, 80 kg rider capacity.",
            "personal transport / recreational unicycling, light indoor+outdoor use",
            None,
            None,
        ),
        (
            "wheel",
            "unicycle",
            [0, 0.0225, Z0],
            ROTM,
            "cyl:r0.254h0.045",
            "20 in (508 mm OD) wheel: rim + spokes + tire, bought pre-built.",
            "rolling contact with the ground; keyed to the axle",
            "purchase",
            "wheel-20in-assembly",
        ),
        (
            "axle",
            "unicycle",
            [0, b["axle_pose_y"], Z0],
            ROTM,
            f"cyl:r{AXLE_R}h{b['axle_h']}",
            "20 mm dia steel axle, turned from stock round bar with flats at "
            "both ends for the crank clamps; keyed to the wheel hub; runs "
            "through the two fork bearings.",
            "carries wheel + crank torque; located by two ball bearings in the fork legs",
            "stock-cut/steel-1045",
            None,
        ),
        (
            "bearing_left",
            "unicycle",
            [0, BRG_FACE_Y, Z0],
            ROTM,
            f"cyl:r{BRG_R}h{BRG_W}",
            "6204-2RS deep-groove ball bearing, left side.",
            "locates axle in fork_left, carries radial load",
            "purchase",
            "bearing-6204-2rs",
        ),
        (
            "bearing_right",
            "unicycle",
            [0, -BRG_FACE_Y, Z0],
            b["bearing_right_rot"],
            f"cyl:r{BRG_R}h{BRG_W}",
            "6204-2RS deep-groove ball bearing, right side.",
            "locates axle in fork_right, carries radial load",
            "purchase",
            "bearing-6204-2rs",
        ),
        (
            "fork_left",
            "unicycle",
            [FORK_X, FORK_Y, Z0],
            None,
            "box:w0.030d0.045h0.42",
            "Left fork leg, fdm/asa: bearing housing at the bottom "
            "(press-fits bearing_left's 47 mm OD), rises to a bolted "
            "flange at the crown.",
            "carries wheel-side bearing load up to the crown",
            "fdm/asa",
            None,
        ),
        (
            "fork_right",
            "unicycle",
            [FORK_X, -FORK_Y, Z0],
            None,
            "box:w0.030d0.045h0.42",
            "Right fork leg, fdm/asa, mirror of fork_left.",
            "carries wheel-side bearing load up to the crown",
            "fdm/asa",
            None,
        ),
        (
            "crown",
            "unicycle",
            [FORK_X, 0, 0.674],
            None,
            "box:w0.045d0.13h0.06",
            "Crown, fdm/asa: bolts both fork legs together and carries the "
            "seat-tube socket with an integral pinch collar for "
            "seat-height clamping.",
            "joins fork_left + fork_right; hosts the seatpost socket",
            "fdm/asa",
            None,
        ),
        (
            "flange_bolt_left",
            "crown",
            [0, FORK_Y, 0.016],
            FLIP,
            "cyl:r0.0025h0.016",
            "Crown-to-fork_left flange bolt (x4 in the real pattern; one "
            "instance modelled, BOM carries the count).",
            None,
            "purchase",
            "iso-4762-m5x16",
        ),
        (
            "flange_bolt_right",
            "crown",
            [0, -FORK_Y, 0.016],
            FLIP,
            "cyl:r0.0025h0.016",
            "Crown-to-fork_right flange bolt (x4 in the real pattern; one "
            "instance modelled).",
            None,
            "purchase",
            "iso-4762-m5x16",
        ),
        (
            "seatpost",
            "unicycle",
            [FORK_X, 0, 0.704],
            None,
            "cyl:r0.0135h0.30",
            "27.2 mm OD aluminium 6061 tube, cut to length from stock round "
            "tube; slides in the crown's seat-tube socket, height locked "
            "by seatpost_clamp_bolt.",
            "adjustable-height rider support",
            "stock-cut/aluminium-6061",
            None,
        ),
        (
            "seatpost_clamp_bolt",
            "crown",
            [0, 0.07, 0.03],
            ROTM,
            "cyl:r0.003h0.03",
            "Pinch bolt closing the crown's integral collar around the "
            "seatpost tube to lock ride height.",
            None,
            "purchase",
            "iso-4762-m6x30",
        ),
        (
            "seatpost_head",
            "unicycle",
            [FORK_X, 0, 1.004],
            None,
            "box:w0.10d0.24h0.03",
            "fdm/asa saddle cradle, press-fit + pinned onto the seatpost "
            "top, clamps the saddle's steel rails.",
            "adapts round seatpost to bicycle saddle rails",
            "fdm/asa",
            None,
        ),
        (
            "saddle",
            "unicycle",
            [FORK_X, 0, 1.034],
            None,
            "box:w0.10d0.26h0.05",
            "Standard bicycle saddle, bought, 7 mm steel rails.",
            "rider contact surface",
            "purchase",
            "saddle-bicycle",
        ),
        (
            "crank_left",
            "unicycle",
            [0, CR_Y, Z0],
            None,
            f"box:w{CRANK_L}d{CRANK_T}h0.03",
            "fdm/asa crank arm, split-boss pinch-clamped onto the axle's keyed flat.",
            "converts pedal force to axle torque",
            "fdm/asa",
            None,
        ),
        (
            "crank_bolt_left",
            "crank_left",
            [b["crank_bolt_x"], 0, 0.016],
            FLIP,
            "cyl:r0.002h0.02",
            "Pinch bolt closing crank_left's split boss onto the axle.",
            None,
            "purchase",
            "iso-4762-m4x20",
        ),
        (
            "pedal_left",
            "unicycle",
            [CRANK_L, PED_Y, Z0],
            ROTM,
            f"cyl:r{PED_R}h{PED_H}",
            "Bicycle pedal, bought, 9/16 in threaded spindle with sealed bearing.",
            "rider foot contact; spins on its own spindle bearing",
            "purchase",
            "pedal-9-16",
        ),
        (
            "crank_right",
            "unicycle",
            [0, -CR_Y, Z0],
            None,
            f"box:w{CRANK_L}d{CRANK_T}h0.03",
            "fdm/asa crank arm, mirror of crank_left.",
            "converts pedal force to axle torque",
            "fdm/asa",
            None,
        ),
        (
            "crank_bolt_right",
            "crank_right",
            [b["crank_bolt_x"], 0, 0.016],
            FLIP,
            "cyl:r0.002h0.02",
            "Pinch bolt closing crank_right's split boss onto the axle.",
            None,
            "purchase",
            "iso-4762-m4x20",
        ),
        (
            "pedal_right",
            "unicycle",
            [b["pedal_right_x"], -PED_Y, Z0],
            b["pedal_right_rot"],
            f"cyl:r{PED_R}h{PED_H}",
            "Bicycle pedal, bought, mirror of pedal_left.",
            "rider foot contact; spins on its own spindle bearing",
            "purchase",
            "pedal-9-16",
        ),
    ]
    ops: list[dict[str, Any]] = []
    for name, parent, pose, rot, env, desc, use, mode, bound in rows:
        op: dict[str, Any] = {"op": "add_block", "name": name}
        if parent is not None:
            op["parent"] = parent
        if (v := _vec(pose, "m")) is not None:
            op["pose"] = v
        if (v := _vec(rot, "rad")) is not None:
            op["rot"] = v
        op["envelope"] = env
        op["desc"] = desc
        if use is not None:
            op["use"] = use
        ops.append(op)
    return ops


def _ports() -> list[dict[str, Any]]:
    """The 35 prod ports, in block order (verbatim from prod)."""
    rows: list[tuple[str, str, list[str] | None, list[float] | None]] = [
        ("wheel", "hub", ["keyed"], None),
        ("axle", "hub", ["keyed"], None),
        ("axle", "brg_l", ["bearing_seat"], None),
        ("axle", "brg_r", ["bearing_seat"], None),
        ("axle", "end_l", ["crank_mount"], None),
        ("axle", "end_r", ["crank_mount"], None),
        ("bearing_left", "bore", None, None),
        ("bearing_left", "seat", None, None),
        ("bearing_right", "bore", None, None),
        ("bearing_right", "seat", None, None),
        ("fork_left", "bearing_seat", None, None),
        ("fork_left", "crown_flange", None, [0, 0, 0.42]),
        ("fork_right", "bearing_seat", None, None),
        ("fork_right", "crown_flange", None, [0, 0, 0.42]),
        ("crown", "left_socket", None, None),
        ("crown", "right_socket", None, None),
        ("crown", "seat_tube", None, None),
        ("flange_bolt_left", "thread", None, None),
        ("flange_bolt_right", "thread", None, None),
        ("seatpost", "tube", None, None),
        ("seatpost", "top", None, [0, 0, 0.3]),
        ("seatpost_clamp_bolt", "thread", None, None),
        ("seatpost_head", "base", None, None),
        ("seatpost_head", "rail_clamp", None, None),
        ("saddle", "rails", None, None),
        ("crank_left", "axle_bore", None, None),
        ("crank_left", "clamp", None, None),
        ("crank_left", "pedal_mount", None, None),
        ("crank_bolt_left", "thread", None, None),
        ("pedal_left", "spindle", None, None),
        ("crank_right", "axle_bore", None, None),
        ("crank_right", "clamp", None, None),
        ("crank_right", "pedal_mount", None, None),
        ("crank_bolt_right", "thread", None, None),
        ("pedal_right", "spindle", None, None),
    ]
    ops = []
    for block, name, roles, pose in rows:
        op: dict[str, Any] = {"op": "add_port", "block": block, "name": name}
        if roles:
            op["roles"] = roles
        if (v := _vec(pose, "m")) is not None:
            op["pose"] = v
        ops.append(op)
    return ops


def _connects() -> list[dict[str, Any]]:
    """The 17 prod connects (verbatim joints)."""
    key = {"class": "rigid", "mechanism": "key"}
    press = {"class": "rigid", "mechanism": "press"}
    brg = {"axis": [0.0, 1.0, 0.0], "class": "revolute", "mechanism": "bearing"}
    screw_nt = {
        "class": "rigid",
        "mechanism": "screw",
        "params": {"counterbore": True, "thread_strategy": "nut-trap"},
    }
    screw_nt2 = {
        "class": "rigid",
        "mechanism": "screw",
        "params": {"thread_strategy": "nut-trap"},
    }
    screw_ins = {
        "class": "rigid",
        "mechanism": "screw",
        "params": {"thread_strategy": "insert"},
    }
    prism = {
        "axis": [0.0, 0.0, 1.0],
        "class": "prismatic",
        "mechanism": "press",
    }
    return [
        {"op": "connect", "a": "axle.hub", "b": "wheel.hub", "joint": key},
        {"op": "connect", "a": "axle.brg_l", "b": "bearing_left.bore", "joint": brg},
        {"op": "connect", "a": "axle.brg_r", "b": "bearing_right.bore", "joint": brg},
        {
            "op": "connect",
            "a": "bearing_left.seat",
            "b": "fork_left.bearing_seat",
            "joint": press,
        },
        {
            "op": "connect",
            "a": "bearing_right.seat",
            "b": "fork_right.bearing_seat",
            "joint": press,
        },
        {
            "op": "connect",
            "a": "flange_bolt_left.thread",
            "b": "fork_left.crown_flange",
            "joint": screw_nt,
        },
        {
            "op": "connect",
            "a": "flange_bolt_right.thread",
            "b": "fork_right.crown_flange",
            "joint": screw_nt,
        },
        {
            "op": "connect",
            "a": "crown.seat_tube",
            "b": "seatpost.tube",
            "joint": prism,
        },
        {
            "op": "connect",
            "a": "seatpost.tube",
            "b": "seatpost_clamp_bolt.thread",
            "joint": screw_nt2,
        },
        {
            "op": "connect",
            "a": "seatpost.top",
            "b": "seatpost_head.base",
            "joint": press,
        },
        {
            "op": "connect",
            "a": "saddle.rails",
            "b": "seatpost_head.rail_clamp",
            "joint": press,
        },
        {
            "op": "connect",
            "a": "axle.end_l",
            "b": "crank_left.axle_bore",
            "joint": press,
        },
        {
            "op": "connect",
            "a": "crank_bolt_left.thread",
            "b": "crank_left.clamp",
            "joint": screw_ins,
        },
        {
            "op": "connect",
            "a": "crank_left.pedal_mount",
            "b": "pedal_left.spindle",
            "joint": brg,
        },
        {
            "op": "connect",
            "a": "axle.end_r",
            "b": "crank_right.axle_bore",
            "joint": press,
        },
        {
            "op": "connect",
            "a": "crank_bolt_right.thread",
            "b": "crank_right.clamp",
            "joint": screw_ins,
        },
        {
            "op": "connect",
            "a": "crank_right.pedal_mount",
            "b": "pedal_right.spindle",
            "joint": brg,
        },
    ]


#: Per-block facets in _block_order — (name, mode, bound_design, load).
#: mode/bindings/loads verbatim from prod se_blocks.
_FACETS: list[tuple[str, str | None, str | None, dict[str, Any] | None]] = [
    ("unicycle", None, None, None),
    ("wheel", "purchase", "wheel-20in-assembly", {"fixed": ["x", "y", "z"]}),
    ("axle", "stock-cut/steel-1045", None, None),
    ("bearing_left", "purchase", "bearing-6204-2rs", None),
    ("bearing_right", "purchase", "bearing-6204-2rs", None),
    ("fork_left", "fdm/asa", None, None),
    ("fork_right", "fdm/asa", None, None),
    ("crown", "fdm/asa", None, None),
    ("flange_bolt_left", "purchase", "iso-4762-m5x16", None),
    ("flange_bolt_right", "purchase", "iso-4762-m5x16", None),
    ("seatpost", "stock-cut/aluminium-6061", None, None),
    ("seatpost_clamp_bolt", "purchase", "iso-4762-m6x30", None),
    ("seatpost_head", "fdm/asa", None, None),
    ("saddle", "purchase", "saddle-bicycle", {"force": [0.0, 0.0, -1800.0]}),
    ("crank_left", "fdm/asa", None, None),
    ("crank_bolt_left", "purchase", "iso-4762-m4x20", None),
    ("pedal_left", "purchase", "pedal-9-16", {"force": [0.0, 0.0, -900.0]}),
    ("crank_right", "fdm/asa", None, None),
    ("crank_bolt_right", "purchase", "iso-4762-m4x20", None),
    ("pedal_right", "purchase", "pedal-9-16", None),
]


def _facet_ops() -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for name, mode, bound, load in _FACETS:
        if mode is not None:
            ops.append({"op": "set_mode", "block": name, "mode": mode})
        if bound is not None:
            ops.append(
                {
                    "op": "set_binding",
                    "block": name,
                    "kind": "component",
                    "design": bound,
                }
            )
        if load is not None:
            ops.append({"op": "set_load", "block": name, **load})
    return ops


def _ledger_ops() -> list[dict[str, Any]]:
    """Prod's measures, BOM rows and notes — verbatim."""
    ops: list[dict[str, Any]] = [
        {
            "op": "add_measure",
            "block": "wheel",
            "name": "diameter",
            "value": 0.508,
            "strength": "hard",
            "reason": "20-inch wheel per requirement",
            "origin": "user",
            "unit": "m",
        },
        {
            "op": "add_measure",
            "block": "bearing_left",
            "name": "bore",
            "value": 0.02,
            "strength": "hard",
            "reason": "6204 bearing bore must match the 20 mm axle",
            "origin": "user",
            "unit": "m",
        },
        {
            "op": "add_measure",
            "block": "bearing_right",
            "name": "bore",
            "value": 0.02,
            "strength": "hard",
            "reason": "6204 bearing bore must match the 20 mm axle",
            "origin": "user",
            "unit": "m",
        },
        {
            "op": "add_measure",
            "block": "unicycle",
            "name": "seat_height",
            "min": 0.75,
            "max": 0.95,
            "strength": "soft",
            "reason": "adjustable seatpost range for riders of different heights",
            "origin": "user",
            "unit": "m",
        },
        {
            "op": "add_bom",
            "item_kind": "component",
            "item": "iso-7040-m5",
            "qty": 1,
            "block": "flange_bolt_left",
            "reason": "captured nyloc nut for the nut-trap",
        },
        {
            "op": "add_bom",
            "item_kind": "component",
            "item": "iso-7040-m5",
            "qty": 1,
            "block": "flange_bolt_right",
            "reason": "captured nyloc nut for the nut-trap",
        },
        {
            "op": "add_bom",
            "item_kind": "component",
            "item": "iso-7040-m6",
            "qty": 1,
            "block": "seatpost_clamp_bolt",
            "reason": "captured nyloc nut for the collar nut-trap",
        },
        {
            "op": "add_bom",
            "item_kind": "component",
            "item": "insert-brass-heatset-m4",
            "qty": 1,
            "block": "crank_bolt_left",
            "reason": "heat-set insert in the printed clamp boss",
        },
        {
            "op": "add_bom",
            "item_kind": "component",
            "item": "insert-brass-heatset-m4",
            "qty": 1,
            "block": "crank_bolt_right",
            "reason": "heat-set insert in the printed clamp boss",
        },
        {
            "op": "add_note",
            "name": "flange_bolt_left_qty",
            "kind": "decision",
            "text": "Modelled as one bolt per leg for the fasten/DRC checks; "
            "the real flange uses a 4-bolt pattern per leg (BOM should be "
            "scaled x4 at detailing time).",
            "about": ["flange_bolt_left"],
            "origin": "user",
        },
        {
            "op": "add_note",
            "name": "seatpost_head_saddle_fastening_scope",
            "kind": "decision",
            "text": "seatpost-to-seatpost_head and seatpost_head-to-saddle "
            "are declared as plain rigid press joints, not run through the "
            "screw/fasten pass (each is a lower-stakes secondary joint vs. "
            "the flange and crank clamps, which do carry full fastener "
            "modelling). Real build: a transverse M4 roll pin or bolt "
            "through seatpost_head+tube, and the seatpost_head's own "
            "rail-clamp bolts (bicycle seatpost-head hardware) for the "
            "saddle.",
            "about": ["seatpost_head", "saddle"],
            "origin": "user",
        },
        {
            "op": "add_note",
            "name": "rider_load_basis",
            "kind": "decision",
            "text": "Saddle load 1800 N = 80 kg x 9.81 m/s^2 x ~2.3 dynamic "
            "factor for hopping/curb impacts (house engineering margin, not "
            "from a cited standard). Pedal load 900 N at one pedal models a "
            "single-leg-stand worst case. Ground reaction is taken as a "
            "full fixed support at the wheel block rather than a rolling "
            "contact-patch model.",
            "about": ["saddle", "pedal_left", "wheel"],
            "origin": "user",
        },
        {
            "op": "add_note",
            "name": "fork_leg_print_split",
            "kind": "decision",
            "text": "fork_left/fork_right are modelled as single 0.42 m "
            "blocks, but that exceeds the Z travel of most desktop FDM "
            "printers (typically 0.25-0.30 m). A real build must split "
            "each leg into two segments joined by a bolted flange (same "
            "nut-trap treatment as the crown flange), or print it "
            "diagonally if the bed diagonal allows — not modelled here; "
            "scope-trimmed to keep the block count focused on the "
            "wheel/bearing/crank load path.",
            "about": ["fork_left", "fork_right"],
            "origin": "user",
        },
        {
            "op": "add_note",
            "name": "wheel_opaque_purchase",
            "kind": "decision",
            "text": "wheel is modelled as one opaque purchased assembly "
            "(rim+spokes+tire), unlike the prod unicycle-mk2 worked example "
            "which lays out a 12-spoke tension truss as individual axial "
            "members and checks it with view=stability. That means "
            "view=stability here has no axial subgraph to analyse inside "
            "the wheel; spoke tensioning is the wheel vendor's problem, not "
            "this design's. A from-scratch-spoked variant would follow "
            "mk2's pattern instead.",
            "about": ["wheel"],
            "origin": "user",
        },
    ]
    return ops


def build_ops(*, broken: bool = False) -> list[dict[str, Any]]:
    """The full replayable ops list for se:unicycle-c1.

    ``broken=True`` reproduces prod's current (defective) geometry —
    same op shapes with the pre-fix literals — for a before/after
    comparison on a dev database.
    """
    return (
        _blocks(broken=broken) + _ports() + _connects() + _facet_ops() + _ledger_ops()
    )


def payload(*, broken: bool = False) -> dict[str, Any]:
    return {"description": DESCRIPTION, "ops": build_ops(broken=broken)}


def _report(broken: bool) -> str:
    """Dry-run table: every block's y-span (world frame, mm) + the three
    defect sites' numbers, from the ops alone (no database)."""
    from precis.cad import dsl as cad_dsl
    from precis.cad.bulk import expr_aabb
    from precis.cad.graph import Design as CadDesign
    from precis.cad.vec import as_vec3 as cad_as_vec3
    from precis.cad.vec import pose as cad_pose
    from precis_se.ops import SeTree, apply_ops

    tree = apply_ops(SeTree(), build_ops(broken=broken))
    design = CadDesign()
    lines = [
        f"{'block':<22} {'x':>18} {'y':>18} {'z':>18}",
        "-" * 80,
    ]
    spans: dict[str, Any] = {}
    for name, node in tree.blocks.items():
        if not node.envelope:
            lines.append(f"{name:<22} (no envelope)")
            continue
        spec = cad_dsl.parse(node.envelope)
        prim = cad_dsl.build(spec)
        xform = cad_pose(
            cad_as_vec3([float(c) for c in node.pose]), cad_as_vec3(node.rot)
        )
        design.add_component(name, design.prim(name, prim, xform))
        lo, hi = expr_aabb(design, design.components[name])
        spans[name] = (lo, hi)
        lines.append(
            f"{name:<22} "
            f"{lo[0] * 1e3:>8.1f}..{hi[0] * 1e3:<8.1f} "
            f"{lo[1] * 1e3:>8.1f}..{hi[1] * 1e3:<8.1f} "
            f"{lo[2] * 1e3:>8.1f}..{hi[2] * 1e3:<8.1f}"
        )
    checks = [
        "",
        f"AXLE_HALF = CR_Y({CR_Y}) + CRANK_T/2({CRANK_T / 2}) = {AXLE_HALF} "
        f"→ axle spans y ±{AXLE_HALF * 1e3:.0f} mm",
        f"crank slab |y|: {CR_Y - CRANK_T / 2:.3f}..{CR_Y + CRANK_T / 2:.3f} m "
        "(axle reaches into both cranks)"
        if not broken
        else f"crank slab |y|: {CR_Y - CRANK_T / 2:.3f}..{CR_Y + CRANK_T / 2:.3f} m "
        "(axle stops short — the bug)",
        f"bearings: left {spans['bearing_left'][0][1] * 1e3:.1f}.."
        f"{spans['bearing_left'][1][1] * 1e3:.1f} mm, "
        f"right {spans['bearing_right'][0][1] * 1e3:.1f}.."
        f"{spans['bearing_right'][1][1] * 1e3:.1f} mm",
        f"pedals: left x={tree.blocks['pedal_left'].pose[0] * 1e3:.0f} mm, "
        f"right x={tree.blocks['pedal_right'].pose[0] * 1e3:.0f} mm",
    ]
    return "\n".join(lines + checks)


def _print_tool_result(result: Any) -> None:
    if hasattr(result, "content"):
        print(result.content[0].text)
    else:
        print(result)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--apply", action="store_true", help="put() the design (full replace)"
    )
    p.add_argument(
        "--broken",
        action="store_true",
        help="rebuild prod's current defective geometry (before-snapshot)",
    )
    p.add_argument(
        "--emit-ops",
        action="store_true",
        help="print the put payload JSON instead of the geometry table",
    )
    p.add_argument("--id", default=SLUG, help="target design slug")
    args = p.parse_args()

    if args.emit_ops:
        print(json.dumps(payload(broken=args.broken)))
        return

    if not args.apply:
        print(_report(args.broken))
        print("\ndry run — pass --apply to write, --emit-ops for the payload")
        return

    if not os.environ.get("PRECIS_DATABASE_URL"):
        sys.exit("PRECIS_DATABASE_URL is not set")
    from precis.tools import core

    _print_tool_result(
        core.put(kind="se", id=args.id, text=json.dumps(payload(broken=args.broken)))
    )
    for view in ("validate", "clearance"):
        print(f"\n{'=' * 20} view={view} {'=' * 20}")
        _print_tool_result(core.get(kind="se", id=args.id, view=view))


if __name__ == "__main__":
    main()
