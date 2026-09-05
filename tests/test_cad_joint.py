"""Joints — the articulated generalisation of mates (cad slice 3).

Pure kernel, like ``test_cad_mate.py``: designs resolve from an in-memory
dict; a mate is a ``fixed`` joint, and the articulated kinds insert a
state-dependent transform at the interface. Also covers typed ports,
component-scoped ports + component joints, gear/belt couplings, and the
``state=`` posing path.
"""

from __future__ import annotations

import math

import pytest

from precis.cad.scene import (
    SceneError,
    build_design,
    expand_instances,
    joints_of,
    mates_of,
    parse_source,
    ports_of,
    spec_to_source,
)

#: A motor whose shaft face is a typed port 40 mm up its own z axis.
_MOTOR = """
component body
case add box:w42d42h40
port shaft @0,0,40 type:shaft-d5
"""

#: A crank arm reaching +x from its hub, with a typed hub bore.
_CRANK = """
component arm
bar add box:w30d6h6 @15,0,0
port hub type:shaft-d5
port tip @30,0,0
"""

_LIBRARY = {"motor": parse_source(_MOTOR), "crank": parse_source(_CRANK)}


def _resolve(slug: str):
    if slug not in _LIBRARY:
        raise KeyError(slug)
    return _LIBRARY[slug]


def _node(spec, name):
    return next(n for n in spec.nodes if n.name == name)


#: A crank jointed onto the motor shaft — the canonical revolute chain.
_RIG = """
port base
use motor as m
mate m.shaft to base
use crank as c
joint c.hub to m.shaft revolute limits:-180..180
"""

#: A one-design arm articulated about a component-scoped port.
_ARM = """
component base
slab add box:w60d60h5
component arm
beam add box:w40d8h8 @20,0,9
port shoulder @0,0,9 of:arm
joint arm revolute at:shoulder limits:-90..90
"""


# ── parsing + round trip ─────────────────────────────────────────────────


def test_joint_lines_round_trip_through_source():
    spec = parse_source(_RIG)
    src = spec_to_source(spec)
    assert "joint c.hub to m.shaft revolute limits:-180..180" in src
    assert parse_source(src) == spec


def test_component_joint_round_trips():
    spec = parse_source(_ARM)
    src = spec_to_source(spec)
    assert "port shoulder @0,0,9 of:arm" in src
    assert "joint arm revolute at:shoulder limits:-90..90" in src
    assert parse_source(src) == spec


def test_typed_port_round_trips_and_reads():
    spec = parse_source(_MOTOR)
    (port,) = ports_of(spec)
    assert port.type == "shaft-d5"
    assert "port shaft @0,0,40 type:shaft-d5" in spec_to_source(spec)


def test_couple_round_trips():
    src = (
        "port a\nport b @0,0,60\nuse crank as c1\nuse crank as c2\n"
        "joint c1.hub to a revolute\njoint c2.hub to b revolute\n"
        "gear c1 to c2 ratio:-2\n"
    )
    spec = parse_source(src)
    assert "gear c1 to c2 ratio:-2" in spec_to_source(spec)
    assert parse_source(spec_to_source(spec)) == spec


def test_a_mate_is_a_fixed_joint():
    spec = parse_source(_RIG)
    kinds = {m.instance: m.kind for m in mates_of(spec)}
    assert kinds == {"m": "fixed", "c": "revolute"}
    # ...and the fixed one still renders as the `mate` sugar it was written as
    assert "mate m.shaft to base" in spec_to_source(spec)


@pytest.mark.parametrize(
    ("line", "msg"),
    [
        ("joint c.hub to base frobnicate", "not one of"),
        ("joint c.hub to base fixed limits:0..9", "no state"),
        ("joint c.hub to base screw", "requires pitch"),
        ("joint c.hub to base revolute pitch:2", "only applies to 'screw'"),
        ("joint c.hub to base revolute limits:90..-90", "lo must be < hi"),
        ("joint arm fixed at:shoulder", "no-op"),
        ("gear c to c ratio:2", "coupled to itself"),
        ("gear c to x ratio:0", "non-zero"),
        ("port p polar:n3r5", "cannot carry a pattern"),
    ],
)
def test_bad_joint_lines_are_refused(line, msg):
    with pytest.raises(SceneError, match=msg):
        parse_source(f"port base\nuse crank as c\n{line}\n")


