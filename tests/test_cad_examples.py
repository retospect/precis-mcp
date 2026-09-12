"""Worked examples — practical mechanisms as integration tests.

Each test here is a recognizable piece of real engineering written in the
design language exactly as a user would write it, exercising several
features together (instancing, ports/mates, joints, couplings, payloads,
sweep). They double as documentation: read the sources as a tutorial.
Kept deliberately readable over terse — these are the repo's living
example library.

Kernel-level (dict resolver): hinge lid, lead-screw stage, 2R arm, vise,
typed pulley, gimbal nesting. Handler-level (live store): boss-module
lint + volume attribution, typed-port card.
"""

from __future__ import annotations

import math

import pytest

from precis.cad.scene import (
    SceneError,
    expand_instances,
    parse_source,
)
from precis.cad.vec import vec3


def _lib_resolver(lib: dict[str, str]):
    parsed = {slug: parse_source(src) for slug, src in lib.items()}

    def _resolve(slug: str):
        return parsed[slug]

    return _resolve


def _build(top: str, lib: dict[str, str], state=None):
    from precis.cad.scene import build_design

    return build_design(parse_source(top), resolve=_lib_resolver(lib), state=state)


# ── 1. Piano hinge on a box lid ──────────────────────────────────────
#
# The straddling-module flagship: ONE articulated interface, payloads in
# BOTH directions — the lid's port machines a knuckle recess into the box
# body; the box's port drills the pin bore through the lid.

_LID = """
desc: box lid, hinged along its back edge
component panel
sheet add box:w60mmd40mmh4mm
port hinge @0mm,20mm,0mm rot:0deg,90deg,0deg of:panel
payload recess cut box:w12mmd4mmh30mm at:hinge @0mm,0mm,-15mm
"""

_HINGED_BOX = """
desc: storage box with a hinged lid
component body
shell add box:w60mmd40mmh30mm
port hp @0mm,20mm,30mm rot:0deg,90deg,0deg of:body
payload pin_bore cut cyl:r1.5mmh50mm at:hp @0mm,0mm,-25mm

use lid as l
joint l.hinge to hp revolute limits:-110deg..110deg
"""


def test_hinged_box_payloads_land_in_both_hosts():
    ex = expand_instances(
        parse_source(_HINGED_BOX), resolve=_lib_resolver({"lid": _LID})
    )
    by_name = {n.name: n for n in ex.nodes if "~" in n.name}
    # the lid's recess is machined into the BODY...
    assert by_name["l~recess"].component == "body"
    assert by_name["l~recess"].op == "cut"
    # ...and the body's pin bore is drilled through the LID
    assert by_name["l~pin_bore"].component == "l.panel"


def test_hinged_box_recess_and_bore_remove_real_material():
    d = _build(_HINGED_BOX, {"lid": _LID})
    # recess: world x∈[-15,15]mm, y∈[18,22]mm, z∈[24,36]mm — gone from body
    assert not d.classify_point(vec3(0, 0.019, 0.027), component="body").inside
    # just below the recess the shell is still solid
    assert d.classify_point(vec3(0, 0.019, 0.020), component="body").inside
    # pin bore (r1.5mm about the y=20mm,z=30mm hinge line): gone from the lid
    assert not d.classify_point(vec3(0, 0.0195, 0.0305), component="l.panel").inside
    # mid-lid, closed at q=0: the lid lies on top of the box
    assert d.classify_point(vec3(0, 0, 0.032), component="l.panel").inside


def test_hinged_box_recess_stays_put_while_the_lid_swings():
    lib = _lib_resolver({"lid": _LID})
    spec = parse_source(_HINGED_BOX)
    closed = expand_instances(spec, resolve=lib, state={"l": 0})
    open_ = expand_instances(spec, resolve=lib, state={"l": math.radians(90)})
    pick = lambda ex, nm: next(n for n in ex.nodes if n.name == nm)
    assert pick(closed, "l~recess") == pick(open_, "l~recess")
    assert pick(closed, "l.sheet") != pick(open_, "l.sheet")


