"""Inter-part relations — clearance / interference / DOF.

The headline case is the shaft↔bore gap: clearance must be measured to the
*subtracted* bore wall, not report a false collision against the un-bored
plate.
"""

from __future__ import annotations

import math

import pytest

from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.primitives import CircularFrustum
from precis.cad.relate import (
    SEED_GRID,
    _region,
    clearance,
    component_sdf,
    translational_dof,
)
from precis.cad.vec import pose, translation, vec3

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


def test_clearance_sub_cell_overlap_lens_reads_negative() -> None:
    """gr334763 (nm dogfood, Å-unit design): an overlap lens far thinner
    than the coarse-grid seed spacing (0.23 units vs ~1.6-unit cells)
    must still read as interference at ≈ the true depth — no seed lands
    inside the lens, and a single descent start could die in a near-side
    local minimum of the ``max()`` surface, reporting '+0.02 (clear)'."""
    side = 10.0  # ~10 Å at the nm domain's unit scale
    depth = 0.23
    d = Design()
    d.add_component("a", d.prim("a", build_config(f"box:w{side}d{side}h{side}")))
    d.add_component(
        "b",
        d.prim(
            "b",
            build_config(f"box:w{side}d{side}h{side}"),
            translation(side - depth, 0, 0),
        ),
    )
    res = clearance(d, "a", "b")
    assert res.interfering
    assert res.gap == pytest.approx(-depth, rel=0.25)


def test_clearance_sub_cell_gap_reads_positive_and_accurate() -> None:
    """The mirror case: a genuinely clear sub-cell gap (0.05 units)
    reports a small positive value at ≈ the true width — a true near
    touch and a sub-cell overlap must never print the same number
    (gr334763's identical-label half)."""
    side = 10.0
    gap = 0.05
    d = Design()
    d.add_component("a", d.prim("a", build_config(f"box:w{side}d{side}h{side}")))
    d.add_component(
        "b",
        d.prim(
            "b",
            build_config(f"box:w{side}d{side}h{side}"),
            translation(side + gap, 0, 0),
        ),
    )
    res = clearance(d, "a", "b")
    assert not res.interfering
    assert res.gap == pytest.approx(gap, rel=0.25)


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
# clearance — shallow interpenetration (gr334763)
#
# Measured in prod (nm dogfood 2026-09-11, design azo-stick-5nm): a sphere
# anchor poking 0.23 Å into a rod's end cap read as +0.022 Å "(clear)". The
# overlap lens was ~⅛ of the coarse grid's seed spacing, so no seed landed
# inside it and the descent stalled outside both bodies, on the wrong side
# of zero. These pin the whole measured curve, not just the one point.
# ---------------------------------------------------------------------------


def _anchor_and_rod(gap: float) -> Design:
    """The prod pair: sphere r2 at the origin, cylinder r3.5 h16.5 laid
    along +x with its end cap ``gap`` from the sphere's pole (negative =
    interpenetration). The cap is far wider than the sphere, so the contact
    is sphere-pole-into-flat-face and the true signed gap is exactly
    ``gap``."""
    d = Design()
    d.add_component("anchor", d.prim("anchor", build_config("sphere:r2")))
    d.add_component(
        "rod",
        d.prim(
            "rod",
            build_config("cyl:r3.5h16.5"),
            pose(vec3(2.0 + gap, 0, 0), vec3(0, math.radians(90), 0)),
        ),
    )
    return d


def _seed_spacing(d: Design) -> float:
    """The coarse grid's widest seed spacing for this pair — the length the
    overlaps below are expressed as fractions of."""
    lo, hi = _region(d, [d.components["anchor"], d.components["rod"]])
    return float(max((hi - lo) / (SEED_GRID - 1)))


def test_clearance_shallow_overlap_reads_negative() -> None:
    # The exact prod observation: −0.23 Å must not print as +0.022 Å.
    res = clearance(_anchor_and_rod(-0.23), "anchor", "rod")
    assert res.interfering
    assert math.isclose(res.gap, -0.23, abs_tol=1e-3)