def test_component_joint_refusals():
    # jointing an instance via the component form
    with pytest.raises(SceneError, match="that's an instance"):
        parse_source("port p\nuse crank as x\njoint x revolute at:p\n")
    # unknown component
    with pytest.raises(SceneError, match="no such component"):
        parse_source("port p\nsolo add cyl:r5h5\njoint ghost revolute at:p\n")
    # pivot port not scoped to the jointed component
    with pytest.raises(SceneError, match="must be scoped 'of:arm'"):
        parse_source(
            "component arm\nbeam add box:w40d8h8\nport shoulder\n"
            "joint arm revolute at:shoulder\n"
        )
    # jointed twice
    with pytest.raises(SceneError, match="jointed twice"):
        parse_source(
            "component arm\nbeam add box:w40d8h8\nport s of:arm\nport t of:arm\n"
            "joint arm revolute at:s\njoint arm prismatic at:t limits:0..9\n"
        )
    # port of: an instance
    with pytest.raises(SceneError, match="must name a component"):
        parse_source("use crank as c\nport p of:c\n")


def test_couple_refusals():
    base = (
        "port a\nport b @0,0,60\nuse crank as c1\nuse crank as c2\n"
        "joint c1.hub to a revolute\njoint c2.hub to b revolute\n"
    )
    with pytest.raises(SceneError, match="not an articulated joint"):
        parse_source(base + "gear c1 to ghost ratio:2\n")
    with pytest.raises(SceneError, match="driven by two"):
        parse_source(base + "gear c1 to c2 ratio:2\nbelt c1 to c2 ratio:3\n")
    with pytest.raises(SceneError, match="coupling cycle"):
        parse_source(base + "gear c1 to c2 ratio:2\ngear c2 to c1 ratio:2\n")
    # a fixed mate has no state to couple
    with pytest.raises(SceneError, match="not an articulated joint"):
        parse_source(
            "port a\nuse motor as m\nmate m.shaft to a\n"
            "use crank as c\njoint c.hub to a revolute\n"
            "gear c to m ratio:2\n"
        )


# ── posing ───────────────────────────────────────────────────────────────


def test_revolute_state_swings_the_crank():
    spec = parse_source(_RIG)
    at0 = expand_instances(spec, _resolve)
    at90 = expand_instances(spec, _resolve, state={"c": 90.0})
    # m.shaft is mated TO base (the origin), so the crank hub rides at z=0
    bar0, bar90 = _node(at0, "c.bar"), _node(at90, "c.bar")
    assert bar0.loc == pytest.approx((15.0, 0.0, 0.0))
    assert bar90.loc == pytest.approx((0.0, 15.0, 0.0))


def test_default_state_is_neutral_and_design_builds_as_before():
    # acceptance: a jointed design without state= behaves exactly like the
    # same design with the joint replaced by a plain mate
    jointed = expand_instances(parse_source(_RIG), _resolve)
    mated = expand_instances(
        parse_source(
            _RIG.replace(
                "joint c.hub to m.shaft revolute limits:-180..180",
                "mate c.hub to m.shaft",
            )
        ),
        _resolve,
    )
    assert [(n.name, n.loc, n.rot) for n in jointed.nodes] == [
        (n.name, n.loc, n.rot) for n in mated.nodes
    ]


def test_component_joint_rotates_about_the_port_frame():
    spec = parse_source(_ARM)
    beam = _node(expand_instances(spec, None, state={"arm": 90.0}), "beam")
    # the shoulder port sits at the origin in x/y, so +x swings to +y;
    # z is preserved (rotation about the port's z axis at z=9)
    assert beam.loc == pytest.approx((0.0, 20.0, 9.0))
    base = _node(expand_instances(spec, None, state={"arm": 90.0}), "slab")
    assert base.loc == pytest.approx((0.0, 0.0, 0.0))  # unjointed part stays


def test_component_joint_off_axis_pivot():
    # pivot away from the origin: conjugation, not a rotation about z=0,0
    src = (
        "component arm\nbeam add box:w10d4h4 @30,0,0\n"
        "port pivot @20,0,0 of:arm\n"
        "joint arm revolute at:pivot limits:-180..180\n"
    )
    beam = _node(
        expand_instances(parse_source(src), None, state={"arm": 180.0}), "beam"
    )
    # beam centre was 10 mm +x of the pivot; at 180° it is 10 mm -x of it
    assert beam.loc == pytest.approx((10.0, 0.0, 0.0))


