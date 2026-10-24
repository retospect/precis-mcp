"""Unicycle (c1 re-build) as a workspace script.

Run:  uv run python scratch/unicycle/main.py [key=value ...]
Writes scratch/out/unicycle/{manifest.json,trace.json,blocks/*.cad}.
All lengths mm, +z up, wheel axis along y.
"""

from __future__ import annotations

import sys

from precis_geom import out

P = {
    "wheel_in": 20.0,  # wheel diameter, inches (tire OD)
    "wheel_w": 45.0,
    "crank": 170.0,  # axle -> pedal spindle
    "fork_above": 420.0,  # axle height -> fork top / crown base
    "fork_y": 50.0,  # fork leg centre offset
    "seat_len": 300.0,
    "seat_insert": 30.0,
    "axle_half": 110.0,
    "debug_bad": 0,
}
for a in sys.argv[1:]:
    k, v = a.split("=")
    P[k] = float(v)

R = P["wheel_in"] * 25.4 / 2  # wheel radius
Z0 = R  # axle height above ground
FY = P["fork_y"]
BRG_Y = 65.0
CR_Y = 100.0  # crank centre plane |y|
PED_LEN = 90.0


def part(o, name, src, ports, desc):
    """One template block: DSL source + ports."""
    o.block(name, src, ports=ports, desc=desc)


def fork(o):
    h = P["fork_above"] + 40
    src = f"""desc: fork leg
component fork
leg    add box:w30mmd45mmh{h}mm
house  add cyl:r30mmh45mm @0mm,22.5mm,40mm rot:90deg,0deg,0deg
bore   cut cyl:r23.5mmh50mm @0mm,25mm,40mm rot:90deg,0deg,0deg
"""
    part(
        o,
        "fork",
        src,
        [
            {"name": "bearing_seat", "roles": ["bearing_seat"], "pose": "[0 0 40] mm"},
            {
                "name": "crown_flange",
                "roles": ["flange"],
                "direction": [0, 0, 1],
                "pose": f"[0 0 {h}] mm",
            },
        ],
        "fdm/asa fork leg, bearing housing at bottom",
    )


def crown(o):
    src = """desc: crown
component crown
body   add box:w45mmd130mmh60mm
sock   cut cyl:r13.75mmh64mm @0mm,0mm,-2mm
"""
    part(
        o,
        "crown",
        src,
        [
            {"name": "left_socket", "roles": ["flange"], "pose": f"[0 {FY} 0] mm"},
            {"name": "right_socket", "roles": ["flange"], "pose": f"[0 {-FY} 0] mm"},
            {"name": "seat_tube", "roles": ["socket"], "pose": "[0 0 30] mm"},
            {"name": "clamp", "roles": ["clamp"], "pose": "[0 20 30] mm"},
        ],
        "crown joining the fork legs, seat-tube socket",
    )


def crank(o):
    L = P["crank"]
    src = f"""desc: crank arm
component crank
boss   add cyl:r18mmh20mm @0mm,10mm,0mm rot:90deg,0deg,0deg
arm    add box:w{L}mmd20mmh30mm @{L / 2}mm,0mm,-15mm
bore   cut cyl:r10mmh24mm @0mm,12mm,0mm rot:90deg,0deg,0deg
pedal  cut cyl:r6mmh24mm @{L}mm,12mm,0mm rot:90deg,0deg,0deg
"""
    part(
        o,
        "crank",
        src,
        [
            {"name": "axle_bore", "roles": ["crank_mount"], "pose": "[0 0 0] mm"},
            {"name": "clamp", "roles": ["clamp"], "pose": "[-16 0 0] mm"},
            {"name": "pedal_mount", "roles": ["pedal"], "pose": f"[{L} 0 0] mm"},
        ],
        "fdm/asa crank, split boss pinch-clamped to axle",
    )


def bolt(o, name, r, h):
    src = f"""desc: {name}
component {name}
shank  add cyl:r{r}mmh{h}mm
head   add cyl:r{r * 1.8}mmh{r * 1.4}mm @0mm,0mm,{h}mm
"""
    part(
        o,
        name,
        src,
        [{"name": "thread", "roles": ["thread"], "direction": [0, 0, -1]}],
        f"ISO 4762 bolt r{r}",
    )


