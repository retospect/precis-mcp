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
sheet add box:w60d40h4
port hinge @0,20,0 rot:0,90,0 of:panel
payload recess cut box:w12d4h30 at:hinge @0,0,-15
"""

_HINGED_BOX = """
desc: storage box with a hinged lid
component body
shell add box:w60d40h30
port hp @0,20,30 rot:0,90,0 of:body
payload pin_bore cut cyl:r1.5h50 at:hp @0,0,-25

use lid as l
joint l.hinge to hp revolute limits:-110..110
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
    # recess: world x∈[-15,15], y∈[18,22], z∈[24,36] — gone from the body
    assert not d.classify_point(vec3(0, 19, 27), component="body").inside
    # just below the recess the shell is still solid
    assert d.classify_point(vec3(0, 19, 20), component="body").inside
    # pin bore (r1.5 about the y=20,z=30 hinge line): gone from the lid
    assert not d.classify_point(vec3(0, 19.5, 30.5), component="l.panel").inside
    # mid-lid, closed at q=0: the lid lies on top of the box
    assert d.classify_point(vec3(0, 0, 32), component="l.panel").inside


def test_hinged_box_recess_stays_put_while_the_lid_swings():
    lib = _lib_resolver({"lid": _LID})
    spec = parse_source(_HINGED_BOX)
    closed = expand_instances(spec, resolve=lib, state={"l": 0})
    open_ = expand_instances(spec, resolve=lib, state={"l": 90})
    pick = lambda ex, nm: next(n for n in ex.nodes if n.name == nm)
    assert pick(closed, "l~recess") == pick(open_, "l~recess")
    assert pick(closed, "l.sheet") != pick(open_, "l.sheet")


# ── 2. Lead-screw linear stage ───────────────────────────────────────
#
# A screw joint: state is degrees, the carriage advances pitch mm per
# revolution. A geared handwheel drives it — one wheel turn = one screw
# turn × ratio.

_CARRIAGE = """
component block
flange add box:w30d30h2
port nut @0,0,1 of:block
"""

_WHEEL = """
component disc
rim add cyl:r20h5
port hub of:disc
"""

_STAGE = """
desc: lead-screw linear stage, 2 mm pitch, geared 2:1 off a handwheel
component base
rail add box:w20d20h120
port screw_top @0,0,100 of:base
port crank @-30,0,50 rot:0,90,0 of:base

use carriage as c
joint c.nut to screw_top screw pitch:2 limits:-7200..0

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
    assert at0.classify_point(vec3(12, 0, 100), component="c.block").inside
    at_turn = _build(_STAGE, _STAGE_LIB, state={"w": -180})
    assert not at_turn.classify_point(vec3(12, 0, 100), component="c.block").inside
    assert at_turn.classify_point(vec3(12, 0, 98), component="c.block").inside


def test_consistent_explicit_screw_state_is_allowed():
    # stating BOTH ends of the coupling is fine when they agree.
    d = _build(_STAGE, _STAGE_LIB, state={"w": -180, "c": -360})
    assert d.classify_point(vec3(12, 0, 98), component="c.block").inside


def test_conflicting_explicit_screw_state_is_refused():
    with pytest.raises(SceneError):
        _build(_STAGE, _STAGE_LIB, state={"w": -180, "c": -100})


# ── 3. NEMA-17 motor + GT2 pulley — typed ports doing real work ──────

_NEMA17 = """
desc: NEMA-17 stepper motor
component body
case add box:w42d42h40
port shaft @0,0,40 type:shaft-d5 of:body
"""

_PULLEY_GT2 = """
desc: GT2 pulley, 5 mm bore
component wheel
rim add cyl:r8h8
port bore type:shaft-d5 of:wheel
"""

_PULLEY_BIG = """
desc: pulley with an 8 mm bore — does NOT fit a NEMA-17 shaft
component wheel
rim add cyl:r12h10
port bore type:shaft-d8 of:wheel
"""


def test_matching_shaft_types_mate():
    d = _build(
        "use nema17 as m\nuse gt2 as p\nmate p.bore to m.shaft",
        {"nema17": _NEMA17, "gt2": _PULLEY_GT2},
    )
    assert d.classify_point(vec3(5, 0, 44), component="p.wheel").inside


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
beam add box:w80d10h10 @40,0,2
port shoulder of:link
port tip @80,0,2 of:link
"""

_FOREARM = """
component link
beam add box:w60d10h10 @30,0,2
port elbow of:link
port tip @60,0,2 of:link
"""

