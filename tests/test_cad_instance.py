"""Sub-assembly instancing — `use <slug> as <name>` (kernel level).

The expander is the whole feature: it turns an instance node into inlined,
namespaced, re-posed nodes so every downstream layer (probe / relate /
export / tessellate) keeps working on a flat spec and never learns about
instancing. These tests pin the parse, the pose composition, the guards,
and the identity fast path.
"""

from __future__ import annotations

import math

import pytest

from precis.cad.scene import (
    SceneError,
    SceneSpec,
    build_design,
    expand_instances,
    has_instances,
    instance_slug,
    parse_source,
    spec_to_source,
)
from precis.cad.vec import vec3

_PEG = "component peg\nshaft add cyl:r2mmh10mm\n"
_PLATE = "component plate\nslab add box:w40mmd40mmh4mm\n"


def _lib(**sources: str):
    """A resolver over an in-memory {slug: source} library."""
    specs = {slug: parse_source(src) for slug, src in sources.items()}

    def _resolve(slug: str) -> SceneSpec:
        if slug not in specs:
            raise SceneError(f"no such design {slug!r}")
        return specs[slug]

    return _resolve


def test_parse_use_directive() -> None:
    spec = parse_source(
        "component base\nslab add box:w10mmd10mmh2mm\nuse peg as p1 @3mm,0mm,2mm\n"
    )
    assert [n.name for n in spec.nodes] == ["slab", "p1"]
    inst = spec.nodes[1]
    assert instance_slug(inst.config) == "peg"
    assert inst.component == "p1"
    assert inst.loc == (0.003, 0.0, 0.002)
    # the `use` line does not close or join the enclosing component block
    assert spec.nodes[0].component == "base"
    assert spec.components == ["base", "p1"]


def test_use_round_trips_through_source() -> None:
    src = (
        "component base\nslab add box:w10mmd10mmh2mm\n"
        "use peg as p1 @3mm,0mm,2mm rot:0deg,0deg,45deg\n"
    )
    spec = parse_source(src)
    assert parse_source(spec_to_source(spec)) == spec


def test_expand_inlines_namespaced_nodes() -> None:
    spec = parse_source(
        "component base\nslab add box:w40mmd40mmh4mm\nuse peg as p1 @10mm,0mm,4mm\n"
    )
    out = expand_instances(spec, _lib(peg=_PEG))
    assert [n.name for n in out.nodes] == ["slab", "p1.shaft"]
    assert out.components == ["base", "p1.peg"]
    # the sub-node's own pose is composed under the instance's
    assert out.nodes[1].loc == (0.01, 0.0, 0.004)


def test_expand_composes_rotation_and_translation() -> None:
    # peg's shaft sits at @5,0,0 (mm) locally; the instance rotates 90° about
    # z, so the shaft must land on +y, not +x.
    lib = _lib(peg="component peg\nshaft add cyl:r2mmh10mm @5mm,0mm,0mm\n")
    spec = parse_source("use peg as p1 @0mm,0mm,0mm rot:0deg,0deg,90deg\n")
    out = expand_instances(spec, lib)
    loc = out.nodes[0].loc
    assert math.isclose(loc[0], 0.0, abs_tol=1e-9)
    assert math.isclose(loc[1], 0.005, abs_tol=1e-9)
    # and the built geometry agrees: material on +y, none on +x
    design = build_design(out)
    assert design.classify_point(vec3(0, 0.005, 0.005)).inside
    assert not design.classify_point(vec3(0.005, 0, 0.005)).inside


def test_instance_pattern_replicates_the_sub_assembly() -> None:
    spec = parse_source("use peg as p @10mm,0mm,0mm polar:n4r10mm\n")
    out = expand_instances(spec, _lib(peg=_PEG))
    assert [n.name for n in out.nodes] == [
        "p#1.shaft",
        "p#2.shaft",
        "p#3.shaft",
        "p#4.shaft",
    ]
    assert out.components == ["p#1.peg", "p#2.peg", "p#3.peg", "p#4.peg"]
    design = build_design(out)
    for x, y in ((0.010, 0), (0, 0.010), (-0.010, 0), (0, -0.010)):
        assert design.classify_point(vec3(x, y, 0.005)).inside