def test_prismatic_slides_along_z():
    src = "port a\nuse crank as c\njoint c.hub to a prismatic limits:0..50\n"
    bar = _node(
        expand_instances(parse_source(src), _resolve, state={"c": 12.5}), "c.bar"
    )
    assert bar.loc == pytest.approx((15.0, 0.0, 12.5))


def test_screw_advances_pitch_per_turn():
    src = "port a\nuse crank as c\njoint c.hub to a screw pitch:2 limits:0..720\n"
    bar = _node(
        expand_instances(parse_source(src), _resolve, state={"c": 360.0}), "c.bar"
    )
    assert bar.loc[2] == pytest.approx(2.0)
    # ...and it rotated a full turn back to +x
    assert bar.loc[0] == pytest.approx(15.0)


def test_cylindrical_takes_two_dof():
    src = "port a\nuse crank as c\njoint c.hub to a cylindrical\n"
    bar = _node(
        expand_instances(parse_source(src), _resolve, state={"c": [90.0, 7.0]}),
        "c.bar",
    )
    assert bar.loc == pytest.approx((0.0, 15.0, 7.0))
    with pytest.raises(SceneError, match="angle_deg, slide_mm"):
        expand_instances(parse_source(src), _resolve, state={"c": 90.0})


def test_gear_coupling_derives_the_driven_state():
    src = (
        "port a\nport b @0,0,60\nuse crank as c1\nuse crank as c2\n"
        "joint c1.hub to a revolute\njoint c2.hub to b revolute\n"
        "gear c1 to c2 ratio:-2\n"
    )
    flat = expand_instances(parse_source(src), _resolve, state={"c1": 30.0})
    b1, b2 = _node(flat, "c1.bar"), _node(flat, "c2.bar")
    assert math.degrees(math.atan2(b1.loc[1], b1.loc[0])) == pytest.approx(30.0)
    assert math.degrees(math.atan2(b2.loc[1], b2.loc[0])) == pytest.approx(-60.0)
    # explicitly setting the driven joint to something inconsistent is caught
    with pytest.raises(SceneError, match="conflicts"):
        expand_instances(parse_source(src), _resolve, state={"c1": 30.0, "c2": 10.0})
    # ...but a consistent explicit value is fine
    flat2 = expand_instances(
        parse_source(src), _resolve, state={"c1": 30.0, "c2": -60.0}
    )
    assert _node(flat2, "c2.bar").loc == pytest.approx(b2.loc)


def test_state_validation():
    spec = parse_source(_RIG)
    with pytest.raises(SceneError, match="outside limits"):
        expand_instances(spec, _resolve, state={"c": 999.0})
    with pytest.raises(SceneError, match="unknown joint"):
        expand_instances(spec, _resolve, state={"ghost": 1.0})
    with pytest.raises(SceneError, match="must be a number"):
        expand_instances(spec, _resolve, state={"c": "fast"})
    with pytest.raises(SceneError, match="declares no joints"):
        expand_instances(parse_source("solo add cyl:r5h5\n"), None, state={"x": 1})


def test_default_state_clamps_into_limits():
    # a 10..80 mm actuator defaults to 10, not an illegal 0
    src = "port a\nuse crank as c\njoint c.hub to a prismatic limits:10..80\n"
    bar = _node(expand_instances(parse_source(src), _resolve), "c.bar")
    assert bar.loc[2] == pytest.approx(10.0)


def test_coupling_limit_violation_is_caught():
    src = (
        "port a\nport b @0,0,60\nuse crank as c1\nuse crank as c2\n"
        "joint c1.hub to a revolute\n"
        "joint c2.hub to b revolute limits:-30..30\n"
        "gear c1 to c2 ratio:-2\n"
    )
    with pytest.raises(SceneError, match="outside limits"):
        expand_instances(parse_source(src), _resolve, state={"c1": 90.0})


def test_typed_ports_refuse_a_mismatched_mate():
    lib = dict(_LIBRARY)
    lib["plug"] = parse_source("component b\nc add cyl:r5h5\nport out type:xt60\n")
    src = "port inlet type:xt30\nuse plug as p\nmate p.out to inlet\n"
    with pytest.raises(SceneError, match="type mismatch"):
        expand_instances(parse_source(src), lib.__getitem__)
    # matching types (and one-side-untyped) both pass
    ok = "port inlet type:xt60\nuse plug as p\nmate p.out to inlet\n"
    assert expand_instances(parse_source(ok), lib.__getitem__).nodes
    bare = "port inlet\nuse plug as p\nmate p.out to inlet\n"
    assert expand_instances(parse_source(bare), lib.__getitem__).nodes


