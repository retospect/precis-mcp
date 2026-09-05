"""Ports + mates — the assembly-by-interface half of the machine spec.

Pure kernel: designs are resolved from an in-memory dict, so nothing here
touches the store (``precis.cad`` imports no DB by contract).
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.cad.scene import (
    MateSpec,
    PortSpec,
    SceneError,
    build_design,
    expand_instances,
    mates_of,
    parse_source,
    ports_of,
    spec_to_source,
)

#: A motor whose output shaft face is a port 60 mm up its own z axis.
_MOTOR = """
component body
shell add box:w40d40h60
port shaft @0,0,60
"""

#: A gearbox with an input face at its origin and an output face 30 mm up.
_GEARBOX = """
component case
shell add box:w50d50h30
port drive @0,0,0
port out   @0,0,30
"""

_LIBRARY = {"motor": parse_source(_MOTOR), "gearbox": parse_source(_GEARBOX)}


def _resolve(slug: str):
    if slug not in _LIBRARY:
        raise KeyError(slug)
    return _LIBRARY[slug]


def _node(spec, name):
    return next(n for n in spec.nodes if n.name == name)


# ── parsing ──────────────────────────────────────────────────────────────


def test_port_parses_into_meta_not_nodes():
    spec = parse_source(_GEARBOX)
    # a port is a frame, not geometry — it must never become a node row
    assert [n.name for n in spec.nodes] == ["shell"]
    assert [(p.name, p.loc) for p in ports_of(spec)] == [
        ("drive", (0.0, 0.0, 0.0)),
        ("out", (0.0, 0.0, 30.0)),
    ]


def test_port_accepts_a_rotated_frame():
    spec = parse_source("body add box:w10d10h10\nport face @5,0,0 rot:0,90,0")
    (port,) = ports_of(spec)
    assert port.loc == (5.0, 0.0, 0.0) and port.rot == (0.0, 90.0, 0.0)


def test_mate_parses_both_anchor_forms():
    spec = parse_source(
        "port base @0,0,0\n"
        "use motor as m\n"
        "use gearbox as g\n"
        "mate g.drive to base\n"
        "mate m.shaft to g.out flip spin:45\n"
    )
    own, chained = mates_of(spec)
    assert (own.instance, own.port) == ("g", "drive")
    assert own.anchor_instance is None and own.anchor_port == "base"
    assert not own.flip and own.spin == 0.0
    assert (chained.anchor_instance, chained.anchor_port) == ("g", "out")
    assert chained.flip and chained.spin == 45.0


@pytest.mark.parametrize(
    "line, msg",
    [
        ("port", "expected 'port <name>"),
        ("port a.b @0,0,0", "bad port name"),
        ("port p @0,0,0\nport p @1,0,0", "duplicate port"),
        ("port p @0,0,0 polar:n4r10", "cannot carry a pattern"),
        ("mate a.b", "expected 'mate"),
        ("mate a.b at c", "expected 'mate"),
        ("mate ab to c", "mate subject must be"),
        ("mate a.b.c to d", "mate subject must be"),
        ("mate a.b to c.d.e", "mate anchor must be"),
        ("mate a.b to c wobble", "unrecognised token"),
    ],
)
def test_bad_port_or_mate_lines_are_scene_errors(line, msg):
    with pytest.raises(SceneError, match=msg):
        parse_source(line)


def test_source_round_trips_ports_and_mates():
    src = (
        "desc: a drivetrain\n"
        "port base @0,0,0\n"
        "port side @10,0,0 rot:0,90,0\n"
        "use gearbox as g\n"
        "use motor as m\n"
        "mate g.drive to base\n"
        "mate m.shaft to g.out flip spin:90\n"
    )
    spec = parse_source(src)
    assert parse_source(spec_to_source(spec)) == spec
    rendered = spec_to_source(spec)
    # mates render after the `use` lines they address
    assert rendered.index("use gearbox") < rendered.index("mate g.drive")
    assert "mate m.shaft to g.out flip spin:90" in rendered


def test_a_design_without_ports_or_mates_keeps_bare_meta():
    # existing designs must be byte-identical through the new code
    spec = parse_source("body add cyl:r5h5")
    assert spec.meta == {"units": "mm"}


# ── solving ──────────────────────────────────────────────────────────────


def test_mate_to_own_port_places_the_instance():
    # the motor's shaft (z=60 in its own frame) must land on the design's
    # `base` port at z=100 — so the motor body sits 60 below it.
    spec = parse_source("port base @0,0,100\nuse motor as m\nmate m.shaft to base\n")
    out = expand_instances(spec, _resolve)
    node = _node(out, "m.shell")
    assert node.loc == pytest.approx((0.0, 0.0, 40.0))
    assert node.rot == pytest.approx((0.0, 0.0, 0.0))


def test_mate_chains_through_another_instance():
    spec = parse_source(
        "port base @0,0,0\n"
        "use gearbox as g\n"
        "use motor as m\n"
        "mate g.drive to base\n"
        "mate m.shaft to g.out\n"
    )
    out = expand_instances(spec, _resolve)
    assert _node(out, "g.shell").loc == pytest.approx((0.0, 0.0, 0.0))
    # gearbox `out` is at z=30; the motor shaft is 60 up its own body
    assert _node(out, "m.shell").loc == pytest.approx((0.0, 0.0, -30.0))


def test_mate_equals_the_hand_placed_design():
    """The acceptance criterion: zero world coordinates, same geometry."""
    mated = parse_source("port base @0,0,100\nuse motor as m\nmate m.shaft to base\n")
    placed = parse_source("use motor as m @0,0,40\n")
    a = expand_instances(mated, _resolve)
    b = expand_instances(placed, _resolve)
    assert [(n.name, n.loc) for n in a.nodes] == [(n.name, n.loc) for n in b.nodes]


def test_flip_opposes_the_mating_frames():
    # default is coincidence; `flip` is the explicit 180° about x
    plain = expand_instances(
        parse_source("port base @0,0,0\nuse motor as m\nmate m.shaft to base\n"),
        _resolve,
    )
    flipped = expand_instances(
        parse_source("port base @0,0,0\nuse motor as m\nmate m.shaft to base flip\n"),
        _resolve,
    )
    assert _node(plain, "m.shell").rot == pytest.approx((0.0, 0.0, 0.0))
    assert _node(plain, "m.shell").loc == pytest.approx((0.0, 0.0, -60.0))
    assert _node(flipped, "m.shell").rot[0] == pytest.approx(180.0)
    # flipped, the body extends the other way out of the port
    assert _node(flipped, "m.shell").loc == pytest.approx((0.0, 0.0, 60.0), abs=1e-9)


def test_spin_rotates_about_the_port_axis():
    out = expand_instances(
        parse_source(
            "port base @0,0,0 rot:0,0,0\nuse motor as m\nmate m.shaft to base spin:90\n"
        ),
        _resolve,
    )
    assert _node(out, "m.shell").rot == pytest.approx((0.0, 0.0, 90.0))


def test_mate_against_a_rotated_own_port():
    # a port lying on its side: the mated body must come out along +x
    spec = parse_source(
        "port side @100,0,0 rot:0,90,0\nuse motor as m\nmate m.shaft to side\n"
    )
    out = expand_instances(spec, _resolve)
    node = _node(out, "m.shell")
    # the shaft was +60 along the motor's own z, now pointing along world +x
    assert node.loc == pytest.approx((40.0, 0.0, 0.0), abs=1e-9)
    assert node.rot == pytest.approx((0.0, 90.0, 0.0))


def test_unmated_instance_still_sits_where_it_was_placed():
    # a base/frame instance at the origin stays legal alongside mates
    spec = parse_source(
        "port base @0,0,0\n"
        "use gearbox as g\n"
        "use motor as m @5,5,5\n"
        "mate g.drive to base\n"
    )
    out = expand_instances(spec, _resolve)
    assert _node(out, "m.shell").loc == pytest.approx((5.0, 5.0, 5.0))


def test_mated_design_builds_and_probes():
    spec = parse_source(
        "port base @0,0,0\n"
        "use gearbox as g\n"
        "use motor as m\n"
        "mate g.drive to base\n"
        "mate m.shaft to g.out\n"
    )
    design = build_design(spec, resolve=_resolve)
    assert set(design.components) == {"g.case", "m.body"}


# ── refusals ─────────────────────────────────────────────────────────────


def test_mate_subject_must_be_an_instance():
    with pytest.raises(SceneError, match="is not an instance in this design"):
        expand_instances(
            parse_source("port base @0,0,0\nmate m.shaft to base\n"), _resolve
        )


def test_instance_mated_twice_is_over_constrained():
    src = (
        "port a @0,0,0\nport b @0,0,50\nuse motor as m\n"
        "mate m.shaft to a\nmate m.shaft to b\n"
    )
    with pytest.raises(SceneError, match="mated twice"):
        expand_instances(parse_source(src), _resolve)


def test_mated_and_explicitly_placed_is_over_constrained():
    src = "port base @0,0,0\nuse motor as m @1,2,3\nmate m.shaft to base\n"
    with pytest.raises(SceneError, match="both mated and explicitly placed"):
        expand_instances(parse_source(src), _resolve)


def test_patterned_instance_cannot_be_mated():
    src = "port base @0,0,0\nuse motor as m polar:n4r20\nmate m.shaft to base\n"
    with pytest.raises(SceneError, match="patterned instance"):
        expand_instances(parse_source(src), _resolve)


def test_unknown_port_on_the_subject_lists_what_exists():
    src = "port base @0,0,0\nuse motor as m\nmate m.nope to base\n"
    with pytest.raises(SceneError, match="has no port 'nope'.*declared ports: shaft"):
        expand_instances(parse_source(src), _resolve)


def test_unknown_own_anchor_port_lists_what_exists():
    src = "port base @0,0,0\nuse motor as m\nmate m.shaft to missing\n"
    with pytest.raises(SceneError, match="not a port of this design"):
        expand_instances(parse_source(src), _resolve)


def test_unknown_anchor_instance_is_named():
    src = "use motor as m\nmate m.shaft to ghost.out\n"
    with pytest.raises(SceneError, match="'ghost' is not an instance"):
        expand_instances(parse_source(src), _resolve)


def test_mate_cycle_names_the_chain():
    src = (
        "use motor as a\nuse motor as b\n"
        "mate a.shaft to b.shaft\nmate b.shaft to a.shaft\n"
    )
    with pytest.raises(SceneError, match="mate cycle: a → b → a"):
        expand_instances(parse_source(src), _resolve)


def test_self_mate_is_a_cycle():
    src = "use gearbox as g\nmate g.drive to g.out\n"
    with pytest.raises(SceneError, match="mate cycle: g → g"):
        expand_instances(parse_source(src), _resolve)


def test_mates_without_a_resolver_are_refused_not_crashed():
    src = "port base @0,0,0\nuse motor as m\nmate m.shaft to base\n"
    with pytest.raises(SceneError, match="no resolver"):
        expand_instances(parse_source(src), None)


def test_unresolvable_mated_design_is_a_scene_error():
    src = "port base @0,0,0\nuse absent as m\nmate m.shaft to base\n"
    with pytest.raises(SceneError, match="cannot resolve design 'absent'"):
        expand_instances(parse_source(src), _resolve)


def test_malformed_stored_port_is_refused():
    spec = parse_source("body add cyl:r5h5")
    spec.meta["ports"] = [{"loc": [0, 0, 0]}]
    with pytest.raises(SceneError, match="malformed stored port"):
        ports_of(spec)


def test_malformed_stored_mate_is_refused():
    spec = parse_source("body add cyl:r5h5")
    spec.meta["mates"] = ["not a dict"]
    with pytest.raises(SceneError, match="malformed stored mate"):
        mates_of(spec)


# ── the no-mate path is untouched ────────────────────────────────────────


def test_spec_without_mates_is_returned_identically():
    spec = parse_source("body add cyl:r5h5")
    assert expand_instances(spec, None) is spec


def test_instances_without_mates_still_expand():
    spec = parse_source("use motor as m @0,0,10\n")
    out = expand_instances(spec, _resolve)
    assert _node(out, "m.shell").loc == pytest.approx((0.0, 0.0, 10.0))


# ── the dataclasses themselves ───────────────────────────────────────────


def test_port_frame_is_the_pose_transform():
    frame = PortSpec(name="p", loc=(1.0, 2.0, 3.0), rot=(0.0, 0.0, 90.0)).frame()
    assert np.allclose(frame.apply(np.zeros(3)), [1.0, 2.0, 3.0])
    assert np.allclose(frame.apply_dir(np.array([1.0, 0.0, 0.0])), [0.0, 1.0, 0.0])


def test_mate_meta_round_trips():
    mate = MateSpec.build(
        subject="m.shaft", anchor="g.out", flip=True, spin=30.0, where="t"
    )
    assert MateSpec.from_meta(mate.to_meta()) == mate
    plain = MateSpec.build(
        subject="m.shaft", anchor="base", flip=False, spin=0.0, where="t"
    )
    # flip/spin are omitted at their defaults so stored meta stays minimal
    assert plain.to_meta() == {"subject": "m.shaft", "anchor": "base"}
    assert MateSpec.from_meta(plain.to_meta()) == plain


def test_expansion_is_idempotent():
    """``build_design`` re-expands whatever it is handed, so an already-expanded
    spec must not try to re-solve mates whose instances it just consumed."""
    spec = parse_source("port base @0,0,100\nuse motor as m\nmate m.shaft to base\n")
    once = expand_instances(spec, _resolve)
    twice = expand_instances(once, _resolve)
    assert [(n.name, n.loc) for n in twice.nodes] == [
        (n.name, n.loc) for n in once.nodes
    ]
    assert "mates" not in once.meta
    # ports survive: they still describe this design's interfaces
    assert [p.name for p in ports_of(once)] == ["base"]


def test_patterned_anchor_instance_is_refused():
    # a `polar:` anchor is N frames, not one — mating against it would
    # silently pick the base copy
    src = "use gearbox as g polar:n4r30\nuse motor as m\nmate m.shaft to g.out\n"
    with pytest.raises(SceneError, match="is patterned, so its port is many frames"):
        expand_instances(parse_source(src), _resolve)
