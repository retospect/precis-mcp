"""Boolean DAG fold + Design — attribution tests (ADR 0041 §3, §6).

The headline guarantee: a subtraction is *visible* without a merge — a
ray through a drilled bore reports ``void`` attributed to the cutter, not
material.
"""

from __future__ import annotations

import math

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.primitives import CircularFrustum, box
from precis.cad.vec import translation, vec3


def _spans(design: Design, o, d):
    return design.ray(vec3(*o), vec3(*d))


# ---------------------------------------------------------------------------
# merge / fusion
# ---------------------------------------------------------------------------


def test_merge_two_overlapping_reads_as_one_solid() -> None:
    d = Design()
    a = d.prim("a", box(10, 10, 10))
    b = d.prim("b", box(10, 10, 10), translation(5, 0, 0))
    d.add_component("part", d.merge(a, b))
    spans = _spans(d, (-100, 0, 5), (1, 0, 0))
    solids = [s for s in spans if s.state == "solid"]
    assert len(solids) == 1  # fused into a single run
    assert math.isclose(solids[0].t_in, 95.0, abs_tol=1e-6)  # x=-5
    assert math.isclose(solids[0].t_out, 110.0, abs_tol=1e-6)  # x=+10


# ---------------------------------------------------------------------------
# subtract — the drilled-bore guarantee
# ---------------------------------------------------------------------------


def test_subtract_bore_reads_as_void_attributed_to_cutter() -> None:
    d = Design()
    plate = d.prim("plate", box(40, 40, 10))
    bore = d.prim("bore", CircularFrustum(rb=5, rt=5, h=20), translation(0, 0, -5))
    d.add_component("part", d.subtract(plate, bore))
    # ray along +x at mid-height, through the centre bore
    spans = _spans(d, (-100, 0, 5), (1, 0, 0))
    states = [(s.state, s.feature) for s in spans]
    # solid plate | void bore | solid plate
    assert states == [
        ("solid", "plate"),
        ("void", "bore"),
        ("solid", "plate"),
    ]


def test_subtract_void_geometry_correct() -> None:
    d = Design()
    plate = d.prim("plate", box(40, 40, 10))
    bore = d.prim("bore", CircularFrustum(rb=5, rt=5, h=20), translation(0, 0, -5))
    d.add_component("part", d.subtract(plate, bore))
    spans = _spans(d, (-100, 0, 5), (1, 0, 0))
    void = next(s for s in spans if s.state == "void")
    assert math.isclose(void.t_in, 95.0, abs_tol=1e-6)  # x=-5 (bore wall)
    assert math.isclose(void.t_out, 105.0, abs_tol=1e-6)  # x=+5


def test_point_in_bore_is_removed_by_cutter() -> None:
    d = Design()
    plate = d.prim("plate", box(40, 40, 10))
    bore = d.prim("bore", CircularFrustum(rb=5, rt=5, h=20), translation(0, 0, -5))
    d.add_component("part", d.subtract(plate, bore))
    c = d.classify_point(vec3(0, 0, 5))
    assert c.inside is False
    assert c.additive is True  # plate would be here
    assert d.instances[c.blocker].label == "bore"


def test_point_in_solid_names_owner() -> None:
    d = Design()
    plate = d.prim("plate", box(40, 40, 10))
    bore = d.prim("bore", CircularFrustum(rb=5, rt=5, h=20), translation(0, 0, -5))
    d.add_component("part", d.subtract(plate, bore))
    c = d.classify_point(vec3(15, 0, 5))
    assert c.inside is True
    assert d.instances[c.owner].label == "plate"


# ---------------------------------------------------------------------------
# intersect
# ---------------------------------------------------------------------------


def test_intersect_only_overlap_is_solid() -> None:
    d = Design()
    a = d.prim("a", box(20, 20, 20))
    b = d.prim("b", box(20, 20, 20), translation(10, 0, 0))
    d.add_component("part", d.intersect(a, b))
    spans = _spans(d, (-100, 0, 0), (1, 0, 0))
    solids = [s for s in spans if s.state == "solid"]
    assert len(solids) == 1
    # overlap is x in [0, 10] → t in [100, 110]
    assert math.isclose(solids[0].t_in, 100.0, abs_tol=1e-6)
    assert math.isclose(solids[0].t_out, 110.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# pattern — bolt circle
# ---------------------------------------------------------------------------


def test_pattern_polar_six_bolts() -> None:
    d = Design()
    plate = d.prim("plate", CircularFrustum(rb=25, rt=25, h=8), translation(0, 0, 0))
    # six bolt holes on an r=18 polar pattern at 60° spacing
    transforms = []
    for k in range(6):
        ang = 2 * math.pi * k / 6
        transforms.append(translation(18 * math.cos(ang), 18 * math.sin(ang), -1))
    bolts = d.pattern("bolt", CircularFrustum(rb=2.5, rt=2.5, h=10), transforms)
    d.add_component("flange", d.subtract(plate, bolts))
    # a +x ray at z=4 crosses the diameter: bolt at x=-18 (bolt#4) and x=+18 (bolt#1)
    spans = _spans(d, (-100, 0, 4), (1, 0, 0))
    voids = [s for s in spans if s.state == "void"]
    assert [v.feature for v in voids] == ["bolt#4", "bolt#1"]
    # each bolt hole is Ø5 → 5 mm of void
    for v in voids:
        assert math.isclose(v.t_out - v.t_in, 5.0, abs_tol=1e-6)


def test_pattern_collapses_instances_but_labels_each() -> None:
    d = Design()
    cyl = CircularFrustum(rb=1, rt=1, h=5)
    transforms = [translation(x, 0, 0) for x in (0, 10, 20)]
    union = d.pattern("peg", cyl, transforms)
    assert len(union.parts) == 3
    labels = {d.instances[p.iid].label for p in union.parts}  # type: ignore[attr-defined]
    assert labels == {"peg#1", "peg#2", "peg#3"}


# ---------------------------------------------------------------------------
# instance / shared sub-DAG
# ---------------------------------------------------------------------------


def test_instance_replaces_subtree_under_transform() -> None:
    d = Design()
    base = d.prim("widget", box(4, 4, 4))
    moved = d.instance(base, translation(50, 0, 0), suffix="*2")
    d.add_component("a", base)
    d.add_component("b", moved)
    assert d.classify_point(vec3(0, 0, 2), component="a").inside
    assert d.classify_point(vec3(50, 0, 2), component="b").inside
    assert not d.classify_point(vec3(0, 0, 2), component="b").inside


# ---------------------------------------------------------------------------
# DSL integration
# ---------------------------------------------------------------------------


def test_design_from_dsl_configs() -> None:
    d = Design()
    plate = d.prim("plate", build_config("cyl:r25h8"))
    bore = d.prim("bore", build_config("cyl:r8h10"), translation(0, 0, -1))
    d.add_component("flange", d.subtract(plate, bore))
    c = d.classify_point(vec3(0, 0, 4))
    assert not c.inside
    assert d.instances[c.blocker].label == "bore"
