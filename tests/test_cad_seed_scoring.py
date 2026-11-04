"""Batch seed scoring preserves the scalar minimiser's geometry and coverage."""

from __future__ import annotations

from itertools import product

import numpy as np
import pytest
from numpy.typing import NDArray

from precis.cad import relate
from precis.cad.dsl import build_config
from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.vec import Vec3, pose, translation, vec3


def _pair(case: str, scale: float) -> tuple[Design, Expr, Expr, Vec3]:
    design = Design()
    offset = scale * vec3(3, -2, 1)
    frame = pose(scale * vec3(7, -4, 3), vec3(0.3, -0.5, 0.7))
    shifted_frame = translation(*(-offset)).compose(frame)
    a: Expr
    b: Expr
    if case == "thin-overlap":
        box = build_config(f"box:w{10 * scale}d{10 * scale}h{10 * scale}")
        a = design.prim("a", box, shifted_frame)
        b = design.prim("b", box, frame.compose(translation(9.77 * scale, 0, 0)))
    else:
        a = design.prim(
            "shaft",
            build_config(f"cyl:r{5 * scale}h{20 * scale}"),
            shifted_frame.compose(translation(0, 0, -5 * scale)),
        )
        plate = design.prim(
            "plate", build_config(f"cyl:r{20 * scale}h{10 * scale}"), frame
        )
        bore = design.prim(
            "bore",
            build_config(f"cyl:r{5.1 * scale}h{12 * scale}"),
            frame.compose(translation(0, 0, -scale)),
        )
        b = design.subtract(plate, bore)
    return design, a, b, offset


@pytest.mark.parametrize(
    ("case", "scale", "interfering"),
    [
        pytest.param("thin-overlap", 1e-3, True, id="small-transformed-overlap"),
        pytest.param("thin-overlap", 1e3, True, id="large-transformed-overlap"),
        pytest.param("bore", 1.0, False, id="transformed-carved-clearance"),
    ],
)
def test_batched_gap_matches_scalar_seed_scoring(
    monkeypatch: pytest.MonkeyPatch, case: str, scale: float, interfering: bool
) -> None:
    design, a, b, offset = _pair(case, scale)
    lo, hi = relate._region(design, [a, b])
    region = (np.minimum(lo, lo + offset), np.maximum(hi, hi + offset))
    batched, _ = relate._min_max_sdf(design, a, b, offset, region)
    reference_calls = 0

    def scalar_scores(
        design: Design, expr: Expr, points: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        nonlocal reference_calls
        reference_calls += 1
        return np.array([relate.component_sdf(design, expr, p) for p in points])

    # Reuse the production seed selection/descent with the old pointwise
    # scoring, so the reference does not duplicate the minimiser algorithm.
    monkeypatch.setattr(relate, "component_sdf_np", scalar_scores)
    scalar, _ = relate._min_max_sdf(design, a, b, offset, region)
    assert reference_calls > 0, "full minimisation must use the batch SDF path"
    assert 2 * batched == pytest.approx(2 * scalar, rel=1e-4, abs=1e-6 * scale)
    assert (batched < 0) == (scalar < 0) == interfering


def test_full_grid_is_scored_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = Design()
    a = design.prim("a", build_config("sphere:r1"))
    b = design.prim("b", build_config("sphere:r2"), translation(3, 0, 0))
    lo, hi = vec3(-2, -3, -4), vec3(5, 6, 7)
    offset = vec3(0.25, -0.5, 0.75)
    batches: dict[str, list[NDArray[np.float64]]] = {"a": [], "b": []}
    batch_sdf = relate.component_sdf_np
    scalar_sdf = relate.component_sdf
    scalar_calls = 0

    def record_batch(
        design: Design, expr: Expr, points: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        batches["a" if expr is a else "b"].append(points.copy())
        return batch_sdf(design, expr, points)

    def record_scalar(design: Design, expr: Expr, point: Vec3) -> float:
        nonlocal scalar_calls
        scalar_calls += 1
        return scalar_sdf(design, expr, point)

    # Out-of-region structural candidates exercise clipping before the
    # moving component's offset is subtracted. Grid points remain untouched.
    monkeypatch.setattr(relate, "_contact_seeds", lambda *args: [lo - 10, hi + 10])
    monkeypatch.setattr(relate, "component_sdf_np", record_batch)
    monkeypatch.setattr(relate, "component_sdf", record_scalar)
    relate._min_max_sdf(design, a, b, offset, (lo, hi), iters=0)

    assert relate.SEED_GRID == 14
    assert 2 <= sum(map(len, batches.values())) <= 8
    # Descent still uses scalar SDFs; scoring 2 * 14³ seeds must not do so.
    assert 0 < scalar_calls < 100
    expected_grid = np.array(
        list(product(*(np.linspace(l, h, 14) for l, h in zip(lo, hi, strict=True))))
    )
    expected = np.vstack([(lo + hi) / 2, lo, hi, expected_grid])
    for name, shift in (("a", offset), ("b", vec3(0, 0, 0))):
        assert batches[name]
        points = np.concatenate(batches[name])
        assert points.shape == (14**3 + 3, 3)
        np.testing.assert_allclose(points, expected - shift, rtol=0, atol=1e-14)


def test_first_structural_contact_returns_before_descent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = Design()
    a = design.prim("a", build_config("box:w10d10h10"))
    b = design.prim("b", build_config("box:w10d10h10"))

    def unexpected_work(*args: object, **kwargs: object) -> None:
        pytest.fail("a structural seed already proves contact")

    # The batch scores structural seeds and grid together (one call per
    # body); the first seed <= 0 in order still wins, before any descent.
    real_np = relate.component_sdf_np
    calls: list[int] = []

    def record(design: Design, expr: Expr, points: NDArray[np.float64]) -> NDArray:
        calls.append(len(points))
        return real_np(design, expr, points)

    monkeypatch.setattr(relate, "component_sdf_np", record)
    monkeypatch.setattr(relate, "_descend", unexpected_work)
    # The region midpoint is outside both boxes; the first body-centroid
    # seed is inside, so its witness pins the structural-first return order.
    half, point = relate._min_max_sdf(
        design,
        a,
        b,
        vec3(0, 0, 0),
        (vec3(-10, -10, -10), vec3(30, 30, 30)),
        stop_at_contact=True,
    )
    assert half < 0
    np.testing.assert_allclose(point, vec3(0, 0, 5), atol=1e-14)
    # One batched call per body, covering every seed — never per-point.
    assert calls and all(n > 1 for n in calls) and len(calls) == 2