@pytest.mark.parametrize("fraction", [0.1, 0.3, 0.8, 2.0])
def test_clearance_overlap_negative_at_every_seed_fraction(fraction: float) -> None:
    # Overlaps at 0.1× … 2× the seed spacing: the sub-spacing ones are the
    # ones a grid-only seeding cannot see.
    spacing = _seed_spacing(_anchor_and_rod(0.0))
    overlap = fraction * spacing
    res = clearance(_anchor_and_rod(-overlap), "anchor", "rod")
    assert res.gap < 0, f"overlap of {overlap} Å reported as {res.gap}"
    assert res.interfering
    # Past full engulfment the half-gap saturates at the sphere's radius —
    # once the anchor is wholly inside the rod, pushing further cannot make
    # ``max(d_anchor, d_rod)`` any more negative than −r.
    assert math.isclose(res.gap, -min(overlap, 2 * 2.0), rel_tol=0.02)


def test_clearance_is_monotone_through_zero() -> None:
    # The defect's signature was a JUMP from negative to positive as the
    # overlap got shallower. Reported gap must rise monotonically with the
    # true gap across the sign change, with no excursion to the wrong side.
    trues = [-1.48, -0.8, -0.58, -0.3, -0.23, -0.1, -0.05, 0.05, 0.23, 0.5, 1.0]
    reported = [clearance(_anchor_and_rod(t), "anchor", "rod").gap for t in trues]
    assert reported == sorted(reported), reported
    for t, r in zip(trues, reported, strict=True):
        assert (t < 0) == (r < 0), f"sign flip at true gap {t}: reported {r}"


def test_clearance_separated_gaps_stay_exact() -> None:
    # The fix must not cost the separated cases any accuracy.
    for t in (0.05, 0.23, 0.5, 1.0, 3.0):
        res = clearance(_anchor_and_rod(t), "anchor", "rod")
        assert not res.interfering
        assert math.isclose(res.gap, t, abs_tol=1e-4)


@pytest.mark.parametrize("factor", [1e3, 1.0, 1e-2, 1e-9])
def test_clearance_is_scale_equivariant(factor: float) -> None:
    # The same geometry redrawn at another size must report the same gap,
    # scaled — the minimiser's tolerances are fractions of a governing
    # length, never absolute (docs/backlog/multiscale-design-architecture.md
    # §Units policy). The reported resolution scales with it.
    #
    # ``factor=1e-9`` (nanometre-scale) used to be out of reach here: the
    # *primitives'* inside test compared an unnormalized edge-normal dot
    # product (units of length²) against the old absolute ``LINEAR_EPS``
    # (a length), so below ~1e-3 a frustum reported points just outside
    # its cap as inside. units-policy-cutover's relative-tolerance audit
    # (gr335192/gr334785) made that comparison scale-relative
    # (``LINEAR_REL_EPS``), so the same equivariance now holds all the way
    # down to nm.
    d = Design()
    d.add_component("anchor", d.prim("anchor", build_config(f"sphere:r{2 * factor}")))
    d.add_component(
        "rod",
        d.prim(
            "rod",
            build_config(f"cyl:r{3.5 * factor}h{16.5 * factor}"),
            pose(vec3((2.0 - 0.23) * factor, 0, 0), vec3(0, math.radians(90), 0)),
        ),
    )
    res = clearance(d, "anchor", "rod")
    assert res.interfering
    assert math.isclose(res.gap, -0.23 * factor, rel_tol=1e-3)
    # the honesty band scales with the design too, and stays below the
    # interference it has to be able to distinguish
    assert 0.0 < res.resolution < abs(res.gap)


def test_clearance_resolution_flags_a_true_touch() -> None:
    # A gap inside the query's own resolution is neither clear nor
    # interference — the caller must be able to see that.
    res = clearance(_anchor_and_rod(0.0), "anchor", "rod")
    assert res.resolution > 0.0
    assert abs(res.gap) <= res.resolution


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


def test_dof_dirs_subset_probes_only_named_directions() -> None:
    # each direction costs a full contact scan — callers reading one axis
    # (the se DOF probe) pass dirs and get exactly those keys, with the
    # same values the full probe reports.
    d = Design()
    d.add_component("block", d.prim("block", build_config("box:w10d10h10")))
    d.add_component(
        "wall", d.prim("wall", build_config("box:w10d10h10"), translation(20, 0, 0))
    )
    res = translational_dof(d, "block", "wall", reach=40.0, dirs=("+x", "-x"))
    assert set(res.travel) == {"+x", "-x"}
    assert math.isclose(res.travel["+x"], 10.0, abs_tol=0.2)
    assert res.travel["-x"] == float("inf")
    with pytest.raises(ValueError, match="unknown probe direction"):
        translational_dof(d, "block", "wall", dirs=("+x", "sideways"))
    with pytest.raises(ValueError, match="at least one probe direction"):
        translational_dof(d, "block", "wall", dirs=())
