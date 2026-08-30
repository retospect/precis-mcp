"""Unit + property tests for precis.pcb.geom's two additions: corner
filleting (:func:`fillet_polyline`/:func:`max_inward_deviation`) and
gerber-unit quantization (:func:`quantize`).

Covers: the fillet trig against independently hand-derived closed forms
(not the implementation's own math restated), a tangency property test
over randomized corners (same discipline as
``tests/test_pcb_realize.py``'s tangent-arc property test), the setback
clamp actually firing (not merely present in the code), both skip
thresholds actually triggering (and NOT triggering just past them), the
``max_inward_deviation``/``fillet_polyline`` consistency a caller relies
on to decide whether a radius is provably safe, and quantize's round-trip
through ``gerber.py``'s own emission ``round()``.
"""

from __future__ import annotations

import itertools
import math
import random

import pytest

from precis.pcb.geom import (
    GERBER_UNIT_MM,
    Point,
    _corner_fillet,
    dist,
    fillet_polyline,
    max_inward_deviation,
    max_radius_for_deviation,
    quantize,
)
from precis.pcb.gerber import _u

# ── quantize / quantize_point ────────────────────────────────────────────


def test_gerber_unit_mm_matches_gerber_module_constant():
    # 6 decimal digits per gerber.py's %FSLAX46Y46*% header -- pinned here
    # so a change to that header is caught by this test, not silently
    # absorbed.
    assert pytest.approx(1e-6) == GERBER_UNIT_MM


def test_quantize_actually_moves_excess_precision():
    """Failure direction for the idempotence test below: a no-op
    ``quantize`` would trivially pass idempotence, so first prove it does
    something."""
    x = 1.234567891234
    assert quantize(x) != x
    assert quantize(x) == pytest.approx(1.234568, abs=1e-9)


def test_quantize_matches_hand_computed_value():
    # 0.12345649 mm -> 123456.49 units -> rounds to 123456 -> 0.123456 mm.
    assert quantize(0.12345649) == pytest.approx(0.123456, abs=1e-12)
    # 0.12345651 mm -> 123456.51 units -> rounds to 123457 -> 0.123457 mm.
    assert quantize(0.12345651) == pytest.approx(0.123457, abs=1e-12)


def test_quantize_idempotent():
    rng = random.Random(1)
    for _ in range(500):
        x = rng.uniform(-500.0, 500.0)
        once = quantize(x)
        twice = quantize(once)
        assert twice == once, (x, once, twice)


def test_quantize_makes_gerber_emission_round_a_no_op():
    """The entire point of the change: quantizing FIRST means gerber.py's
    own ``_u()`` round() no longer moves the coordinate. Checked directly
    (not merely trusted) two ways: the emitted integer is unchanged by
    quantizing first, and the quantized value is already an exact
    multiple of the gerber unit so ``_u()``'s rounding has nothing left
    to do."""
    rng = random.Random(2)
    for _ in range(500):
        x = rng.uniform(-500.0, 500.0)
        qx = quantize(x)
        assert _u(qx) == _u(x), (x, qx, _u(qx), _u(x))
        # already an exact multiple of the unit -- round() is a no-op
        units = qx / GERBER_UNIT_MM
        assert units == pytest.approx(round(units), abs=1e-6)


def test_quantize_survives_a_deliberately_unfriendly_value():
    """A value chosen to be maximally likely to round differently
    pre/post-quantization (many significant digits past the gerber
    unit) -- the property must hold even here, not just for "nice"
    inputs."""
    x = 12.345678954321
    qx = quantize(x)
    assert _u(qx) == _u(x)


# ── fillet_polyline: shape/endpoint contract ─────────────────────────────


def test_fillet_polyline_rejects_fewer_than_two_points():
    with pytest.raises(ValueError):
        fillet_polyline([(0.0, 0.0)], 0.5)


def test_fillet_polyline_two_points_returns_one_unchanged_line():
    segs = fillet_polyline([(1.0, 2.0), (5.0, 9.0)], 0.5)
    assert segs == [{"shape": "line", "start": [1.0, 2.0], "end": [5.0, 9.0]}]


