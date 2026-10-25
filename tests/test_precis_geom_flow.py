"""precis_geom.flow + precis_geom.physics.evaluate(['flow']) — the DB-free
half of se-flow-kernel slice 4a: the cad → Domain adapter a workspace
script runs, the physics-only Outcome, common random numbers, purity,
hard measures, the inline budget, and parity with the server's
FLOW_COMPUTERS (one implementation).
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from precis.cad.graph import Design
from precis.cad.primitives import CircularFrustum, Placed, PolyFrustum
from precis.cad.vec import identity, vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput
from precis_geom import flow as geom_flow
from precis_geom.flow import (
    GapDomain,
    PortPatch,
    domain_from_solid,
    gap_cells,
    region_ranges,
)
from precis_geom.physics import (
    FLOW_MEASURANDS,
    INLINE_CELLS,
    INLINE_TRACE_WORK,
    Outcome,
    evaluate,
)
from precis_se import flow as se_flow
from precis_se.measures import MeasureSpec
from precis_se.ops import SeTree, apply_ops
from tests.test_se_flow import (
    D_V_STAR,
    HOLE_R,
    HOLES,
    TIP,
    H,
    L,
    W,
    _hex_design,
    _tip_patch,
)

NX = 80


def _floor_region() -> PortPatch:
    """A rectangle on the chamber lid covering the central band."""
    return PortPatch(
        point=np.array([L / 2, W / 2, H]),
        normal=np.array([0.0, 0.0, 1.0]),
        u=np.array([1.0, 0.0, 0.0]),
        v=np.array([0.0, 1.0, 0.0]),
        half_u=0.03,
        half_v=0.01,
    )


def _spec(**extra: Any) -> dict[str, Any]:
    """The hex chamber spec in its JSON (manifest) form."""
    return {
        "inlet": _tip_patch(0.0, -1.0).to_dict(),
        "outlet": _tip_patch(L, 1.0).to_dict(),
        "dv": D_V_STAR,
        "profile": "plug",
        "n": 200,
        "evenness_region": _floor_region().to_dict(),
        **extra,
    }


@pytest.fixture(scope="module")
def hex_shape() -> Any:
    return _hex_design()


@pytest.fixture(scope="module")
def hex_outcome(hex_shape: Any) -> Outcome:
    return evaluate(hex_shape, ["flow"], _spec(), seed=3, cells=NX)


def test_se_reexports_the_wheel_adapter() -> None:
    """One implementation: se's names are the wheel module's objects."""
    assert se_flow.domain_from_solid is geom_flow.domain_from_solid
    assert se_flow.PortPatch is geom_flow.PortPatch
    assert se_flow.Chamber is GapDomain


def test_port_patch_round_trip_and_refusals() -> None:
    p = PortPatch.coerce(_tip_patch(L, 1.0).to_dict(), "inlet")
    q = _tip_patch(L, 1.0)
    for k in ("point", "normal", "u", "v"):
        assert np.array_equal(getattr(p, k), getattr(q, k))
    assert (p.half_u, p.half_v) == (q.half_u, q.half_v)
    with pytest.raises(BadInput, match="misses half_v"):
        PortPatch.coerce({k: v for k, v in q.to_dict().items() if k != "half_v"}, "x")
    with pytest.raises(BadInput, match="3-vector"):
        PortPatch.coerce({**q.to_dict(), "u": [1, 0]}, "x")
    with pytest.raises(BadInput, match="PortPatch or a dict"):
        PortPatch.coerce("patch:c.side1@0,0+1x1", "x")


def test_gap_domain_from_cad_solid(hex_shape: Any) -> None:
    """The cad (Design, Expr) hex builds a gap: thin z, x* along L,
    deepened floor under the holes, ports on both tips."""
    gap = domain_from_solid(hex_shape, _tip_patch(0.0, -1.0), _tip_patch(L, 1.0), nx=NX)
    assert (gap.thin_axis, gap.x_axis, gap.y_axis) == (2, 0, 1)
    assert math.isclose(gap.L, L) and math.isclose(gap.h0, H)
    assert gap.depth_star.max() > 1.0
    assert gap.domain.ports.inlet.any() and gap.domain.ports.outlet.any()
    assert gap_cells(hex_shape, NX) == gap.domain.mask.shape[::-1]


def test_outcome_is_physics_only(hex_outcome: Outcome) -> None:
    """measures ⊆ the registered flow measurands — no counts, no
    material or process terms; every value in its physical range."""
    assert set(hex_outcome.measures) <= set(FLOW_MEASURANDS)
    assert set(hex_outcome.measures) == set(FLOW_MEASURANDS)
    for banned in ("hole-count", "holes", "n_holes", "material", "mass", "cost"):
        assert banned not in hex_outcome.measures
    m = hex_outcome.measures
    assert 0.0 < m["breakthrough-fraction"] <= 1.0
    assert m["residence-time-t0"] > 0.0
    assert m["resistance-ratio"] < 1.0  # holes deepen the gap: less resistance
    assert 0.0 <= m["evenness"] <= 1.0
    assert hex_outcome.cards == {"flow": None}  # no fluid inputs → no card
    assert hex_outcome.hard_ok and hex_outcome.failures == ()
    json.dumps(hex_outcome.to_dict())  # manifest-safe


def test_common_random_numbers(hex_shape: Any, hex_outcome: Outcome) -> None:
    """Same (shape, spec, seed, cells) → bitwise-equal Outcome and swarm."""
    again = evaluate(hex_shape, ["flow"], _spec(), seed=3, cells=NX)
    assert again == hex_outcome
    assert again.to_dict() == hex_outcome.to_dict()
    assert again.paths is not None and hex_outcome.paths is not None
    assert np.array_equal(
        again.paths.residence, hex_outcome.paths.residence, equal_nan=True
    )
    assert np.array_equal(
        again.paths.cross_star, hex_outcome.paths.cross_star, equal_nan=True
    )
    other = evaluate(hex_shape, ["flow"], _spec(), seed=4, cells=NX)
    assert other.paths is not None
    assert not np.array_equal(
        other.paths.residence, hex_outcome.paths.residence, equal_nan=True
    )


def test_evaluate_is_pure(hex_shape: Any, hex_outcome: Outcome) -> None:
    """spec is never mutated; PortPatch objects and their dict form give
    the same outcome; the shape's design is untouched."""
    spec = _spec()
    before = json.dumps(spec, sort_keys=True)
    design, _expr = hex_shape
    n_inst = len(design.instances)
    out = evaluate(
        hex_shape,
        ["flow"],
        {
            **spec,
            "inlet": _tip_patch(0.0, -1.0),
            "outlet": _tip_patch(L, 1.0),
            "evenness_region": _floor_region(),
        },
        seed=3,
        cells=NX,
    )
    assert json.dumps(spec, sort_keys=True) == before
    assert len(design.instances) == n_inst
    assert out == hex_outcome


