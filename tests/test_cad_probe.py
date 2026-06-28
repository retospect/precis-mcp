"""Probe ladder — point / ray / arc / section / draft (ADR 0041 §6, §11).

Exercised against the canonical flange: a Ø50×8 plate, a Ø16 centre bore,
and six Ø5 bolt holes on an r=18 polar circle.
"""

from __future__ import annotations

import math

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.primitives import CircularFrustum
from precis.cad.probe import (
    probe_arc,
    probe_draft,
    probe_point,
    probe_ray,
    probe_section_z,
)
from precis.cad.vec import translation, vec3


def _flange() -> Design:
    d = Design()
    plate = d.prim("plate", build_config("cyl:r25h8"))
    bore = d.prim("hub_bore", build_config("cyl:r8h10"), translation(0, 0, -1))
    transforms = [
        translation(
            18 * math.cos(2 * math.pi * k / 6), 18 * math.sin(2 * math.pi * k / 6), -1
        )
        for k in range(6)
    ]
    bolts = d.pattern("bolt", CircularFrustum(rb=2.5, rt=2.5, h=10), transforms)
    d.add_component("flange", d.subtract(plate, bore, bolts))
    return d


# ---------------------------------------------------------------------------
# point
# ---------------------------------------------------------------------------


def test_point_in_solid_plate() -> None:
    res = probe_point(_flange(), vec3(15, 0, 4))
    assert res.state == "contains"
    assert any(h.label == "plate" and h.relation == "contains" for h in res.hits)


def test_point_in_bore_removed_by() -> None:
    res = probe_point(_flange(), vec3(0, 0, 4))
    assert res.state == "empty"
    rels = {(h.label, h.relation) for h in res.hits}
    assert ("hub_bore", "removed-by") in rels
    assert ("plate", "would-contain") in rels


def test_point_outside_reports_nearest() -> None:
    res = probe_point(_flange(), vec3(40, 0, 4))
    assert res.state == "empty"
    assert any(h.relation == "nearest" for h in res.hits)


# ---------------------------------------------------------------------------
# ray
# ---------------------------------------------------------------------------


def test_ray_through_center_bore_and_two_bolts() -> None:
    res = probe_ray(_flange(), vec3(-30, 0, 4), vec3(1, 0, 0))
    seq = [(s.state, s.feature) for s in res.segments]
    # plate | bolt#4 | plate | hub_bore | plate | bolt#1 | plate
    assert seq == [
        ("solid", "plate"),
        ("void", "bolt#4"),
        ("solid", "plate"),
        ("void", "hub_bore"),
        ("solid", "plate"),
        ("void", "bolt#1"),
        ("solid", "plate"),
    ]


def test_ray_center_bore_diameter() -> None:
    res = probe_ray(_flange(), vec3(-30, 0, 4), vec3(1, 0, 0))
    bore = next(s for s in res.segments if s.feature == "hub_bore")
    assert math.isclose(bore.length, 16.0, abs_tol=1e-6)  # Ø16


# ---------------------------------------------------------------------------
# arc — the bolt circle
# ---------------------------------------------------------------------------


def test_arc_finds_six_bolt_voids() -> None:
    res = probe_arc(_flange(), vec3(0, 0, 4), vec3(0, 0, 1), radius=18.0)
    voids = [s for s in res.segments if s.state == "void"]
    assert len(voids) == 6
    assert all(v.feature.startswith("bolt") for v in voids)


def test_arc_bolt_void_angular_span() -> None:
    res = probe_arc(_flange(), vec3(0, 0, 4), vec3(0, 0, 1), radius=18.0)
    voids = [s for s in res.segments if s.state == "void"]
    # Ø5 hole at r=18 → angular span ≈ 2*asin(2.5/18) ≈ 15.95°
    expected = math.degrees(2 * math.asin(2.5 / 18))
    for v in voids:
        assert math.isclose(v.span, expected, abs_tol=1.0)


# ---------------------------------------------------------------------------
# section z=4
# ---------------------------------------------------------------------------


def test_section_z_lists_plate_and_holes() -> None:
    res = probe_section_z(_flange(), 4.0)
    by_label = {loop.label: loop for loop in res.loops}
    assert by_label["plate"].role == "outer"
    assert math.isclose(by_label["plate"].geom["r"], 25.0)
    assert by_label["hub_bore"].role == "hole"
    assert math.isclose(by_label["hub_bore"].geom["r"], 8.0)
    bolt_loops = [loop for loop in res.loops if loop.label.startswith("bolt")]
    assert len(bolt_loops) == 6
    assert all(loop.role == "hole" for loop in bolt_loops)


def test_section_above_part_is_empty() -> None:
    res = probe_section_z(_flange(), 50.0)
    assert res.loops == []


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------


def test_draft_vertical_wall_fails() -> None:
    # A plain cylinder pulled along +z: its lateral wall is vertical → 0° draft.
    d = Design()
    d.prim("post", build_config("cyl:r5h20"))
    d.add_component("part", d.subtract(d.prim("post2", build_config("cyl:r5h20"))))
    faces = probe_draft(d, vec3(0, 0, 1))
    lateral = [f for f in faces if f.tag == "lateral"]
    assert lateral and all(not f.ok for f in lateral)  # vertical wall = release fail


def test_draft_tapered_cone_passes() -> None:
    d = Design()
    d.prim("draft_cone", build_config("tcone:rb10rt8h20"))  # gentle taper
    d.add_component("part", d.prim("c2", build_config("tcone:rb10rt8h20")))
    faces = probe_draft(d, vec3(0, 0, 1), min_deg=1.0)
    lateral = [f for f in faces if f.tag == "lateral"]
    # slant = atan2(2, 20) ≈ 5.7° ≥ 1° → ok
    assert lateral and all(f.ok for f in lateral)