_ARM_RIG = """
desc: 2R planar arm on a plinth
component base
plinth add box:w40d40h20

port shoulder_mount @0,0,20 of:base
use upper_arm as ua
joint ua.shoulder to shoulder_mount revolute limits:-170..170
use forearm as fa
joint fa.elbow to ua.tip revolute limits:-175..175
"""

_ARM_LIB = {"upper_arm": _UPPER_ARM, "forearm": _FOREARM}


@pytest.mark.parametrize(
    ("q1", "q2", "tip_probe"),
    [
        (0, 0, (139, 0, 29)),  # straight out +x, reach 140
        (90, 0, (0, 139, 29)),  # whole arm swung to +y
        (90, -90, (59, 80, 29)),  # elbow bent back: fa runs +x from (0,80)
    ],
)
def test_arm_forward_kinematics(q1, q2, tip_probe):
    d = _build(_ARM_RIG, _ARM_LIB, state={"ua": q1, "fa": q2})
    assert d.classify_point(vec3(*tip_probe), component="fa.link").inside


def test_arm_folds_into_self_collision_at_the_elbow_limit():
    # at ±175° the forearm lies nearly antiparallel over the upper arm —
    # lateral separation 60·sin(5°) ≈ 5.2 mm < the two half-widths (10).
    d = _build(_ARM_RIG, _ARM_LIB, state={"fa": 175})
    from precis.cad.relate import clearance

    assert clearance(d, "fa.link", "ua.link").interfering


# ── 5. Machine vise — screw-driven jaw, travel limits, clearance ─────

_VISE_JAW = """
component block
face add box:w10d30h25 @0,0,-10
port nut rot:0,90,0 of:block
"""

_VISE = """
desc: machine vise: sliding jaw on a prismatic drive, fixed jaw at +x
component frame
bed add box:w160d30h10
jaw_fixed add box:w10d30h30 @75,0,10

port drive @-80,0,25 rot:0,90,0 of:frame
use vise_jaw as j
joint j.nut to drive prismatic limits:0..144
"""

_VISE_LIB = {"vise_jaw": _VISE_JAW}


def test_vise_jaw_travels_and_stops_short_of_the_fixed_jaw():
    from precis.cad.relate import clearance

    # fully open: jaw at x∈[-85,-75]
    open_ = _build(_VISE, _VISE_LIB, state={"j": 0})
    assert open_.classify_point(vec3(-80, 0, 25), component="j.block").inside
    # fully closed at the limit: leading face at x=69, fixed jaw at x=70
    closed = _build(_VISE, _VISE_LIB, state={"j": 144})
    assert closed.classify_point(vec3(68, 0, 25), component="j.block").inside
    gap = clearance(closed, "j.block", "frame")
    assert not gap.interfering
    # the clearance probe is sampled, not exact — allow a little slack
    assert gap.gap == pytest.approx(1.0, abs=0.2)


def test_vise_overtravel_is_refused_not_clamped():
    with pytest.raises(SceneError):
        _build(_VISE, _VISE_LIB, state={"j": 150})


# ── 7. Camera gimbal — nesting rules, stated as errors that teach ────

_PAN_STAGE = """
component base
ring add cyl:r20h5
component platform
table add cyl:r18h4 @0,0,6
port spin @0,0,6 of:platform
joint platform revolute at:spin

port mount of:base
"""

_TILT_HEAD = """
component yoke
fork add box:w40d10h30
port arm @0,0,30 rot:90,0,0 of:yoke

use pan_stage as p
mate p.mount to arm

port base of:yoke
"""

_GIMBAL_RIG = """
desc: camera gimbal on a mast
component mast
pole add cyl:r5h100
port top @0,0,100 of:mast

use tilt_head as head
mate head.base to top
"""

_GIMBAL_LIB = {"pan_stage": _PAN_STAGE, "tilt_head": _TILT_HEAD}


def test_gimbal_nests_three_deep_at_defaults():
    d = _build(_GIMBAL_RIG, _GIMBAL_LIB)
    # the mast is there and the head landed on top of it
    assert d.classify_point(vec3(0, 0, 50), component="mast").inside
    assert any(c.startswith("head.") for c in d.components)


def test_state_only_addresses_the_top_designs_joints():
    # the pan stage's joint is two levels down — invisible to state=.
    with pytest.raises(SceneError):
        _build(_GIMBAL_RIG, _GIMBAL_LIB, state={"p": 30})


def test_state_on_a_jointless_design_is_refused():
    with pytest.raises(SceneError, match="joints: none"):
        _build(_GIMBAL_RIG, _GIMBAL_LIB, state={"head": 10})