def test_hard_measures(hex_shape: Any) -> None:
    out = evaluate(
        hex_shape,
        ["flow"],
        _spec(
            evenness_region=None,
            measures=[
                {
                    "name": "rr",
                    "measurand": "resistance-ratio",
                    "strength": "hard",
                    "max_value": 0.1,
                },
                {
                    "measurand": "breakthrough-fraction",
                    "strength": "hard",
                    "min_value": 0.0,
                },
                {
                    "name": "ev",
                    "measurand": "evenness",
                    "strength": "hard",
                    "min_value": 0.5,
                },
                {"measurand": "residence-time-t0", "strength": "soft"},
            ],
        ),
        seed=0,
        cells=NX,
    )
    assert not out.hard_ok
    by_name = {f.name: f for f in out.failures}
    assert set(by_name) == {"rr", "ev"}
    assert by_name["rr"].value == out.measures["resistance-ratio"]
    assert by_name["rr"].max_value == 0.1
    # no region → evenness has a reason, and a hard bound on it fails
    assert (
        "evenness" not in out.measures and "evenness_region" in out.reasons["evenness"]
    )
    assert by_name["ev"].value is None and by_name["ev"].reason


def test_refusals(hex_shape: Any) -> None:
    with pytest.raises(BadInput) as exc:
        evaluate(
            hex_shape, ["flow"], _spec(measures=[{"measurand": "hole-count"}]), cells=NX
        )
    assert exc.value.next and "objective" in exc.value.next
    with pytest.raises(BadInput, match="unknown physics"):
        evaluate(hex_shape, ["stokes"], _spec(), cells=NX)
    spec = _spec()
    del spec["dv"]
    with pytest.raises(BadInput, match="dv"):
        evaluate(hex_shape, ["flow"], spec, cells=NX)
    with pytest.raises(BadInput, match="unknown spec key"):
        evaluate(hex_shape, ["flow"], _spec(holes=6), cells=NX)
    with pytest.raises(BadInput, match="hard measure needs"):
        evaluate(
            hex_shape,
            ["flow"],
            _spec(measures=[{"measurand": "evenness", "strength": "hard"}]),
            cells=NX,
        )