# ── 2. Lead-screw linear stage ───────────────────────────────────────
#
# A screw joint: state is radians (an explicit deg/rad unit at the text
# boundary), the carriage advances pitch metres per revolution (2π
# radians). A geared handwheel drives it — one wheel turn = one screw
# turn × ratio.

_CARRIAGE = """
component block
flange add box:w30mmd30mmh2mm
port nut @0mm,0mm,1mm of:block
"""

_WHEEL = """
component disc
rim add cyl:r20mmh5mm
port hub of:disc
"""

_STAGE = """
desc: lead-screw linear stage, 2 mm pitch, geared 2:1 off a handwheel
component base
rail add box:w20mmd20mmh120mm
port screw_top @0mm,0mm,100mm of:base
port crank @-30mm,0mm,50mm rot:0deg,90deg,0deg of:base

use carriage as c
joint c.nut to screw_top screw pitch:2mm limits:-7200deg..0deg

use wheel as w
joint w.hub to crank revolute

gear w to c ratio:2
"""

_STAGE_LIB = {"carriage": _CARRIAGE, "wheel": _WHEEL}


def test_leadscrew_advances_pitch_mm_per_revolution():
    # the screw is gear-DRIVEN, so it is posed through the wheel (setting
    # the driven joint alone would conflict with its coupling — see the
    # refusal test below). Half a wheel turn × ratio 2 = one screw
    # revolution = 2 mm of travel; full screw turns keep the (square)
    # carriage orientation clean, so a probe point sees pure z-advance.
    at0 = _build(_STAGE, _STAGE_LIB, state={"w": 0})
    assert at0.classify_point(vec3(0.012, 0, 0.100), component="c.block").inside
    at_turn = _build(_STAGE, _STAGE_LIB, state={"w": math.radians(-180)})
    assert not at_turn.classify_point(vec3(0.012, 0, 0.100), component="c.block").inside
    assert at_turn.classify_point(vec3(0.012, 0, 0.098), component="c.block").inside


def test_consistent_explicit_screw_state_is_allowed():
    # stating BOTH ends of the coupling is fine when they agree.
    d = _build(
        _STAGE,
        _STAGE_LIB,
        state={"w": math.radians(-180), "c": math.radians(-360)},
    )
    assert d.classify_point(vec3(0.012, 0, 0.098), component="c.block").inside


def test_conflicting_explicit_screw_state_is_refused():
    with pytest.raises(SceneError):
        _build(
            _STAGE,
            _STAGE_LIB,
            state={"w": math.radians(-180), "c": math.radians(-100)},
        )


# ── 3. NEMA-17 motor + GT2 pulley — typed ports doing real work ──────

_NEMA17 = """
desc: NEMA-17 stepper motor
component body
case add box:w42mmd42mmh40mm
port shaft @0mm,0mm,40mm type:shaft-d5 of:body
"""

_PULLEY_GT2 = """
desc: GT2 pulley, 5 mm bore
component wheel
rim add cyl:r8mmh8mm
port bore type:shaft-d5 of:wheel
"""

_PULLEY_BIG = """
desc: pulley with an 8 mm bore — does NOT fit a NEMA-17 shaft
component wheel
rim add cyl:r12mmh10mm
port bore type:shaft-d8 of:wheel
"""


def test_matching_shaft_types_mate():
    d = _build(
        "use nema17 as m\nuse gt2 as p\nmate p.bore to m.shaft",
        {"nema17": _NEMA17, "gt2": _PULLEY_GT2},
    )
    assert d.classify_point(vec3(0.005, 0, 0.044), component="p.wheel").inside


def test_wrong_bore_type_is_refused_with_both_types_named():
    with pytest.raises(SceneError, match="shaft-d8") as err:
        _build(
            "use nema17 as m\nuse big as p\nmate p.bore to m.shaft",
            {"nema17": _NEMA17, "big": _PULLEY_BIG},
        )
    assert "shaft-d5" in str(err.value)


# ── 4. Two-link planar arm — forward kinematics as a regression net ──
#
# Instance joints CHAIN (an anchor on a jointed instance's port follows
# it), so shoulder+elbow compose like real FK. Beams float 2 mm above the
# plinth so only true self-collisions show up in the sweep.