def test_fillet_polyline_preserves_endpoints_of_a_multi_corner_path():
    points = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (10.0, 5.0)]
    segs = fillet_polyline(points, 0.5)
    assert segs[0]["shape"] == "line"
    assert segs[0]["start"] == [0.0, 0.0]
    assert segs[-1]["shape"] == "line"
    assert segs[-1]["end"] == [10.0, 5.0]


# ── fillet_polyline: hand-derived right-angle closed form ────────────────


def test_fillet_polyline_right_angle_matches_hand_computation_left_turn():
    """prev=(-1,0), cur=(0,0), next=(0,1): a 90-degree LEFT (ccw) turn.
    t = r / tan(45deg) = r; centre = r/sin(45deg) along the bisector
    (-1,1)/sqrt(2) from the vertex -- independently computed here, not
    read back from the implementation."""
    r = 0.2
    points = [(-1.0, 0.0), (0.0, 0.0), (0.0, 1.0)]
    segs = fillet_polyline(points, r)
    assert [s["shape"] for s in segs] == ["line", "arc", "line"]
    line1, arc, line2 = segs
    assert line1["start"] == [-1.0, 0.0]
    assert line1["end"] == pytest.approx([-r, 0.0])
    assert arc["start"] == pytest.approx([-r, 0.0])
    assert arc["end"] == pytest.approx([0.0, r])
    assert arc["center"] == pytest.approx([-r, r])
    assert arc["cw"] is False  # ccw sweep -- matches realize.py's convention
    assert line2["start"] == pytest.approx([0.0, r])
    assert line2["end"] == [0.0, 1.0]


def test_fillet_polyline_right_angle_matches_hand_computation_right_turn():
    """Same corner, mirrored: prev=(-1,0), cur=(0,0), next=(0,-1) is a
    90-degree RIGHT (cw) turn -- the sweep sign must flip."""
    r = 0.2
    points = [(-1.0, 0.0), (0.0, 0.0), (0.0, -1.0)]
    segs = fillet_polyline(points, r)
    arc = segs[1]
    assert arc["center"] == pytest.approx([-r, -r])
    assert arc["cw"] is True


# ── fillet_polyline: clamp ────────────────────────────────────────────────


def test_fillet_polyline_clamps_radius_when_legs_are_short():
    """Same 90-degree corner as above but legs only 0.1mm long each, and a
    requested radius (0.2) that would need a 0.2mm setback -- more than
    half a leg (0.05mm). The setback must clamp to 0.05mm, giving an
    EFFECTIVE radius of 0.05 * tan(45deg) = 0.05, not the requested 0.2.
    This is the failure-direction check: a clamp that never fires would
    place the tangent point at distance 0.2 from the vertex, past the far
    end of a 0.1mm leg."""
    points = [(-0.1, 0.0), (0.0, 0.0), (0.0, 0.1)]
    segs = fillet_polyline(points, 0.2)
    line1, arc, line2 = segs
    assert line1["end"] == pytest.approx([-0.05, 0.0], abs=1e-9)
    assert arc["end"] == pytest.approx([0.0, 0.05], abs=1e-9)
    # setback never exceeds half the (0.1mm) leg
    assert dist((0.0, 0.0), tuple(line1["end"])) <= 0.05 + 1e-9
    assert dist((0.0, 0.0), tuple(arc["end"])) <= 0.05 + 1e-9


def test_fillet_polyline_adjacent_corners_do_not_overrun_shared_leg():
    """A short zigzag with a generous radius: the two corners sharing the
    middle leg must each claim at most half of it, so together they never
    exceed its full length (module contract: 'reduce r for that corner
    rather than failing')."""
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (2.0, 1.0)]
    shared_leg_len = dist(points[1], points[2])  # 1.0
    segs = fillet_polyline(points, radius_mm=5.0)
    arcs = [s for s in segs if s["shape"] == "arc"]
    assert len(arcs) == 2
    # first arc's END and second arc's START both lie on the shared leg,
    # each within [0, shared_leg_len] of its own end, and they must not
    # cross past each other.
    first_end_setback = dist(points[1], tuple(arcs[0]["end"]))
    second_start_setback = dist(points[2], tuple(arcs[1]["start"]))
    assert first_end_setback <= shared_leg_len / 2 + 1e-9
    assert second_start_setback <= shared_leg_len / 2 + 1e-9
    assert first_end_setback + second_start_setback <= shared_leg_len + 1e-9