def test_budget_refuses_above_inline_cells(hex_shape: Any) -> None:
    nx = 1200  # 1200 × 360 cells on the 0.1 × 0.03 hex
    assert gap_cells(hex_shape, nx)[0] * gap_cells(hex_shape, nx)[1] > INLINE_CELLS
    with pytest.raises(BadInput, match="INLINE_CELLS") as exc:
        evaluate(hex_shape, ["flow"], _spec(), cells=nx)
    assert exc.value.next and "budget='job'" in exc.value.next
    with pytest.raises(BadInput, match="job lane") as exc2:
        evaluate(hex_shape, ["flow"], _spec(), cells=nx, budget="job")
    assert exc2.value.next and "cells=" in exc2.value.next


def test_regime_card_from_fluid(hex_shape: Any) -> None:
    out = evaluate(
        hex_shape,
        ["flow"],
        _spec(n=50, fluid={"mu": 1e-3, "rho": 1000.0, "u0": 0.01}),
        cells=NX,
    )
    card = out.cards["flow"]
    assert card is not None and card.ok
    assert out.to_dict()["cards"]["flow"]["ok"] is True
    with pytest.raises(BadInput, match="exactly one of u0"):
        evaluate(hex_shape, ["flow"], _spec(fluid={"mu": 1e-3, "rho": 1e3}), cells=NX)


def test_knudsen_route_keeps_its_next(hex_shape: Any) -> None:
    """gr515473: BadInput is a ValueError — evaluate's ``except ValueError``
    wrap must not swallow ``_card``'s knudsen-route refusal and drop the
    next= that names the way out (the nano propagator)."""
    with pytest.raises(BadInput, match="free-molecular") as exc:
        evaluate(
            hex_shape,
            ["flow"],
            _spec(fluid={"mu": 1e-3, "rho": 1e3, "u0": 0.01, "mean_free_path": 1.0}),
            cells=NX,
        )
    assert exc.value.next and "nano propagator" in exc.value.next


def _box_chamber_tree() -> SeTree:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "c", "envelope": "box:w0.1d0.03h0.001"},
            {
                "op": "add_pocket",
                "block": "c",
                "name": "flow",
                "shape": (
                    "flow:in=patch:c.side3@0,0+0.006x0.001;"
                    "out=patch:c.side1@0,0+0.006x0.001;dv=6;profile=plug"
                ),
                "regions": [
                    {"selector": "patch:c.side1@0,0+0.006x0.001"},
                    {"selector": "patch:c.bottom@0,0+0.06x0.02"},
                ],
            },
        ],
    )
    return tree


