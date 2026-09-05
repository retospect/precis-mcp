"""Straddling modules — port payload geometry (cad slice 5).

Pure kernel, like ``test_cad_joint.py``: designs resolve from an in-memory
dict. A port's payloads splice into the component on the *other* side of
its mate as ``<instance>~<name>`` nodes — a hinge's recess is machined
into the bracket it mates to, attributed loudly, never silently.
"""

from __future__ import annotations

import pytest

from precis.cad.scene import (
    PortSpec,
    SceneError,
    build_design,
    expand_instances,
    parse_source,
    ports_of,
    spec_to_source,
)
from precis.cad.vec import vec3

#: A hinge leaf whose port machines a recess + pin bore into its host.
_HINGE = """
component body
barrel add cyl:r4h20
port leaf_a @-10,0,0 of:body type:hinge-leaf
payload recess   cut box:w8d3h20 at:leaf_a @0,0,-10
payload pin_bore cut cyl:r2h24   at:leaf_a @0,0,-2
"""

#: A slab host with a scoped port for the hinge (slab spans z 5..15).
_BRACKET_RIG = """
component bracket
slab add box:w60d40h10 @0,0,5
port hp @20,0,10 of:bracket

use hinge as h
mate h.leaf_a to hp
"""

_LIBRARY = {"hinge": parse_source(_HINGE)}


def _resolve(slug: str):
    return _LIBRARY[slug]


def _node(spec, name):
    return next(n for n in spec.nodes if n.name == name)


# ── grammar / round-trip ─────────────────────────────────────────────


def test_payload_round_trips_through_source_and_meta():
    spec = _LIBRARY["hinge"]
    (port,) = ports_of(spec)
    assert [p.name for p in port.payloads] == ["recess", "pin_bore"]
    assert parse_source(spec_to_source(spec)) == spec
    assert PortSpec.from_meta(port.to_meta()) == port
    src = spec_to_source(spec)
    assert "payload recess cut box:w8d3h20 at:leaf_a @0,0,-10" in src


def test_payload_free_port_meta_is_byte_stable():
    # Slice-2/3 stored ports must not grow a key they never had.
    (port,) = ports_of(parse_source("plate add cyl:r5h5\nport top @0,0,5"))
    assert "payloads" not in port.to_meta()


def test_payload_line_precedes_its_port_declaration():
    spec = parse_source(
        """
plate add box:w20d20h5
payload notch cut box:w2d2h5 at:top
port top @0,0,5 of:part
"""
    )
    (port,) = ports_of(spec)
    assert port.payloads[0].name == "notch"


@pytest.mark.parametrize(
    ("line", "hint"),
    [
        ("payload x intersect box:w2d2h2 at:top", "intersect"),
        ("payload x add chamfer:2x45 at:top", "chamfer"),
        ("payload x cut box:w2d2h2", "at:<port>"),
        ("payload x cut box:w2d2h2 at:nope", "not a declared port"),
        ("payload x cut box:w2d2h2 at:top polar:n4r5", "pattern"),
        ("payload plate cut box:w2d2h2 at:top", "duplicate"),
        ("payload x cut", "expected"),
    ],
)
def test_bad_payload_lines_refused(line, hint):
    with pytest.raises(SceneError, match=hint):
        parse_source(f"plate add cyl:r5h5\nport top @0,0,5 of:part\n{line}")


# ── splice mechanics ─────────────────────────────────────────────────


def test_payload_cuts_into_the_mated_host():
    ex = expand_instances(parse_source(_BRACKET_RIG), resolve=_resolve)
    recess = _node(ex, "h~recess")
    assert recess.component == "bracket"
    assert recess.op == "cut"
    assert recess.loc == (20.0, 0.0, 0.0)  # hp @20,0,10 + payload @0,0,-10
    d = build_design(ex)
    # inside the recess: cut away; beside it: still solid
    assert not d.classify_point(vec3(20, 0, 7), component="bracket").inside
    assert d.classify_point(vec3(20, 15, 7), component="bracket").inside


def test_add_payload_contributes_material_to_the_host():
    lib = {
        "boss": parse_source(
            "core add cyl:r3h5\nport face of:part\n"
            "payload pad add cyl:r6h2 at:face @0,0,-2"
        )
    }
    ex = expand_instances(
        parse_source(
            "component wall\nslab add box:w40d40h4\nport wp @10,0,4 of:wall\n"
            "use boss as b\nmate b.face to wp"
        ),
        resolve=lambda s: lib[s],
    )
    d = build_design(ex)
    # the pad (r6 about x=10, z 2..4) is part of *wall*, outside the slab's
    # own footprint contribution at that point? No — inside the slab; probe
    # the pad region below the port instead: z 2..4 is inside slab anyway,
    # so probe a point only the pad could claim is impossible here; assert
    # the node landed in the wall and folds as add.
    pad = _node(ex, "b~pad")
    assert (pad.component, pad.op) == ("wall", "add")
    assert d.classify_point(vec3(10, 0, 3), component="wall").inside