def test_nested_instances_expand() -> None:
    lib = _lib(
        peg=_PEG,
        pegpair="use peg as a @0mm,0mm,0mm\nuse peg as b @20mm,0mm,0mm\n",
    )
    out = expand_instances(parse_source("use pegpair as pp @0mm,0mm,50mm\n"), lib)
    assert [n.name for n in out.nodes] == ["pp.a.shaft", "pp.b.shaft"]
    design = build_design(out)
    assert design.classify_point(vec3(0, 0, 0.055)).inside
    assert design.classify_point(vec3(0.020, 0, 0.055)).inside


def test_cycle_is_reported() -> None:
    lib = _lib(a="use b as x\n", b="use a as y\n")
    with pytest.raises(SceneError, match="cycle"):
        expand_instances(parse_source("use a as top\n"), lib)


def test_missing_resolver_is_an_error_not_a_crash() -> None:
    with pytest.raises(SceneError, match="resolver"):
        expand_instances(parse_source("use peg as p1\n"))


def test_unresolvable_design_is_reported() -> None:
    with pytest.raises(SceneError, match="nope"):
        expand_instances(parse_source("use nope as p1\n"), _lib(peg=_PEG))


def test_patterned_intersect_refused_when_instanced() -> None:
    # add/cut fold associatively when flattened; intersect does not, so the
    # expander must refuse rather than silently change the sub-design.
    lib = _lib(
        odd=(
            "component o\nbase add box:w40mmd40mmh4mm\n"
            "win intersect cyl:r5mmh9mm @10mm,0mm,-1mm polar:n3r10mm\n"
        )
    )
    with pytest.raises(SceneError, match="intersect"):
        expand_instances(parse_source("use odd as o1\n"), lib)


def test_duplicate_instance_name_rejected() -> None:
    with pytest.raises(SceneError, match="duplicate"):
        parse_source("use peg as p1\nuse peg as p1\n")


def test_component_colliding_with_instance_rejected() -> None:
    with pytest.raises(SceneError, match="collides"):
        parse_source("use peg as p1\ncomponent p1\nx add box:w1mmd1mmh1mm\n")


def test_dotted_instance_name_rejected() -> None:
    with pytest.raises(SceneError, match="instance name"):
        parse_source("use peg as p1.sub\n")


def test_no_instances_is_the_identity() -> None:
    spec = parse_source(_PLATE)
    assert not has_instances(spec)
    # same object back — non-instanced designs are byte-identical through this
    assert expand_instances(spec, _lib()) is spec
    assert expand_instances(spec) is spec


def test_top_level_pattern_survives_expansion() -> None:
    # a patterned node in the *parent* keeps its pattern (it is not inlined),
    # so an unrelated instance elsewhere can't perturb its fold.
    spec = parse_source(
        "component base\nslab add box:w40mmd40mmh4mm\n"
        "holes cut cyl:r2mmh6mm @15mm,0mm,-1mm polar:n6r15mm\n"
        "use peg as p1 @0mm,0mm,4mm\n"
    )
    out = expand_instances(spec, _lib(peg=_PEG))
    holes = next(n for n in out.nodes if n.name == "holes")
    assert holes.pattern is not None
    design = build_design(out)
    # the polar cut is still a cut through the slab
    assert not design.classify_point(vec3(0.015, 0, 0.002), component="base").inside


def test_build_design_threads_the_resolver() -> None:
    design = build_design(
        parse_source("use peg as p1 @0mm,0mm,0mm\n"), resolve=_lib(peg=_PEG)
    )
    assert list(design.components) == ["p1.peg"]
    assert design.classify_point(vec3(0, 0, 0.005)).inside