def test_server_computers_equal_evaluate() -> None:
    """The server's FLOW_COMPUTERS number is evaluate's number for the
    same chamber, ports, seed and cells — exactly (one implementation)."""
    tree = _box_chamber_tree()
    placed = se_flow._chamber_placed(None, tree, "c")
    assert placed is not None
    out_patch = "patch:c.side1@0,0+0.006x0.001"
    floor = "patch:c.bottom@0,0+0.06x0.02"
    out = evaluate(
        placed,
        ["flow"],
        {
            "inlet": se_flow.patch_port(tree, "c", "patch:c.side3@0,0+0.006x0.001"),
            "outlet": se_flow.patch_port(tree, "c", out_patch),
            "dv": 6.0,
            "profile": "plug",
            "evenness_region": se_flow.patch_port(tree, "c", floor),
        },
        seed=0,
        cells=geom_flow.DEFAULT_NX,
    )
    for slug in FLOW_MEASURANDS:
        datum = floor if slug == "evenness" else out_patch
        m = MeasureSpec(block="c", name=slug, datum=datum, measurand=slug, unit="ratio")
        # flat chamber: no crossings — evenness is a reason on both sides
        assert se_flow.FLOW_COMPUTERS[slug](tree, m) == out.measures.get(slug), slug


# ── review follow-ups (slice-4a review) ────────────────────────────────


def _flat_slab() -> Placed:
    """A flat rectangular gap L × W × H — the analytic case."""
    ring = [(0.0, 0.0), (L, 0.0), (L, W), (0.0, W)]
    return Placed(prim=PolyFrustum(ring, ring, H), xform=identity())


def _end_patch(x: float, sign: float) -> PortPatch:
    """A port rectangle covering the whole x=0 / x=L end face."""
    return PortPatch(
        point=np.array([x, W / 2, H / 2]),
        normal=np.array([sign, 0.0, 0.0]),
        u=np.array([0.0, 1.0, 0.0]),
        v=np.array([0.0, 0.0, 1.0]),
        half_u=W / 2,
        half_v=H / 2,
    )


def test_flat_slab_analytic() -> None:
    """A flat slab with full-end ports: h0 = H, no floor topography,
    resistance-ratio 1.0, plug breakthrough 1 — the analytic case."""
    out = evaluate(
        _flat_slab(),
        ["flow"],
        {
            "inlet": _end_patch(0.0, -1.0),
            "outlet": _end_patch(L, 1.0),
            "dv": 6.0,
            "profile": "plug",
            "n": 100,
        },
        seed=0,
        cells=NX,
    )
    gap = out.gap
    assert gap is not None
    assert math.isclose(gap.h0, H)
    assert np.all(gap.depth_star == pytest.approx(0.0))
    m = out.measures
    assert m["resistance-ratio"] == pytest.approx(1.0)
    # the tracking plane is the ceiling (psi = z_plane_star): nothing
    # deepens the flat floor, so no particle ever climbs — 0.0 here, and
    # every particle resolves by exiting instead
    assert m["breakthrough-fraction"] == 0.0
    assert out.paths is not None and out.paths.resolved_fraction == 1.0


def _hex_scaled(k: float) -> Any:
    """The cfdtest hex void at scale ``k`` — every coordinate × k."""
    ring = [
        (TIP * k, 0.0),
        ((L - TIP) * k, 0.0),
        (L * k, W / 2 * k),
        ((L - TIP) * k, W * k),
        (TIP * k, W * k),
        (0.0, W / 2 * k),
    ]
    d = Design()
    parts = [d.prim("chamber", PolyFrustum(ring, ring, H * k))]
    for i, (hx, hy, hd) in enumerate(HOLES):
        parts.append(
            d.prim(
                f"hole{i}",
                CircularFrustum(HOLE_R * k, HOLE_R * k, (hd + 5e-4) * k),
                cad_pose(vec3(hx * k, hy * k, -hd * k), vec3(0, 0, 0)),
            )
        )
    return d, d.merge(*parts)