def axle(o):
    hl = P["axle_half"]
    src = f"""desc: axle
component axle
rod add cyl:r10mmh{2 * hl}mm @0mm,{hl}mm,0mm rot:90deg,0deg,0deg
"""
    part(
        o,
        "axle",
        src,
        [
            {"name": "hub", "roles": ["keyed"]},
            {
                "name": "brg_l",
                "roles": ["bearing_seat"],
                "pose": f"[0 {BRG_Y - 7} 0] mm",
            },
            {
                "name": "brg_r",
                "roles": ["bearing_seat"],
                "pose": f"[0 {-BRG_Y + 7} 0] mm",
            },
            {"name": "end_l", "roles": ["crank_mount"], "pose": f"[0 {CR_Y} 0] mm"},
            {"name": "end_r", "roles": ["crank_mount"], "pose": f"[0 {-CR_Y} 0] mm"},
        ],
        "20 mm steel axle",
    )


def bearing(o):
    src = """desc: 6204-2RS
component bearing
ring add cyl:r23.5mmh14mm @0mm,7mm,0mm rot:90deg,0deg,0deg
hole cut cyl:r10mmh16mm @0mm,8mm,0mm rot:90deg,0deg,0deg
"""
    part(
        o,
        "bearing",
        src,
        [{"name": "bore", "roles": ["bore"]}, {"name": "seat", "roles": ["seat"]}],
        "6204-2RS deep-groove ball bearing",
    )


def wheel(o):
    w = P["wheel_w"]
    src = f"""desc: wheel
component wheel
tire   add cyl:r{R}mmh{w}mm @0mm,{w / 2}mm,0mm rot:90deg,0deg,0deg
inner  cut cyl:r{R - 14}mmh{w + 2}mm @0mm,{w / 2 + 1}mm,0mm rot:90deg,0deg,0deg
hub    add cyl:r25mmh{w}mm @0mm,{w / 2}mm,0mm rot:90deg,0deg,0deg
spokes add box:w{R - 14}mmd3mmh3mm @{(R - 14) / 2 + 12}mm,0mm,0mm polar:n12r0mm
bore   cut cyl:r10mmh{w + 2}mm @0mm,{w / 2 + 1}mm,0mm rot:90deg,0deg,0deg
"""
    part(
        o,
        "wheel",
        src,
        [{"name": "hub", "roles": ["keyed"]}],
        "20in wheel, rim+spokes+tire, bought",
    )


def pedal(o):
    src = f"""desc: pedal
component pedal
spindle add cyl:r7mmh{PED_LEN}mm
"""
    part(o, "pedal", src, [{"name": "spindle", "roles": ["spindle"]}], "bicycle pedal")


def seat(o):
    L = P["seat_len"]
    part(
        o,
        "seatpost",
        f"desc: seatpost\ncomponent seatpost\ntube add cyl:r13.5mmh{L}mm\n",
        [
            {"name": "tube", "roles": ["tube"], "pose": f"[0 0 {P['seat_insert']}] mm"},
            {"name": "top", "roles": ["top"], "pose": f"[0 0 {L}] mm"},
        ],
        "27.2 tube",
    )
    part(
        o,
        "seatpost_head",
        "desc: head\ncomponent head\nb add box:w100mmd240mmh30mm\n",
        [
            {"name": "base", "roles": ["base"]},
            {"name": "rail_clamp", "roles": ["clamp"], "pose": "[0 0 30] mm"},
        ],
        "saddle cradle",
    )
    part(
        o,
        "saddle",
        "desc: saddle\ncomponent saddle\nb add box:w100mmd260mmh50mm\n",
        [{"name": "rails", "roles": ["rails"]}],
        "saddle, bought",
    )


