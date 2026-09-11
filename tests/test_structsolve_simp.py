"""precis.structsolve.simp — the pure SIMP engine, the AM overhang filter
and the gyroid fill (docs/backlog/structural-solution-space.md slice 4).
Unit-agnostic, store-free: arrays in, arrays out.

Every grid here is deliberately tiny (at most ~16x4x4 elements wherever a
finite-element solve is involved) so the whole module stays a couple of
seconds of CI. The lattice tests use a larger grid because they run no
solve at all — they are a handful of trig evaluations.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structsolve import (
    LatticeResult,
    SimpResult,
    lattice_fill,
    overhang_violations,
    simp_optimize,
)
from precis.structsolve.simp import (
    _am_sweep_exact,
    _build_problem,
    _compliance_and_sensitivity,
    _filter_offsets,
    _filter_weight_sums,
    _sensitivity_filter,
    hex_element_stiffness,
)

# --------------------------------------------------------------------------
# shared tiny problems
# --------------------------------------------------------------------------


def _face_supports(ny: int, nz: int) -> list[tuple[tuple[int, int, int], str]]:
    """Every node of the x = 0 face, fully clamped."""
    return [((0, j, k), "xyz") for j in range(ny + 1) for k in range(nz + 1)]


def _cantilever() -> tuple[
    np.ndarray,
    list[tuple[tuple[int, int, int], tuple[float, float, float]]],
    list[tuple[tuple[int, int, int], str]],
]:
    """16x4x4 block, x = 0 face clamped, unit downward (-z) load spread along
    the free end's mid-height edge. 256 elements — a few hundred ms a run."""
    nx, ny, nz = 16, 4, 4
    domain = np.ones((nx, ny, nz), dtype=bool)
    share = -1.0 / (ny + 1)
    loads = [((nx, j, nz // 2), (0.0, 0.0, share)) for j in range(ny + 1)]
    return domain, loads, _face_supports(ny, nz)


def _node_index(shape: tuple[int, int, int], i: int, j: int, k: int) -> int:
    _, ny, nz = shape
    return (i * (ny + 1) + j) * (nz + 1) + k


# --------------------------------------------------------------------------
# 1. element stiffness invariants
# --------------------------------------------------------------------------


def test_hex_element_stiffness_invariants() -> None:
    """The 24x24 is integrated from Bᵀ C B rather than transcribed, so it is
    checkable: symmetric, positive semidefinite, and with exactly the six
    rigid-body zero-energy modes an unrestrained free body must have."""
    ke = hex_element_stiffness()
    assert ke.shape == (24, 24)
    assert np.array_equal(ke, ke.T)

    eig = np.linalg.eigvalsh(ke)
    cutoff = 1e-10 * float(eig.max())
    assert int(np.count_nonzero(np.abs(eig) < cutoff)) == 6
    assert float(eig.min()) > -cutoff  # positive semidefinite
    assert float(np.sort(eig)[6]) > cutoff  # the 7th mode carries real energy


def test_hex_element_stiffness_rigid_body_modes_are_the_null_space() -> None:
    """Name the six modes explicitly: three translations and three
    infinitesimal rotations about the element centre, all at zero energy."""
    ke = hex_element_stiffness()
    corners = np.array(
        [[(n >> 2) & 1, (n >> 1) & 1, n & 1] for n in range(8)], dtype=float
    )
    centred = corners - 0.5
    modes = []
    for axis in range(3):
        translation = np.zeros((8, 3))
        translation[:, axis] = 1.0
        modes.append(translation.reshape(-1))
        omega = np.zeros(3)
        omega[axis] = 1.0
        modes.append(np.cross(np.broadcast_to(omega, (8, 3)), centred).reshape(-1))
    for mode in modes:
        energy = float(mode @ ke @ mode)
        assert abs(energy) < 1e-12, energy


def test_hex_element_stiffness_scales_linearly_with_the_voxel_pitch() -> None:
    """B goes as 1/h and dV as h³, so K goes as h."""
    assert np.allclose(hex_element_stiffness(h=2.5), 2.5 * hex_element_stiffness())


# --------------------------------------------------------------------------
# 2. FEA sanity against a closed form
# --------------------------------------------------------------------------


def test_solid_bar_tip_displacement_matches_fl_over_ea() -> None:
    """An 8x2x2 all-solid bar under end tension should extend by FL/(EA).

    The discretisation is deliberately crude and biases *stiff*: clamping all
    three DOFs on the x = 0 face suppresses the Poisson contraction a real
    bar has there, and trilinear hexes cannot represent that transition. The
    band asserted is 5%; the measured error is about 1.4% low, in the
    expected direction.
    """
    nx, ny, nz = 8, 2, 2
    shape = (nx, ny, nz)
    domain = np.ones(shape, dtype=bool)

    # Consistent nodal loads for a uniform end traction: each element face
    # sends a quarter of its share to each of its four nodes, so corners get
    # one contribution, edge nodes two, the centre four.
    weights = np.zeros((ny + 1, nz + 1))
    for j in range(ny):
        for k in range(nz):
            weights[j : j + 2, k : k + 2] += 0.25
    weights /= weights.sum()
    loads = [
        ((nx, j, k), (float(weights[j, k]), 0.0, 0.0))
        for j in range(ny + 1)
        for k in range(nz + 1)
    ]

    problem = _build_problem(domain, loads, _face_supports(ny, nz), cg_tol=1e-12)
    result = _compliance_and_sensitivity(np.ones(shape), problem)
    assert not result.cg_capped

    u = result.displacement.reshape(-1, 3)
    tip = float(
        np.mean(
            [
                u[_node_index(shape, nx, j, k), 0]
                for j in range(ny + 1)
                for k in range(nz + 1)
            ]
        )
    )
    expected = 1.0 * nx / (1.0 * ny * nz)  # F·L / (E·A), all in voxel units
    assert tip == pytest.approx(expected, rel=0.05)
    assert tip < expected  # the clamped end can only stiffen it


# --------------------------------------------------------------------------
# 3. the optimiser descends
# --------------------------------------------------------------------------


def test_cantilever_compliance_descends_and_hits_the_volume_target() -> None:
    domain, loads, supports = _cantilever()
    res = simp_optimize(domain, loads, supports, volfrac=0.4)

    assert isinstance(res, SimpResult)
    assert len(res.compliance_history) == res.iterations + 1
    assert res.compliance_history[-1] < res.compliance_history[0]
    assert res.volume_fraction == pytest.approx(0.4, abs=0.02)
    assert res.density.shape == domain.shape
    assert np.all(res.density >= 0.0) and np.all(res.density <= 1.0)
    # no build direction was given, so the printed field IS the design field
    assert np.array_equal(res.density, res.design_density)
    # converged or not, the notes have to say which
    joined = " ".join(res.notes)
    assert ("converged after" in joined) or ("BUDGET EXHAUSTED" in joined)
    assert "never a hard DRC" in joined
    # the unconstrained run measures its overhangs, it does not eliminate them
    assert res.overhang_violation_count == overhang_violations(res.density)


# --------------------------------------------------------------------------
# 4. the domain mask is respected and the filter does not bleed
# --------------------------------------------------------------------------


def test_inactive_elements_stay_exactly_zero() -> None:
    """A carved keep-out must read exactly 0.0 — not 1e-9, not emin — in
    every field the optimiser hands back, at any point in the descent."""
    domain, loads, supports = _cantilever()
    domain = domain.copy()
    domain[6:10, 1:3, 1:3] = False  # a hole through the middle of the beam

    for budget in (1, 3, 8):
        res = simp_optimize(domain, loads, supports, volfrac=0.4, max_iter=budget)
        assert np.all(res.density[~domain] == 0.0)
        assert np.all(res.design_density[~domain] == 0.0)
        # the volume target is met over the ACTIVE elements only
        assert res.volume_fraction == pytest.approx(
            float(res.density[domain].mean()), abs=1e-12
        )


def test_sensitivity_filter_ignores_the_hole_it_straddles() -> None:
    """Mask-awareness has two halves and this pins both: an inactive element
    neither contributes to the weighted sum (so nothing bleeds across a
    keep-out, even with an absurd gradient parked inside it) nor dilutes the
    weight normaliser (so the neighbours' filtered gradient is an average
    over real material).

    On a 3x1x1 strip with the middle element carved out and rmin = 1.5, each
    surviving element's only in-radius active neighbour is itself, so the
    filter is the identity. Were the normaliser blind to the mask it would
    divide by 2.0 instead of 1.5 and shrink both values by a quarter.
    """
    domain = np.array([True, False, True]).reshape(3, 1, 1)
    x = np.array([0.5, 0.9, 0.5]).reshape(3, 1, 1)  # 0.9 is junk in the hole
    dc = np.array([-1.0, -1.0e6, -5.0]).reshape(3, 1, 1)  # so is the -1e6
    offsets = _filter_offsets(1.5)
    weight_sums = _filter_weight_sums(domain, offsets)

    filtered = _sensitivity_filter(dc, x, domain, offsets, weight_sums)
    assert filtered.ravel() == pytest.approx([-1.0, 0.0, -5.0])

    # and the same strip with nothing carved out gives a different answer,
    # so the assertion above is about the mask rather than a coincidence
    solid = np.ones((3, 1, 1), dtype=bool)
    filtered_solid = _sensitivity_filter(
        dc, x, solid, offsets, _filter_weight_sums(solid, offsets)
    )
    assert not np.allclose(filtered_solid.ravel(), filtered.ravel())


# --------------------------------------------------------------------------
# 5. the AM filter guarantee
# --------------------------------------------------------------------------


def test_exact_am_sweep_is_self_supporting_for_any_field() -> None:
    """The guarantee is structural, not statistical: after the hard sweep,
    anything above the threshold has something above the threshold beneath
    it, whatever went in."""
    rng = np.random.default_rng(11)
    # sparse on purpose: a uniform [0, 1) field is half solid, and at that
    # density every voxel happens to find support, so it would not test much
    field = np.where(rng.random((8, 6, 5)) < 0.15, 0.9, 0.05)
    assert overhang_violations(field) > 0  # the raw field does violate
    assert overhang_violations(_am_sweep_exact(field)) == 0
    # and the sweep only ever removes material
    assert np.all(_am_sweep_exact(field) <= field)


def test_build_direction_eliminates_overhangs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """build_dir='z+' must come back printable, and must have changed
    something. Nothing is asserted about whether the constrained compliance
    beats the unconstrained one — it usually does not, and a test that
    demanded either would be asserting a preference, not a property. Both are
    printed for the log.
    """
    domain, loads, supports = _cantilever()
    free = simp_optimize(domain, loads, supports, volfrac=0.4)
    printed = simp_optimize(domain, loads, supports, volfrac=0.4, build_dir="z+")

    assert printed.overhang_violation_count == 0
    assert overhang_violations(printed.density) == 0
    # the filter did something: the printed field is not the design field
    assert not np.allclose(printed.design_density, printed.density)
    assert np.all(printed.density[~domain] == 0.0)

    joined = " ".join(printed.notes)
    assert "no bridging allowance" in joined
    assert "one build direction" in joined

    with capsys.disabled():
        print(
            f"\ncompliance: unconstrained {free.compliance_history[-1]:.1f} "
            f"vs build_dir='z+' {printed.compliance_history[-1]:.1f}; "
            f"overhangs {free.overhang_violation_count} -> "
            f"{printed.overhang_violation_count}"
        )


def test_unconstrained_run_reports_overhangs_rather_than_hiding_them() -> None:
    """The honest half of the same coin: with build_dir=None the count is a
    measurement. The cantilever's free optimum hangs material off the tip, so
    it should be non-zero — and the notes must say the number is measured."""
    domain, loads, supports = _cantilever()
    res = simp_optimize(domain, loads, supports, volfrac=0.4)
    assert res.overhang_violation_count > 0
    assert "MEASURED count" in " ".join(res.notes)


# --------------------------------------------------------------------------
# 6. the AM sensitivity chain rule (the mutation-prone core)
# --------------------------------------------------------------------------


def test_am_filtered_gradient_matches_central_finite_differences() -> None:
    """Central differences through the whole differentiable forward map —
    design densities → Langelaar layer sweep → E(ρ) → K u = f → c = fᵀu —
    against the analytic gradient the optimiser actually uses.

    This is the test that catches a chain-rule slip in the sweep (a dropped
    smooth-min partial, a stencil scattered in the wrong direction, layers
    walked bottom-up instead of top-down). rtol is 1e-3 and is not loosened:
    the CG solves run to a 1e-13 relative residual and the step is 1e-5, so
    the observed agreement is ~1e-8 — three decades of headroom means a
    failure here is a real bug, not noise. An absolute floor tied to the
    largest gradient component keeps near-zero entries from being compared
    on their own (vanishing) scale; the picked indices are the largest
    components anyway.

    The sensitivity *filter* is deliberately outside this map: it smooths the
    gradient on purpose and is not a derivative of anything. Its behaviour is
    pinned by test_sensitivity_filter_ignores_the_hole_it_straddles.
    """
    n = 4
    domain = np.ones((n, n, n), dtype=bool)
    supports = _face_supports(n, n)
    loads = [((n, n // 2, n), (0.0, 0.0, -1.0))]
    problem = _build_problem(
        domain,
        loads,
        supports,
        build_dir="z+",
        cg_tol=1e-13,
        cg_max_iter=4000,
    )

    rng = np.random.default_rng(7)
    x = rng.uniform(0.3, 0.9, size=(n, n, n))
    base = _compliance_and_sensitivity(x, problem)
    assert not base.cg_capped

    scale = float(np.abs(base.dc).max())
    step = 1.0e-5
    picks = [
        tuple(int(v) for v in np.unravel_index(flat, x.shape))
        for flat in np.argsort(-np.abs(base.dc).ravel())[:6]
    ]
    for idx in picks:
        up, down = x.copy(), x.copy()
        up[idx] += step
        down[idx] -= step
        fd = (
            _compliance_and_sensitivity(up, problem).compliance
            - _compliance_and_sensitivity(down, problem).compliance
        ) / (2.0 * step)
        assert fd == pytest.approx(float(base.dc[idx]), rel=1e-3, abs=1e-3 * scale), (
            f"gradient mismatch at {idx}"
        )


def test_unfiltered_gradient_matches_central_finite_differences() -> None:
    """The same check with build_dir=None, so a failure localises: if this
    passes and the AM one fails, the bug is in the sweep's chain rule and not
    in the compliance derivative."""
    n = 4
    domain = np.ones((n, n, n), dtype=bool)
    problem = _build_problem(
        domain,
        [((n, n // 2, n), (0.0, 0.0, -1.0))],
        _face_supports(n, n),
        cg_tol=1e-13,
        cg_max_iter=4000,
    )
    rng = np.random.default_rng(3)
    x = rng.uniform(0.3, 0.9, size=(n, n, n))
    base = _compliance_and_sensitivity(x, problem)
    scale = float(np.abs(base.dc).max())
    step = 1.0e-5
    for flat in np.argsort(-np.abs(base.dc).ravel())[:4]:
        idx = tuple(int(v) for v in np.unravel_index(flat, x.shape))
        up, down = x.copy(), x.copy()
        up[idx] += step
        down[idx] -= step
        fd = (
            _compliance_and_sensitivity(up, problem).compliance
            - _compliance_and_sensitivity(down, problem).compliance
        ) / (2.0 * step)
        assert fd == pytest.approx(float(base.dc[idx]), rel=1e-3, abs=1e-3 * scale)


# --------------------------------------------------------------------------
# 7. refusals
# --------------------------------------------------------------------------


def test_refuses_a_problem_with_no_supports() -> None:
    domain, loads, _ = _cantilever()
    with pytest.raises(ValueError, match="no supports"):
        simp_optimize(domain, loads, [], volfrac=0.4)


def test_refuses_a_load_node_that_touches_no_active_element() -> None:
    nx, ny, nz = 4, 4, 4
    domain = np.ones((nx, ny, nz), dtype=bool)
    domain[nx - 1, ny - 1, nz - 1] = False  # the only element under node (4,4,4)
    with pytest.raises(ValueError, match=r"load node \(4, 4, 4\)"):
        simp_optimize(
            domain,
            [((nx, ny, nz), (0.0, 0.0, -1.0))],
            _face_supports(ny, nz),
            volfrac=0.4,
        )


def test_refuses_a_support_node_that_touches_no_active_element() -> None:
    nx, ny, nz = 4, 4, 4
    domain = np.ones((nx, ny, nz), dtype=bool)
    domain[0, 0, 0] = False
    with pytest.raises(ValueError, match=r"support node \(0, 0, 0\)"):
        simp_optimize(
            domain,
            [((nx, ny // 2, nz // 2), (0.0, 0.0, -1.0))],
            [((0, 0, 0), "xyz")],
            volfrac=0.4,
        )


def test_refuses_an_empty_domain() -> None:
    domain = np.zeros((4, 4, 4), dtype=bool)
    with pytest.raises(ValueError, match="domain is empty"):
        simp_optimize(
            domain,
            [((4, 2, 2), (0.0, 0.0, -1.0))],
            _face_supports(4, 4),
            volfrac=0.4,
        )


@pytest.mark.parametrize("volfrac", [0.0, 1.0, -0.2, 1.5])
def test_refuses_a_volume_fraction_outside_the_open_unit_interval(
    volfrac: float,
) -> None:
    domain, loads, supports = _cantilever()
    with pytest.raises(ValueError, match="volfrac"):
        simp_optimize(domain, loads, supports, volfrac=volfrac)


def test_refuses_an_unknown_build_direction() -> None:
    domain, loads, supports = _cantilever()
    with pytest.raises(ValueError, match="build_dir"):
        simp_optimize(domain, loads, supports, volfrac=0.4, build_dir="x+")


def test_refuses_a_load_case_that_is_entirely_zero() -> None:
    domain, _, supports = _cantilever()
    with pytest.raises(ValueError, match="no non-zero load"):
        simp_optimize(domain, [((16, 2, 2), (0.0, 0.0, 0.0))], supports, volfrac=0.4)


# --------------------------------------------------------------------------
# 8. the gyroid lattice fill
# --------------------------------------------------------------------------


def test_gyroid_solid_fraction_lands_in_the_linearised_band() -> None:
    """The wall/threshold linearisation predicts a solid fraction of
    (surface area per unit volume) × wall = 3.0915 · wall / cell — 0.386 for
    wall = 1, cell = 8. The band asserted is ±0.10 absolute: sampling the
    level set at element centres quantises the fraction badly at 8 voxels per
    cell (only a handful of distinct |g| values occur on an aligned grid),
    and the thin-wall linearisation itself drifts as wall/cell grows. The
    measured value is 0.375.

    No solve runs here, so the grid can be bigger than the FEA tests' — this
    is 1024 trig evaluations.
    """
    domain = np.ones((16, 8, 8), dtype=bool)
    res = lattice_fill(domain, cell=8.0, wall=1.0)

    assert isinstance(res, LatticeResult)
    predicted = 3.0915 * 1.0 / 8.0
    assert res.solid_fraction == pytest.approx(predicted, abs=0.10)
    assert 0.0 < res.solid_fraction < 1.0
    assert set(np.unique(res.density)) <= {0.0, 1.0}
    assert res.threshold > 0.0


def test_gyroid_fill_is_zero_outside_the_domain_and_deterministic() -> None:
    domain = np.ones((16, 8, 8), dtype=bool)
    domain[4:12, 2:6, 2:6] = False
    first = lattice_fill(domain, cell=8.0, wall=1.0)
    second = lattice_fill(domain, cell=8.0, wall=1.0)

    assert np.all(first.density[~domain] == 0.0)
    assert np.array_equal(first.density, second.density)
    assert first.solid_fraction == second.solid_fraction
    assert first.solid_fraction == pytest.approx(
        float(first.density[domain].mean()), abs=1e-12
    )


def test_gyroid_fill_measures_its_overhangs_rather_than_claiming_none() -> None:
    """A gyroid is *approximately* self-supporting as a smooth surface, which
    says nothing about this voxelisation at this phase. The function reports
    a number; the test refuses to assert that number is zero."""
    res = lattice_fill(np.ones((16, 8, 8), dtype=bool), cell=8.0, wall=1.0)
    assert isinstance(res.overhang_violation_count, int)
    assert res.overhang_violation_count >= 0
    assert res.overhang_violation_count == overhang_violations(res.density)
    assert "MEASURED" in " ".join(res.notes)


def test_lattice_fill_refuses_what_it_cannot_do() -> None:
    domain = np.ones((8, 4, 4), dtype=bool)
    with pytest.raises(ValueError, match="unknown lattice kind"):
        lattice_fill(domain, cell=4.0, wall=1.0, kind="schwarz-p")
    with pytest.raises(ValueError, match="must be smaller than cell"):
        lattice_fill(domain, cell=4.0, wall=4.0)
    with pytest.raises(ValueError, match="cell must be"):
        lattice_fill(domain, cell=0.0, wall=1.0)
    with pytest.raises(ValueError, match="domain is empty"):
        lattice_fill(np.zeros((4, 4, 4), dtype=bool), cell=4.0, wall=1.0)


# --------------------------------------------------------------------------
# 9. budget honesty
# --------------------------------------------------------------------------


def test_exhausted_budget_is_reported_as_exhausted() -> None:
    """Three iterations cannot converge this problem, so the result must say
    so in all three places: the count, the flag and the notes."""
    domain, loads, supports = _cantilever()
    res = simp_optimize(domain, loads, supports, volfrac=0.4, max_iter=3)

    assert res.iterations == 3
    assert res.converged is False
    assert len(res.compliance_history) == 4
    assert any("BUDGET EXHAUSTED" in note for note in res.notes)
    assert any("max_iter=3" in note for note in res.notes)


def test_convergence_is_reported_as_convergence() -> None:
    """The mirror branch. A tolerance above the 0.2 move limit cannot fail to
    be met, so this converges on the first step — which is the point: it
    exercises the reporting path without spending a second of CI on a real
    descent (test_cantilever_compliance_descends... already does that)."""
    domain, loads, supports = _cantilever()
    res = simp_optimize(domain, loads, supports, volfrac=0.4, tol=0.5, max_iter=30)

    assert res.converged is True
    assert res.iterations == 1
    assert any("converged after" in note for note in res.notes)


# --------------------------------------------------------------------------
# overhang_violations as a standalone predicate
# --------------------------------------------------------------------------


def test_overhang_predicate_counts_the_45_degree_rule() -> None:
    """One solid voxel floating two layers up with nothing under it is one
    violation; the same voxel offset by one in x from a solid voxel below is
    supported (that diagonal IS 45 degrees at cubic voxels); layer 0 is the
    build plate and never violates."""
    field = np.zeros((4, 4, 3))
    field[1, 1, 2] = 1.0
    assert overhang_violations(field) == 1

    field[0, 1, 1] = 1.0  # one across, one down — the 45 degree limit
    assert overhang_violations(field) == 1  # the new voxel is now unsupported
    field[0, 1, 0] = 1.0  # ...and now it rests on the plate
    assert overhang_violations(field) == 0

    plate_only = np.zeros((4, 4, 3))
    plate_only[:, :, 0] = 1.0
    assert overhang_violations(plate_only) == 0

    with pytest.raises(ValueError, match="3D"):
        overhang_violations(np.zeros((4, 4)))