_UPPER_ARM = """
component link
beam add box:w80mmd10mmh10mm @40mm,0mm,2mm
port shoulder of:link
port tip @80mm,0mm,2mm of:link
"""

_FOREARM = """
component link
beam add box:w60mmd10mmh10mm @30mm,0mm,2mm
port elbow of:link
port tip @60mm,0mm,2mm of:link
"""

_ARM_RIG = """
desc: 2R planar arm on a plinth
component base
plinth add box:w40mmd40mmh20mm

port shoulder_mount @0mm,0mm,20mm of:base
use upper_arm as ua
joint ua.shoulder to shoulder_mount revolute limits:-170deg..170deg
use forearm as fa
joint fa.elbow to ua.tip revolute limits:-175deg..175deg
"""

_ARM_LIB = {"upper_arm": _UPPER_ARM, "forearm": _FOREARM}


@pytest.mark.parametrize(
    ("q1", "q2", "tip_probe"),
    [
        (0, 0, (0.139, 0, 0.029)),  # straight out +x, reach 140mm
        (90, 0, (0, 0.139, 0.029)),  # whole arm swung to +y
        (90, -90, (0.059, 0.080, 0.029)),  # elbow bent back: fa runs +x from (0,80)
    ],
)
def test_arm_forward_kinematics(q1, q2, tip_probe):
    d = _build(
        _ARM_RIG,
        _ARM_LIB,
        state={"ua": math.radians(q1), "fa": math.radians(q2)},
    )
    assert d.classify_point(vec3(*tip_probe), component="fa.link").inside


def test_arm_folds_into_self_collision_at_the_elbow_limit():
    # at ±175° the forearm lies nearly antiparallel over the upper arm —
    # lateral separation 60·sin(5°) ≈ 5.2 mm < the two half-widths (10).
    d = _build(_ARM_RIG, _ARM_LIB, state={"fa": math.radians(175)})
    from precis.cad.relate import clearance

    assert clearance(d, "fa.link", "ua.link").interfering


# ── 5. Machine vise — screw-driven jaw, travel limits, clearance ─────

_VISE_JAW = """
component block
face add box:w10mmd30mmh25mm @0mm,0mm,-10mm
port nut rot:0deg,90deg,0deg of:block
"""

_VISE = """
desc: machine vise: sliding jaw on a prismatic drive, fixed jaw at +x
component frame
bed add box:w160mmd30mmh10mm
jaw_fixed add box:w10mmd30mmh30mm @75mm,0mm,10mm

port drive @-80mm,0mm,25mm rot:0deg,90deg,0deg of:frame
use vise_jaw as j
joint j.nut to drive prismatic limits:0mm..144mm
"""

_VISE_LIB = {"vise_jaw": _VISE_JAW}


def test_vise_jaw_travels_and_stops_short_of_the_fixed_jaw():
    from precis.cad.relate import clearance

    # fully open: jaw at x∈[-85,-75]mm
    open_ = _build(_VISE, _VISE_LIB, state={"j": 0})
    assert open_.classify_point(vec3(-0.080, 0, 0.025), component="j.block").inside
    # fully closed at the limit: leading face at x=69mm, fixed jaw at x=70mm
    closed = _build(_VISE, _VISE_LIB, state={"j": 0.144})
    assert closed.classify_point(vec3(0.068, 0, 0.025), component="j.block").inside
    gap = clearance(closed, "j.block", "frame")
    assert not gap.interfering
    # the clearance probe is sampled, not exact — allow a little slack
    assert gap.gap == pytest.approx(0.001, abs=0.0002)


def test_vise_overtravel_is_refused_not_clamped():
    with pytest.raises(SceneError):
        _build(_VISE, _VISE_LIB, state={"j": 0.150})


# ── 7. Camera gimbal — nesting rules, stated as errors that teach ────

_PAN_STAGE = """
component base
ring add cyl:r20mmh5mm
component platform
table add cyl:r18mmh4mm @0mm,0mm,6mm
port spin @0mm,0mm,6mm of:platform
joint platform revolute at:spin

port mount of:base
"""