def build(o):
    fork(o)
    crown(o)
    crank(o)
    bolt(o, "bolt_m4", 2.0, 20.0)
    bolt(o, "bolt_m5", 2.5, 16.0)
    bolt(o, "bolt_m6", 3.0, 30.0)
    axle(o)
    bearing(o)
    wheel(o)
    pedal(o)
    seat(o)

    ct = Z0 + P["fork_above"]  # crown base z
    inst = o.instance
    # instances: (name, template, parent, pose, rot)
    inst("wheel", template="wheel", pose=f"[0 0 {Z0}] mm")
    inst("axle", template="axle", pose=f"[0 0 {Z0}] mm")
    inst("bearing_left", template="bearing", pose=f"[0 {BRG_Y - 14} {Z0}] mm")
    inst(
        "bearing_right",
        template="bearing",
        pose=f"[0 {-BRG_Y + 14} {Z0}] mm",
        rot="[0 0 180] deg",
    )
    inst("fork_left", template="fork", pose=f"[0 {FY} {Z0 - 40}] mm")
    inst(
        "fork_right",
        template="fork",
        pose=f"[0 {-FY} {Z0 - 40}] mm",
        rot="[0 0 180] deg",
    )
    inst("crown", template="crown", pose=f"[0 0 {ct}] mm")
    inst(
        "flange_bolt_left",
        template="bolt_m5",
        parent="crown",
        pose=f"[0 {FY} 12] mm",
        rot="[180 0 0] deg",
    )
    inst(
        "flange_bolt_right",
        template="bolt_m5",
        parent="crown",
        pose=f"[0 {-FY} 12] mm",
        rot="[180 0 0] deg",
    )
    inst(
        "seatpost_clamp_bolt",
        template="bolt_m6",
        parent="crown",
        pose="[-20 20 30] mm",
        rot="[0 90 0] deg",
    )
    inst("crank_left", template="crank", pose=f"[0 {CR_Y} {Z0}] mm")
    inst(
        "crank_right",
        template="crank",
        pose=f"[0 {-CR_Y} {Z0}] mm",
        rot="[0 0 180] deg",
    )
    inst(
        "crank_bolt_left",
        template="bolt_m4",
        parent="crank_left",
        pose="[-16 0 20] mm",
        rot="[180 0 0] deg",
    )
    inst(
        "crank_bolt_right",
        template="bolt_m4",
        parent="crank_right",
        pose="[-16 0 20] mm",
        rot="[180 0 0] deg",
    )
    inst(
        "pedal_left",
        template="pedal",
        pose=f"[{P['crank']} {CR_Y - 10} {Z0}] mm",
        rot="[-90 0 0] deg",
    )
    inst(
        "pedal_right",
        template="pedal",
        pose=f"[{-P['crank']} {-CR_Y + 10} {Z0}] mm",
        rot="[90 0 0] deg",
    )
    inst("seatpost", template="seatpost", pose=f"[0 0 {ct + 60 - P['seat_insert']}] mm")
    top = ct + 60 - P["seat_insert"] + P["seat_len"]
    inst("seatpost_head", template="seatpost_head", pose=f"[0 0 {top}] mm")
    inst("saddle", template="saddle", pose=f"[0 0 {top + 30}] mm")

    c = o.connect
    key = {"class": "rigid", "mechanism": "key"}
    press = {"class": "rigid", "mechanism": "press"}
    brg = {"class": "revolute", "mechanism": "bearing", "axis": [0, 1, 0]}
    if P["debug_bad"] == 2:  # connect to a port that does not exist
        c("axle.hubb", "wheel.hub", joint=key)
    if P["debug_bad"] == 3:  # instance of an unknown template
        inst("ghost", template="t_nope", pose="[0 0 0] mm")
    if P["debug_bad"] == 2:  # connect to a port that does not exist
        c("axle.hubb", "wheel.hub", joint=key)
    if P["debug_bad"] == 3:  # instance of an unknown template
        inst("ghost", template="t_nope", pose="[0 0 0] mm")
    c("axle.hub", "wheel.hub", joint=key)
    c("axle.brg_l", "bearing_left.bore", joint=brg)
    c("axle.brg_r", "bearing_right.bore", joint=brg)
    c("axle.end_l", "crank_left.axle_bore", joint=press)
    c("axle.end_r", "crank_right.axle_bore", joint=press)
    c("bearing_left.seat", "fork_left.bearing_seat", joint=press)
    c("bearing_right.seat", "fork_right.bearing_seat", joint=press)
    c(
        "flange_bolt_left.thread",
        "fork_left.crown_flange",
        joint={"class": "rigid", "mechanism": "screw"},
    )
    c(
        "flange_bolt_right.thread",
        "fork_right.crown_flange",
        joint={"class": "rigid", "mechanism": "screw"},
    )
    c(
        "crown.seat_tube",
        "seatpost.tube",
        joint={"class": "prismatic", "mechanism": "press", "axis": [0, 0, 1]},
    )
    c(
        "seatpost_clamp_bolt.thread",
        "crown.clamp",
        joint={"class": "rigid", "mechanism": "screw"},
    )
    c(
        "crank_bolt_left.thread",
        "crank_left.clamp",
        joint={"class": "rigid", "mechanism": "screw"},
    )
    c(
        "crank_bolt_right.thread",
        "crank_right.clamp",
        joint={"class": "rigid", "mechanism": "screw"},
    )
    c(
        "crank_left.pedal_mount",
        "pedal_left.spindle",
        joint={"class": "revolute", "mechanism": "bearing", "axis": [0, 1, 0]},
    )
    c(
        "crank_right.pedal_mount",
        "pedal_right.spindle",
        joint={"class": "revolute", "mechanism": "bearing", "axis": [0, 1, 0]},
    )
    c("seatpost.top", "seatpost_head.base", joint=press)
    c("seatpost_head.rail_clamp", "saddle.rails", joint=press)


import os
import pathlib

_OUT = pathlib.Path(
    os.environ.get("UNICYCLE_OUT", pathlib.Path(__file__).parent / "out")
)
with out.run(_OUT, check=False) as o:
    build(o)
print(o.root)