def _tip_patch_scaled(x: float, sign: float, k: float) -> PortPatch:
    p = _tip_patch(x * k, sign)
    return PortPatch(
        point=np.array([x * k, W / 2 * k, H / 2 * k]),
        normal=p.normal,
        u=p.u,
        v=p.v,
        half_u=p.half_u * k,
        half_v=p.half_v * k,
    )


def test_size_invariance(hex_outcome: Outcome) -> None:
    """The same shape at 2× (a power of two — exact in binary) gives a
    bitwise-equal outcome: the kernel is dimension-free."""
    k = 2.0
    out = evaluate(
        _hex_scaled(k),
        ["flow"],
        {
            "inlet": _tip_patch_scaled(0.0, -1.0, k),
            "outlet": _tip_patch_scaled(L, 1.0, k),
            "dv": D_V_STAR,
            "profile": "plug",
            "n": 200,
            "evenness_region": PortPatch(
                point=np.array([L * k / 2, W * k / 2, H * k]),
                normal=np.array([0.0, 0.0, 1.0]),
                u=np.array([1.0, 0.0, 0.0]),
                v=np.array([0.0, 1.0, 0.0]),
                half_u=0.03 * k,
                half_v=0.01 * k,
            ),
        },
        seed=3,
        cells=NX,
    )
    assert out.to_dict() == hex_outcome.to_dict()


def _port_flux(field: Any, cells: np.ndarray) -> float:
    """Net reduced flux out of ``cells`` across faces to inside
    neighbours (positive = the port drives flow into the domain)."""
    d = field.domain
    t = np.where(d.mask, d.h_star**3, 0.0)
    p = np.nan_to_num(field.p_star, nan=0.0)
    ny, nx = d.shape
    q = 0.0
    for j, i in zip(*np.where(cells)):
        for nj, ni in ((j, i - 1), (j, i + 1), (j - 1, i), (j + 1, i)):
            if nj < 0 or ni < 0 or nj >= ny or ni >= nx:
                continue
            if not d.mask[nj, ni] or cells[nj, ni]:
                continue
            s = t[j, i] + t[nj, ni]
            face_t = 2.0 * t[j, i] * t[nj, ni] / s if s > 0 else 0.0
            q += face_t * (p[j, i] - p[nj, ni])
    return float(q)


def test_flux_in_equals_flux_out() -> None:
    """Mass conservation and the closed-form R*: on a uniform rectangle
    the reduced resistance is (nx−1)/ny, and the flux the inlet drives
    in equals the flux the outlet draws out — on the flat slab and on
    the topographed hex."""
    from precis_flow import solve

    for gap in (
        domain_from_solid(
            _flat_slab(), _end_patch(0.0, -1.0), _end_patch(L, 1.0), nx=NX
        ),
        domain_from_solid(
            _hex_design(), _tip_patch(0.0, -1.0), _tip_patch(L, 1.0), nx=NX
        ),
    ):
        field = solve(gap.domain)
        ny, nx = gap.domain.mask.shape
        if gap.depth_star.max() == 0.0:
            assert field.r_star == pytest.approx((nx - 1) / ny, rel=1e-9)
        q_in = _port_flux(field, gap.domain.ports.inlet)
        q_out = -_port_flux(field, gap.domain.ports.outlet)
        assert q_in > 0.0
        assert q_in == pytest.approx(q_out, rel=1e-9)