_TILT_HEAD = """
component yoke
fork add box:w40mmd10mmh30mm
port arm @0mm,0mm,30mm rot:90deg,0deg,0deg of:yoke

use pan_stage as p
mate p.mount to arm

port base of:yoke
"""

_GIMBAL_RIG = """
desc: camera gimbal on a mast
component mast
pole add cyl:r5mmh100mm
port top @0mm,0mm,100mm of:mast

use tilt_head as head
mate head.base to top
"""

_GIMBAL_LIB = {"pan_stage": _PAN_STAGE, "tilt_head": _TILT_HEAD}


def test_gimbal_nests_three_deep_at_defaults():
    d = _build(_GIMBAL_RIG, _GIMBAL_LIB)
    # the mast is there and the head landed on top of it
    assert d.classify_point(vec3(0, 0, 0.050), component="mast").inside
    assert any(c.startswith("head.") for c in d.components)


def test_state_only_addresses_the_top_designs_joints():
    # the pan stage's joint is two levels down — invisible to state=.
    with pytest.raises(SceneError):
        _build(_GIMBAL_RIG, _GIMBAL_LIB, state={"p": math.radians(30)})


def test_state_on_a_jointless_design_is_refused():
    with pytest.raises(SceneError, match="joints: none"):
        _build(_GIMBAL_RIG, _GIMBAL_LIB, state={"head": math.radians(10)})


# ── 8. Print-in-place hinge — payloads + joint + dims in one module ──
#
# A printed hinge: the module's own pin reaches into the host; the bore
# (pin radius + FDM clearance) is a `cut` payload carved out of the host
# around it. Printed together, the pin is captive in the bore with real
# air between them. The clearance floor is a named dim (`>= 0.3` for
# FDM), so an undersized printed joint is refused at parse — not
# discovered on the build plate.

_PRINTED_HINGE = """
desc: print-in-place hinge, FDM clearances
dim pin_r = 2mm
dim clearance >= 0.3mm
component knuckle
lug add box:w8mmd6mmh12mm @4mm,0mm,0mm
pin add cyl:r2mmh10mm @0mm,0mm,6mm rot:0deg,-90deg,0deg
port leaf @0mm,0mm,6mm rot:0deg,90deg,0deg type:printed-hinge of:knuckle
payload bore cut cyl:r2.3mmh12mm at:leaf @0mm,0mm,-12mm
"""

_PRINTED_LID = """
component tray
wall add box:w60mmd40mmh4mm
port hp @30mm,0mm,2mm rot:0deg,90deg,0deg type:printed-hinge of:tray
use printed_hinge as h
joint h.leaf to hp revolute limits:0deg..170deg
"""


def test_printed_hinge_pin_is_captive_with_real_clearance():
    d = _build(_PRINTED_LID, {"printed_hinge": _PRINTED_HINGE})
    # the bore is carved out of the tray along the hinge axis...
    assert not d.classify_point(vec3(0.025, 0, 0.002), component="tray").inside
    # ...the module's pin runs inside it...
    assert d.classify_point(vec3(0.025, 0, 0.002), component="h.knuckle").inside
    # ...with real air in the clearance annulus (r2mm pin, r2.3mm bore):
    gap = vec3(0.025, 0.00215, 0.002)
    assert not d.classify_point(gap, component="tray").inside
    assert not d.classify_point(gap, component="h.knuckle").inside
    # and the tray wall away from the bore is untouched
    assert d.classify_point(vec3(0.025, 0.003, 0.0035), component="tray").inside


def test_printed_clearance_floor_is_enforced_by_dims():
    # an undersized clearance contradicts the process floor → refused
    import pytest as _pytest

    from precis.cad.scene import SceneError

    with _pytest.raises(SceneError, match="empty combined range"):
        parse_source(
            "plate add box:w10mmd10mmh2mm\n"
            "dim clearance >= 0.3mm\ndim gap = 0.1mm\nconstrain clearance = gap"
        )