# ── fillet_polyline: skip thresholds ─────────────────────────────────────


def _corner_points(theta_deg: float) -> list[Point]:
    """prev/cur/next with the vertex at the origin and interior angle
    exactly ``theta_deg`` -- e1 fixed at 180deg, e2 at (180 - theta_deg)
    degrees, so the angle between e1 and e2 is exactly theta_deg by
    construction, independent of fillet_polyline's own angle math."""
    prev = (-1.0, 0.0)
    angle2 = math.radians(180.0 - theta_deg)
    nxt = (math.cos(angle2), math.sin(angle2))
    return [prev, (0.0, 0.0), nxt]


def test_fillet_polyline_skips_near_collinear_corner():
    # 179 degrees: 1 degree off straight, inside the default 2-degree
    # collinear threshold -- must stay a sharp (unfilleted) corner.
    segs = fillet_polyline(_corner_points(179.0), 0.1)
    assert [s["shape"] for s in segs] == ["line", "line"]


def test_fillet_polyline_does_not_skip_just_past_the_collinear_threshold():
    # 170 degrees: 10 degrees off straight, clearly outside the default
    # 2-degree threshold -- must be filleted. (Failure direction for the
    # skip test above: proves the skip is a real threshold, not a
    # blanket "never fillet a wide angle".)
    segs = fillet_polyline(_corner_points(170.0), 0.1)
    assert [s["shape"] for s in segs] == ["line", "arc", "line"]


def test_fillet_polyline_skips_near_reversal_corner():
    # 1 degree: almost a full doubling-back, inside the default 2-degree
    # reversal threshold -- must stay sharp.
    segs = fillet_polyline(_corner_points(1.0), 0.05)
    assert [s["shape"] for s in segs] == ["line", "line"]


def test_fillet_polyline_does_not_skip_just_past_the_reversal_threshold():
    # 5 degrees: outside the default 2-degree reversal threshold -- must
    # be filleted, even though the corner is still extreme.
    segs = fillet_polyline(_corner_points(5.0), 0.02)
    assert [s["shape"] for s in segs] == ["line", "arc", "line"]


def test_fillet_polyline_zero_radius_skips_every_corner():
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    segs = fillet_polyline(points, 0.0)
    assert [s["shape"] for s in segs] == ["line", "line"]


# ── property test: tangency, over randomized corners ─────────────────────


def test_fillet_corners_are_tangent_to_their_legs_randomized():
    """Property test (same discipline as test_pcb_realize.py's tangent-arc
    check, 500 randomized placements): for every accepted corner, the
    vector from the arc centre to each tangent point must be exactly
    perpendicular to that leg's own direction (the definition of
    tangency), and both tangent points must sit at exactly the corner's
    own effective radius from the centre -- true whether or not the
    setback clamp fired."""
    rng = random.Random(3)
    trials = 0
    for _ in range(500):
        theta_deg = rng.uniform(3.0, 177.0)  # stay clear of both skip zones
        len_in = rng.uniform(0.05, 10.0)
        len_out = rng.uniform(0.05, 10.0)
        r = rng.uniform(0.01, 3.0)
        vx, vy = rng.uniform(-20, 20), rng.uniform(-20, 20)
        rot = rng.uniform(0, 2 * math.pi)

        # Build prev/cur/next with an EXACT interior angle theta_deg,
        # independently of _corner_fillet's own trig, then translate +
        # rotate so the corner isn't always axis-aligned.
        e1_angle = rot
        e2_angle = rot + math.radians(180.0 - theta_deg)
        cur = (vx, vy)
        prev = (
            cur[0] + len_in * math.cos(e1_angle),
            cur[1] + len_in * math.sin(e1_angle),
        )
        nxt = (
            cur[0] + len_out * math.cos(e2_angle),
            cur[1] + len_out * math.sin(e2_angle),
        )

        corner = _corner_fillet(
            prev,
            cur,
            nxt,
            r,
            reversal_eps_rad=math.radians(2.0),
            collinear_eps_rad=math.radians(2.0),
        )
        if corner is None:
            continue
        trials += 1

        u1x, u1y = (prev[0] - cur[0]) / len_in, (prev[1] - cur[1]) / len_in
        u2x, u2y = (nxt[0] - cur[0]) / len_out, (nxt[1] - cur[1]) / len_out

        c1x = corner.center[0] - corner.t1[0]
        c1y = corner.center[1] - corner.t1[1]
        c2x = corner.center[0] - corner.t2[0]
        c2y = corner.center[1] - corner.t2[1]

        # perpendicularity: (centre - tangent point) . leg_direction == 0
        assert abs(c1x * u1x + c1y * u1y) < 1e-7, (theta_deg, len_in, len_out, r)
        assert abs(c2x * u2x + c2y * u2y) < 1e-7, (theta_deg, len_in, len_out, r)

        # both tangent points lie exactly at the corner's own radius
        assert dist(corner.center, corner.t1) == pytest.approx(
            corner.radius_mm, abs=1e-7
        )
        assert dist(corner.center, corner.t2) == pytest.approx(
            corner.radius_mm, abs=1e-7
        )

        # the effective radius used is never larger than requested
        assert corner.radius_mm <= r + 1e-9

        # setback never exceeds half of either adjoining leg
        t = dist(cur, corner.t1)
        assert t <= len_in / 2 + 1e-7
        assert t <= len_out / 2 + 1e-7
    assert trials > 300  # sanity: most sampled corners were real, non-skipped