def test_rotated_region_ranges() -> None:
    """A 45° patch's lateral footprint is centre ± |hu·u| + |hv·v|."""
    gap = domain_from_solid(
        _flat_slab(), _end_patch(0.0, -1.0), _end_patch(L, 1.0), nx=NX
    )
    s = math.sqrt(0.5)
    a, b = 0.01, 0.004
    patch = PortPatch(
        point=np.array([L / 2, W / 2, H]),
        normal=np.array([0.0, 0.0, 1.0]),
        u=np.array([s, s, 0.0]),
        v=np.array([-s, s, 0.0]),
        half_u=a,
        half_v=b,
    )
    got = region_ranges(gap, patch)
    assert not isinstance(got, str)
    (x_lo, x_hi), (y_lo, y_hi) = got
    e = (a + b) * s / gap.L
    cx, cy = (L / 2 - gap.lo[gap.x_axis]) / gap.L, (W / 2 - gap.lo[gap.y_axis]) / gap.L
    assert x_lo == pytest.approx(cx - e) and x_hi == pytest.approx(cx + e)
    assert y_lo == pytest.approx(cy - e) and y_hi == pytest.approx(cy + e)


def test_port_refusals() -> None:
    """A port off the solid or overlapping the other port refuses with
    next= naming the fix — before any solve runs."""
    off = PortPatch(
        point=np.array([-0.05, W / 2, H / 2]),
        normal=np.array([-1.0, 0.0, 0.0]),
        u=np.array([0.0, 1.0, 0.0]),
        v=np.array([0.0, 0.0, 1.0]),
        half_u=W / 2,
        half_v=H / 2,
    )
    with pytest.raises(BadInput, match="claims no inside cells") as exc:
        domain_from_solid(_flat_slab(), off, _end_patch(L, 1.0), nx=NX)
    assert exc.value.next
    same = _end_patch(0.0, -1.0)
    with pytest.raises(BadInput, match="same cells"):
        domain_from_solid(_flat_slab(), same, same, nx=NX)


def test_seed_z_and_tracer_budget_refusals() -> None:
    spec = {
        "inlet": _end_patch(0.0, -1.0),
        "outlet": _end_patch(L, 1.0),
        "dv": 6.0,
    }
    for bad in ([0.9, 0.1], [0.0, 1.5], [-0.1, 0.5]):
        with pytest.raises(BadInput, match="ordered fractions"):
            evaluate(_flat_slab(), ["flow"], {**spec, "seed_z": bad}, cells=NX)
    out = evaluate(_flat_slab(), ["flow"], {**spec, "seed_z": [0.0, 1.0]}, cells=NX)
    assert out.measures["residence-time-t0"] > 0.0
    over = INLINE_TRACE_WORK + 1
    with pytest.raises(BadInput, match="INLINE_TRACE_WORK") as exc:
        evaluate(_flat_slab(), ["flow"], {**spec, "n": over}, cells=NX)
    assert exc.value.next and "budget='job'" in exc.value.next
    with pytest.raises(BadInput, match="job lane"):
        evaluate(_flat_slab(), ["flow"], {**spec, "n": over}, cells=NX, budget="job")
    with pytest.raises(BadInput, match="INLINE_TRACE_WORK"):
        evaluate(
            _flat_slab(),
            ["flow"],
            {**spec, "n": 1, "frames": INLINE_TRACE_WORK},
            cells=NX,
        )


def test_evenness_no_crossings_is_a_reason_not_zero() -> None:
    """A region no crossing lands in gives a reason, never a 0.0 value
    (the skill's contract)."""
    tiny_inlet_corner = PortPatch(
        point=np.array([0.002, 0.001, H]),
        normal=np.array([0.0, 0.0, 1.0]),
        u=np.array([1.0, 0.0, 0.0]),
        v=np.array([0.0, 1.0, 0.0]),
        half_u=0.001,
        half_v=0.0005,
    )
    out = evaluate(
        _flat_slab(),
        ["flow"],
        {
            "inlet": _end_patch(0.0, -1.0),
            "outlet": _end_patch(L, 1.0),
            "dv": 6.0,
            "profile": "plug",
            "n": 60,
            "evenness_region": tiny_inlet_corner,
        },
        seed=0,
        cells=NX,
    )
    assert "evenness" not in out.measures
    assert "no particle crossed" in out.reasons["evenness"]
