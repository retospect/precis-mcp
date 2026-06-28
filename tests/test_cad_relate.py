"""Inter-part relations — clearance / interference / DOF (ADR 0041 §7).

The headline case is the shaft↔bore gap: clearance must be measured to the
*subtracted* bore wall, not report a false collision against the un-bored
plate.
"""

from __future__ import annotations

import math

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.primitives import CircularFrustum
from precis.cad.relate import clearance, component_sdf, translational_dof
from precis.cad.vec import translation, vec3

# ---------------------------------------------------------------------------
# component SDF sign correctness (foundation)
# ---------------------------------------------------------------------------


def test_sdf_sign_through_bore() -> None:
    d = Design()
    plate = d.prim("plate", build_config("cyl:r20h10"))
    bore = d.prim("bore", build_config("cyl:r5h12"), translation(0, 0, -1))
    expr = d.subtract(plate, bore)
    d.add_component("hub", expr)
    # inside annulus → negative; inside bore void → positive; outside → positive
    assert component_sdf(d, expr, vec3(10, 0, 5)) < 0
    assert component_sdf(d, expr, vec3(0, 0, 5)) > 0  # in the bore
    assert component_sdf(d, expr, vec3(30, 0, 5)) > 0  # outside


# ---------------------------------------------------------------------------
# clearance — separated / overlapping boxes
# ---------------------------------------------------------------------------


def test_clearance_separated_boxes() -> None:
    d = Design()
    d.add_component("a", d.prim("a", build_config("box:w10d10h10")))
    d.add_component(
        "b", d.prim("b", build_config("box:w10d10h10"), translation(20, 0, 0))
    )
    res = clearance(d, "a", "b")
    assert not res.interfering
    assert math.isclose(res.gap, 10.0, abs_tol=0.05)  # x=5 → x=15


def test_clearance_overlapping_boxes_interferes() -> None:
    d = Design()
    d.add_component("a", d.prim("a", build_config("box:w10d10h10")))
    d.add_component(
        "b", d.prim("b", build_config("box:w10d10h10"), translation(8, 0, 0))
    )
    res = clearance(d, "a", "b")
    assert res.interfering
    assert res.gap < 0


# ---------------------------------------------------------------------------
# clearance — shaft ↔ bore (the carved-feature case)
# ---------------------------------------------------------------------------


def _shaft_and_hub(bore_r: float) -> Design:
    d = Design()
    shaft = d.prim(
        "shaft", CircularFrustum(rb=5.0, rt=5.0, h=20.0), translation(0, 0, -5)
    )
    d.add_component("shaft", shaft)
    plate = d.prim("plate", build_config("cyl:r20h10"))
    bore = d.prim(
        "bore", CircularFrustum(rb=bore_r, rt=bore_r, h=12.0), translation(0, 0, -1)
    )
    d.add_component("hub", d.subtract(plate, bore))
    return d


def test_clearance_shaft_in_clearance_bore() -> None:
    # bore Ø10.2 over a Ø10 shaft → 0.1 mm radial clearance.
    d = _shaft_and_hub(bore_r=5.1)
    res = clearance(d, "shaft", "hub")
    assert not res.interfering
    assert math.isclose(res.gap, 0.1, abs_tol=0.03)


def test_clearance_press_fit_interferes() -> None:
    # bore Ø9.8 under a Ø10 shaft → interference (press fit).
    d = _shaft_and_hub(bore_r=4.9)
    res = clearance(d, "shaft", "hub")
    assert res.interfering
    assert res.gap < 0


# ---------------------------------------------------------------------------
# translational DOF
# ---------------------------------------------------------------------------


def test_dof_box_toward_wall() -> None:
    d = Design()
    d.add_component("block", d.prim("block", build_config("box:w10d10h10")))
    d.add_component(
        "wall", d.prim("wall", build_config("box:w10d10h10"), translation(20, 0, 0))
    )
    res = translational_dof(d, "block", "wall", reach=40.0)
    assert math.isclose(res.travel["+x"], 10.0, abs_tol=0.2)  # contact after 10 mm
    assert res.travel["-x"] == float("inf")  # nothing behind it
    assert res.travel["+y"] == float("inf")