# ── max_inward_deviation ──────────────────────────────────────────────────


def test_max_inward_deviation_zero_for_a_two_point_line():
    assert max_inward_deviation([(0.0, 0.0), (1.0, 0.0)], 0.5) == 0.0


def test_max_inward_deviation_zero_when_radius_is_zero():
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert max_inward_deviation(points, 0.0) == 0.0


def test_max_inward_deviation_zero_when_every_corner_is_skipped():
    # near-collinear -- no corner actually gets an arc.
    points = _corner_points(179.5)
    assert max_inward_deviation(points, 0.1) == 0.0


def test_max_inward_deviation_matches_closed_form_right_angle():
    """r * (1 - sin(45deg)) ~= 0.2929 * r at a right angle -- the exact
    figure the task spec states, independently recomputed here."""
    points = [(-1.0, 0.0), (0.0, 0.0), (0.0, 1.0)]
    r = 0.2
    expected = r * (1.0 - math.sin(math.radians(45.0)))
    assert expected == pytest.approx(0.2 * 0.2928932, abs=1e-6)
    assert max_inward_deviation(points, r) == pytest.approx(expected, rel=1e-9)


def test_max_inward_deviation_uses_the_clamped_radius_not_the_requested_one():
    """Same short-leg clamp scenario as the fillet_polyline clamp test:
    requesting r=0.2 on 0.1mm legs actually draws r_eff=0.05, and the
    deviation reported must reflect THAT, not the naive (unclamped)
    0.2 * (1 - sin(45deg)) figure -- a caller trusting the unclamped
    number would over-estimate the copper intrusion and could wrongly
    skip re-running DRC."""
    points = [(-0.1, 0.0), (0.0, 0.0), (0.0, 0.1)]
    requested_r = 0.2
    naive = requested_r * (1.0 - math.sin(math.radians(45.0)))
    actual = max_inward_deviation(points, requested_r)
    r_eff = 0.05
    expected = r_eff * (1.0 - math.sin(math.radians(45.0)))
    assert actual == pytest.approx(expected, rel=1e-6)
    assert actual < naive  # the whole point of using the clamped radius