def test_mate_anchored_on_a_jointed_component_follows_it():
    # a motor mated onto an articulated arm swings with the arm
    src = (
        "component arm\nbeam add box:w40d8h8 @20,0,0\n"
        "port shoulder of:arm\n"
        "port wrist @40,0,4 of:arm\n"
        "joint arm revolute at:shoulder limits:-180..180\n"
        "use motor as m\nmate m.shaft to wrist\n"
    )
    flat = expand_instances(parse_source(src), _resolve, state={"arm": 90.0})
    case = _node(flat, "m.case")
    # the wrist swings from (40,0,4) to (0,40,4); the motor hangs below it
    assert case.loc[0] == pytest.approx(0.0)
    assert case.loc[1] == pytest.approx(40.0)


def test_instancing_a_design_that_itself_mates():
    # the sub-design's own mates solve (at defaults) before inlining —
    # a rig instanced elsewhere must not arrive with its motor at the origin
    lib = dict(_LIBRARY)
    lib["rig"] = parse_source(
        "port deck @0,0,100\nuse motor as m\nmate m.shaft to deck\n"
    )
    flat = expand_instances(parse_source("use rig as r @5,0,0\n"), lib.__getitem__)
    case = _node(flat, "r.m.case")
    assert case.loc == pytest.approx((5.0, 0.0, 60.0))  # 100 - 40, offset +5


def test_patterned_node_in_jointed_component_flattens():
    src = (
        "component wheel\nhub add cyl:r10h6\n"
        "spokes add box:w18d4h6 @14,0,0 polar:n4r14\n"
        "port axle of:wheel\n"
        "joint wheel revolute at:axle\n"
    )
    posed = expand_instances(parse_source(src), None, state={"wheel": 45.0})
    names = {n.name for n in posed.nodes}
    assert {"spokes#1", "spokes#2", "spokes#3", "spokes#4"} <= names
    # at neutral the pattern survives untouched (identity fast behaviour)
    neutral = expand_instances(parse_source(src), None)
    assert _node(neutral, "spokes").pattern is not None


def test_expansion_is_idempotent_with_joints():
    spec = parse_source(_RIG)
    once = expand_instances(spec, _resolve, state={"c": 45.0})
    again = expand_instances(once, _resolve)
    assert once == again
    assert "mates" not in once.meta and "joints" not in once.meta
    assert "couples" not in once.meta


def test_build_design_poses_via_state():
    from precis.cad.vec import vec3

    d = build_design(parse_source(_ARM), state={"arm": 90.0})
    assert d.classify_point(vec3(0.0, 20.0, 9.0), component="arm").inside
    assert not d.classify_point(vec3(20.0, 0.0, 9.0), component="arm").inside


def test_joints_of_and_meta_round_trip():
    spec = parse_source(_ARM)
    (j,) = joints_of(spec)
    assert (j.component, j.kind, j.port, j.limits) == (
        "arm",
        "revolute",
        "shoulder",
        (-90.0, 90.0),
    )
    # meta round-trips through dict payloads (what refs.meta stores)
    import json

    reloaded = parse_source(spec_to_source(spec))
    assert json.loads(json.dumps(reloaded.meta)) == json.loads(json.dumps(spec.meta))


def test_mating_onto_a_sub_designs_jointed_port_uses_its_default_pose():
    # finding-2 regression: a sub-design exports a port riding its own
    # jointed component whose limits exclude 0 — the default clamps to 10,
    # and an outer mate must land on the DEFLECTED frame, matching the
    # geometry _inline bakes.
    lib = dict(_LIBRARY)
    lib["lift"] = parse_source(
        "component carriage\ncart add box:w20d20h20\n"
        "port rail of:carriage\n"
        "port top @0,0,20 of:carriage\n"
        "joint carriage prismatic at:rail limits:10..80\n"
    )
    src = "use lift as L\nuse motor as m\nmate m.shaft to L.top\n"
    flat = expand_instances(parse_source(src), lib.__getitem__)
    cart = _node(flat, "L.cart")
    case = _node(flat, "m.case")
    assert cart.loc[2] == pytest.approx(10.0)  # geometry at the default
    # top frame deflects to z=30; the 40-tall motor mates its shaft there
    assert case.loc[2] == pytest.approx(-10.0)