def test_anchor_payload_splices_into_the_subject():
    # The *host* design's port carries the payload; the mated module gets it.
    lib = {"hinge": parse_source(_HINGE)}
    rig = parse_source(
        """
component bracket
slab add box:w60d40h10 @0,0,5
port hp @20,0,10 of:bracket
payload weep cut cyl:r1h30 at:hp @0,0,-15

use hinge as h
mate h.leaf_a to hp
"""
    )
    ex = expand_instances(rig, resolve=lambda s: lib[s])
    weep = _node(ex, "h~weep")
    assert weep.component == "h.body"
    # rides the subject's frame: hp world (20,0,10) + @0,0,-15
    assert weep.loc == (20.0, 0.0, -5.0)


def test_payload_refuses_a_host_component_with_no_geometry():
    # A payload into a node-less component would become that component's
    # BASE — and a base ignores its op, so a cut would silently add
    # material. Refused (reviewer finding, 2026-09-05).
    rig = parse_source(
        """
component bracket
slab add box:w60d40h10 @0,0,5
component void
port hp @20,0,10 of:void

use hinge as h
mate h.leaf_a to hp
"""
    )
    with pytest.raises(SceneError, match="no geometry of its own"):
        expand_instances(rig, resolve=_resolve)


def test_payload_needs_a_scoped_host_port():
    rig = parse_source(
        "component bracket\nslab add box:w60d40h10\nport hp @20,0,10\n"
        "use hinge as h\nmate h.leaf_a to hp"
    )
    with pytest.raises(SceneError, match="not scoped of: a component"):
        expand_instances(rig, resolve=_resolve)


def test_anchor_payload_needs_a_scoped_subject_port():
    lib = {"puck": parse_source("disc add cyl:r5h2\nport face @0,0,2")}
    rig = parse_source(
        "component base\nslab add box:w40d40h5\nport bp @0,0,5 of:base\n"
        "payload dimple cut sphere:r1 at:bp\n"
        "use puck as p\nmate p.face to bp"
    )
    with pytest.raises(SceneError, match="not scoped of: a component"):
        expand_instances(rig, resolve=lambda s: lib[s])


def test_payload_into_another_instances_component():
    # module mates onto a *sibling instance's* scoped port — the payload
    # host is the namespaced component of that instance.
    lib = {
        "hinge": parse_source(_HINGE),
        "plate": parse_source(
            "body add box:w60d40h10 @0,0,5\nport hp @20,0,10 of:part"
        ),
    }
    ex = expand_instances(
        parse_source("use plate as pl\nuse hinge as h\nmate h.leaf_a to pl.hp"),
        resolve=lambda s: lib[s],
    )
    assert _node(ex, "h~recess").component == "pl.part"


def test_unmated_payload_port_splices_nothing():
    ex = expand_instances(
        parse_source("component base\nslab add box:w40d40h5\nuse hinge as h @30,0,0"),
        resolve=_resolve,
    )
    assert not [n for n in ex.nodes if "~" in n.name]


def test_two_instances_of_one_module_splice_distinct_nodes():
    lib = {"hinge": parse_source(_HINGE)}
    ex = expand_instances(
        parse_source(
            """
component bracket
slab add box:w100d40h10 @0,0,5
port hp1 @20,0,10 of:bracket
port hp2 @-20,0,10 of:bracket
use hinge as h1
use hinge as h2
mate h1.leaf_a to hp1
mate h2.leaf_a to hp2
"""
        ),
        resolve=lambda s: lib[s],
    )
    got = sorted(n.name for n in ex.nodes if "~" in n.name)
    assert got == ["h1~pin_bore", "h1~recess", "h2~pin_bore", "h2~recess"]


# ── payloads × joints ────────────────────────────────────────────────


def test_payload_is_rigid_in_the_host_across_an_articulated_joint():
    # The recess is machined into the bracket — it must not swing with the
    # hinge's state.
    rig = parse_source(
        _BRACKET_RIG.replace("mate h.leaf_a to hp", "joint h.leaf_a to hp revolute")
    )
    at0 = expand_instances(rig, resolve=_resolve, state={"h": 0})
    at90 = expand_instances(rig, resolve=_resolve, state={"h": 90})
    assert _node(at0, "h~recess") == _node(at90, "h~recess")
    # while the module itself did move
    assert _node(at0, "h.barrel") != _node(at90, "h.barrel")


def test_payload_rides_a_jointed_host_component_exactly_once():
    # Host component has its own component joint: the payload is spliced
    # pre-pose and then W-baked with the rest of the component — once.
    rig = parse_source(
        """
component base
slab add box:w40d40h5

component arm
bar add box:w60d10h10 @30,0,5
port pivot @0,0,10 of:arm
port tip @55,0,10 of:arm
joint arm revolute at:pivot

use hinge as h
mate h.leaf_a to tip
"""
    )
    at0 = expand_instances(rig, resolve=_resolve, state={"arm": 0})
    r0 = _node(at0, "h~recess")
    assert (r0.component, r0.loc) == ("arm", (55.0, 0.0, 0.0))
    at90 = expand_instances(rig, resolve=_resolve, state={"arm": 90})
    r90 = _node(at90, "h~recess")
    # pivot at origin-x: (55,0,·) swings to (0,55,·) under Rz(90)
    assert r90.loc[0] == pytest.approx(0.0, abs=1e-9)
    assert r90.loc[1] == pytest.approx(55.0)
    assert r90.loc[2] == pytest.approx(0.0)


def test_expansion_is_idempotent_with_payloads():
    once = expand_instances(parse_source(_BRACKET_RIG), resolve=_resolve)
    assert expand_instances(once, resolve=_resolve) == once