def test_max_inward_deviation_independent_sagitta_cross_check_randomized():
    """The docstring's second derivation (arc sagitta from the chord
    joining its own two tangent points: r*(1 - cos(central_angle/2)),
    central_angle = pi - theta) computed HERE, independently of
    _corner_fillet's own formula, and checked to match
    max_inward_deviation's answer for many randomized corners."""
    rng = random.Random(4)
    trials = 0
    for _ in range(300):
        # theta/r/leg ranges chosen so the setback clamp provably never
        # fires (t_wanted = r/tan(theta/2) stays well under half the
        # shortest leg for every combination here) -- the clamp's own
        # effect on the deviation is covered separately, by
        # test_max_inward_deviation_uses_the_clamped_radius_not_the_
        # requested_one.
        theta_deg = rng.uniform(20.0, 160.0)
        r = rng.uniform(0.01, 0.3)
        len_in = rng.uniform(5.0, 20.0)  # long legs -- no clamp
        len_out = rng.uniform(5.0, 20.0)
        prev = (-len_in, 0.0)
        angle2 = math.radians(180.0 - theta_deg)
        nxt = (len_out * math.cos(angle2), len_out * math.sin(angle2))
        points = [prev, (0.0, 0.0), nxt]

        theta = math.radians(theta_deg)
        central_angle = math.pi - theta
        expected_sagitta = r * (1.0 - math.cos(central_angle / 2.0))

        got = max_inward_deviation(points, r)
        assert got == pytest.approx(expected_sagitta, rel=1e-6, abs=1e-9)
        trials += 1
    assert trials > 250


def test_max_inward_deviation_bounds_the_sampled_arc_against_its_legs():
    """The caller-facing promise: no point on any drawn arc departs from
    ITS OWN corner's two leg lines by more than that corner's own
    max_inward_deviation contribution (checked per corner, via
    _corner_fillet, not a single global bound), over randomized
    multi-corner polylines. This is the property that justifies using
    max_inward_deviation to decide whether a radius is provably safe."""
    rng = random.Random(5)
    checked = 0
    for _ in range(200):
        n = rng.randint(3, 5)
        pts = [(rng.uniform(-10, 10), rng.uniform(-10, 10)) for _ in range(n)]
        r = rng.uniform(0.05, 1.5)
        for i in range(1, len(pts) - 1):
            prev, cur, nxt = pts[i - 1], pts[i], pts[i + 1]
            corner = _corner_fillet(
                prev,
                cur,
                nxt,
                r,
                reversal_eps_rad=math.radians(2.0),
                collinear_eps_rad=math.radians(2.0),
            )
            if corner is None:
                continue
            checked += 1
            u1 = _unit_vec(prev, cur)
            u2 = _unit_vec(nxt, cur)
            for k in range(21):
                frac = k / 20.0
                a1 = math.atan2(
                    corner.t1[1] - corner.center[1], corner.t1[0] - corner.center[0]
                )
                a2 = math.atan2(
                    corner.t2[1] - corner.center[1], corner.t2[0] - corner.center[0]
                )
                sweep = (a2 - a1) % (2 * math.pi)
                if sweep > math.pi:
                    sweep -= 2 * math.pi
                ang = a1 + sweep * frac
                px = corner.center[0] + corner.radius_mm * math.cos(ang)
                py = corner.center[1] + corner.radius_mm * math.sin(ang)
                d1 = abs((px - cur[0]) * u1[1] - (py - cur[1]) * u1[0])
                d2 = abs((px - cur[0]) * u2[1] - (py - cur[1]) * u2[0])
                assert min(d1, d2) <= corner.deviation_mm + 1e-6
    assert checked > 50


def _unit_vec(a: Point, b: Point) -> Point:
    dx, dy = a[0] - b[0], a[1] - b[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n)


# ── the deviation budget cap ──────────────────────────────────────────────
# `_track_from_run` fillets a routed track and must keep the arcs inside the
# straight track's own copper envelope, so the router's clearance proof still
# holds. It used to do that with `radius *= budget / worst`, arguing that
# deviation is linear in the radius. That argument is false at a corner whose
# setback got CLAMPED: `_corner_fillet` back-solves `r_eff = t_max*tan(b)`,
# which ignores the requested radius, so scaling the request moves nothing.
# These tests pin the replacement (`max_radius_for_deviation`) and, crucially,
# measure the OUTPUT GEOMETRY rather than re-running the implementation's own
# deviation formula -- a checker that shares the producer's math checks
# nothing.
def _sample_arc(seg: dict, n: int = 64) -> list[Point]:
    """Points along a filleted arc segment, read off the emitted
    center/start/end/cw only -- no appeal to geom's internals."""
    cx, cy = seg["center"]
    sx, sy = seg["start"]
    ex, ey = seg["end"]
    r = math.hypot(sx - cx, sy - cy)
    a1 = math.atan2(sy - cy, sx - cx)
    a2 = math.atan2(ey - cy, ex - cx)
    sweep = (a2 - a1) % (2 * math.pi)
    if seg["cw"]:
        sweep -= 2 * math.pi
    return [
        (cx + r * math.cos(a1 + sweep * i / n), cy + r * math.sin(a1 + sweep * i / n))
        for i in range(n + 1)
    ]


def _dist_to_polyline(p: Point, pts: list[Point]) -> float:
    best = math.inf
    for a, b in itertools.pairwise(pts):
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2
        t = max(0.0, min(1.0, t))
        best = min(best, math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy)))
    return best


def _measured_deviation(run: list[Point], radius: float) -> float:
    """Max distance any emitted arc point sits from the ORIGINAL mitered
    path -- the physical quantity the budget is about."""
    worst = 0.0
    for seg in fillet_polyline(list(run), radius):
        if seg.get("shape") != "arc":
            continue
        for p in _sample_arc(seg):
            worst = max(worst, _dist_to_polyline(p, run))
    return worst


def _corner(interior_deg: float, leg: float) -> list[Point]:
    """A single-vertex run whose INTERIOR angle is `interior_deg`. The
    incoming leg points back along 180 deg, so the outgoing leg sits at
    `180 - interior` for the angle at the vertex to be `interior`."""
    out = math.pi - math.radians(interior_deg)
    return [
        (-leg, 0.0),
        (0.0, 0.0),
        (leg * math.cos(out), leg * math.sin(out)),
    ]


def test_proportional_scaling_is_a_no_op_on_a_setback_clamped_corner():
    """Pins the DEFECT the cap replaced, so nobody reinstates the cheap
    version. At a clamped corner the drawn radius is independent of the
    requested one, so `radius *= budget/worst` changes the deviation by
    exactly zero -- not 'by a little', zero."""
    w = 0.25
    budget = w / 2.0
    run = _corner(60.0, 4.0 * w)
    radius = 1.5 * w  # RealizeConfig.fillet_radius_tracks default x width

    worst = max_inward_deviation(run, radius)
    assert worst > budget, "fixture must actually exceed the budget"

    scaled = radius * (budget / worst)
    assert max_inward_deviation(run, scaled) == pytest.approx(worst, abs=1e-12)
    assert max_inward_deviation(run, scaled) > budget


def test_max_radius_for_deviation_holds_the_budget_on_a_clamped_corner():
    w = 0.25
    budget = w / 2.0
    run = _corner(60.0, 4.0 * w)
    cap = max_radius_for_deviation(run, budget)
    radius = min(1.5 * w, cap)

    # measured off the emitted arcs, not off geom's own formula
    assert _measured_deviation(run, radius) <= budget + 1e-9


@pytest.mark.parametrize("interior_deg", [15.0, 30.0, 60.0, 90.0, 120.0, 170.0])
@pytest.mark.parametrize("leg_mult", [0.6, 1.0, 4.0, 50.0])
def test_capped_radius_holds_the_budget_across_angles_and_leg_lengths(
    interior_deg: float, leg_mult: float
):
    """Both regimes matter: short legs force the setback clamp, long legs
    leave it unclamped, and the cap has to be correct in both."""
    w = 0.25
    budget = w / 2.0
    run = _corner(interior_deg, leg_mult * w)
    radius = min(1.5 * w, max_radius_for_deviation(run, budget))
    assert _measured_deviation(run, radius) <= budget + 1e-9


def test_the_cap_is_tight_not_merely_safe_when_the_clamp_does_not_fire():
    """A cap that just returned something tiny would pass every budget
    assertion above while destroying the rounding. On a long-legged corner
    (no setback clamp) the cap must land the deviation ON the budget."""
    budget = 0.125
    run = _corner(90.0, 50.0)
    cap = max_radius_for_deviation(run, budget)
    assert max_inward_deviation(run, cap) == pytest.approx(budget, rel=1e-9)


def test_max_radius_for_deviation_is_unbounded_when_no_corner_is_filleted():
    """Two points, or an all-skipped polyline, must not constrain the
    caller -- it `min()`s this against its own preference."""
    assert max_radius_for_deviation([(0.0, 0.0), (1.0, 0.0)], 0.1) == math.inf
    straight = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]  # collinear -> skipped
    assert max_radius_for_deviation(straight, 0.1) == math.inf
