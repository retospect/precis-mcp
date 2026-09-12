"""precis.structsolve.complementarity — the pure active-set unilateral
solver (docs/backlog/complementarity-solver.md slice 1, CORE only).
Unit-agnostic, store-free: arrays in, arrays out. The four acceptance
fixtures from the spec: (a) a two-cable/one-strut tripod where one
cable goes slack under a lateral load; (b) a compression-only contact
that separates under uplift; (c) a must-contact stop reported
`unseated` when the applied pull exceeds what its preload can absorb;
(d) the classic 3-strut/9-cable tensegrity prism (same geometry as
``tests/test_se_stability.py``'s fixture) staying fully taut/bearing
under a small service load. Plus one fixture isolating the geometric-
stiffness term against a hand-derived force number (fixtures (a)-(c)
are all zero-prestress, so that term is otherwise only exercised
indirectly via (d)'s singularity), a units-agnosticism check (m/N vs
Å/nN reach the same status rows), and the refusal paths."""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.structsolve.complementarity import (
    IDIOMS,
    ComplementarityError,
    solve_complementarity,
)

_S32 = math.sqrt(3.0) / 2.0


# ── (a) two-cable + strut tripod: one cable goes slack ───────────────────


def _tripod() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    # Anchors A (strut), B, C (cables) on the unit circle at z=0; apex D
    # above at (0, 0, 1) — the same "anchored triangle... one strut"
    # shape as formfind's own mixed-sign test. D rides the z=0-offset
    # rail (x, y free, z prescribed) so the problem is a clean 2-DOF
    # planar solve at the apex.
    coords = np.array(
        [
            [1.0, 0.0, 0.0],
            [-0.5, _S32, 0.0],
            [-0.5, -_S32, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    members = np.array([[0, 3], [1, 3], [2, 3]])  # A-D strut, B-D/C-D cables
    fixed = np.zeros((4, 3), dtype=bool)
    fixed[:3] = True
    fixed[3] = [False, False, True]
    return coords, members, fixed, np.array([10.0, 10.0, 10.0])


def test_tripod_lateral_load_slacks_one_cable_forces_match_hand_calc() -> None:
    coords, members, fixed, rate = _tripod()
    lengths0 = np.array([np.linalg.norm(coords[3] - coords[k]) for k in range(3)])
    free_length = lengths0.copy()  # no prestress — pure load response
    idiom = np.array(["compression_only", "tension_only", "tension_only"], dtype=object)
    loads = np.zeros((4, 3))
    loads[3] = [1.0, -1.0, 0.0]

    res = solve_complementarity(coords, members, rate, free_length, idiom, fixed, loads)

    assert list(res.status) == ["bearing", "taut", "slack"]
    assert res.forces[0] < 0.0  # strut in compression
    assert res.forces[1] > 0.0  # cable B taut
    assert res.forces[2] == 0.0  # cable C carries nothing once slack
    assert res.residual < 1e-9

    # Hand calc: with member 2 dropped and no prestress, this is a
    # plain 2-member elastic solve at the apex's free (x, y) dof —
    # reconstruct it independently and check the reported forces match.
    u = [(coords[3] - coords[k]) / lengths0[k] for k in (0, 1)]
    k_xy = sum(rate[k] * np.outer(u[k][:2], u[k][:2]) for k in (0, 1))
    d_xy = np.linalg.solve(k_xy, loads[3, :2])
    expected = [rate[k] * float(u[k][:2] @ d_xy) for k in (0, 1)]
    assert res.forces[0] == pytest.approx(expected[0])
    assert res.forces[1] == pytest.approx(expected[1])


# ── (b) compression-only contact separates under uplift ─────────────────


def _contact_and_brace() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # A vertical compression-only "post" from the ground to the moving
    # part, plus a bidirectional brace so the free z-dof still has a
    # load path once the post separates.
    coords = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    members = np.array([[0, 1], [0, 1]])
    fixed = np.array([[True, True, True], [True, True, False]])
    return coords, members, fixed


def test_compression_only_contact_separates_under_uplift() -> None:
    coords, members, fixed = _contact_and_brace()
    rate = np.array([10.0, 5.0])
    length0 = np.array([1.0, 1.0])
    idiom = np.array(["compression_only", "bidirectional"], dtype=object)

    loads = np.zeros((2, 3))
    loads[1, 2] = 5.0  # uplift
    res = solve_complementarity(coords, members, rate, length0, idiom, fixed, loads)
    assert list(res.status) == ["separated", "taut"]
    assert res.forces[0] == 0.0
    assert res.forces[1] == pytest.approx(5.0)
    assert res.residual < 1e-9

    # a downward (gravity-like) load instead keeps the contact bearing
    loads[1, 2] = -5.0
    res = solve_complementarity(coords, members, rate, length0, idiom, fixed, loads)
    assert res.status[0] == "bearing"
    assert res.forces[0] < 0.0


# ── (c) must-contact stop unseated when the preload is insufficient ─────


def _preloaded_stop() -> tuple[
    np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray
]:
    # A must-contact stop from a ground anchor along +x to the moving
    # part, plus an off-axis bidirectional brace to a second, separate
    # ground anchor so the free x-dof keeps a load path once the stop
    # unseats. The stop is pre-compressed (free_length > installed
    # length): t0 = rate*(L - L0) < 0.
    coords = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    members = np.array([[0, 1], [2, 1]])
    fixed = np.array([[True, True, True], [False, True, True], [True, True, True]])
    rate = np.array([20.0, 5.0])
    length0 = np.array([1.0, float(np.linalg.norm(coords[1] - coords[2]))])
    return coords, members, fixed, rate, length0


def test_must_contact_stop_unseats_when_pull_exceeds_preload() -> None:
    coords, members, fixed, rate, length0 = _preloaded_stop()
    free_length = np.array([1.02, length0[1]])  # stop pre-compressed
    idiom = np.array(["must_contact", "bidirectional"], dtype=object)

    loads = np.zeros((3, 3))
    loads[1, 0] = 0.5  # a pull well beyond what the preload absorbs
    res = solve_complementarity(coords, members, rate, free_length, idiom, fixed, loads)
    assert res.status[0] == "unseated"
    assert res.forces[0] == 0.0

    loads[1, 0] = 0.02  # a small pull the preload still absorbs
    res = solve_complementarity(coords, members, rate, free_length, idiom, fixed, loads)
    assert res.status[0] == "seated"
    assert res.forces[0] < 0.0


# ── (d) the 3-strut/9-cable tensegrity prism stays fully taut/bearing ───

_PRISM_NODES = {
    "b0": [1.0, 0.0, 0.0],
    "b1": [-0.5, _S32, 0.0],
    "b2": [-0.5, -_S32, 0.0],
    "t0": [_S32, 0.5, 1.0],
    "t1": [-_S32, 0.5, 1.0],
    "t2": [0.0, -1.0, 1.0],
}
_PRISM_CABLES = [
    ("b0", "b1"),
    ("b1", "b2"),
    ("b2", "b0"),
    ("t0", "t1"),
    ("t1", "t2"),
    ("t2", "t0"),
    ("b0", "t0"),
    ("b1", "t1"),
    ("b2", "t2"),
]
_PRISM_STRUTS = [("b0", "t1"), ("b1", "t2"), ("b2", "t0")]
#: The prism's unique self-stress state (a ray — normalized to the
#: strut magnitude), read off the equilibrium matrix's null space at
#: this exact geometry/support set; sign-flipped so cables are positive
#: (tension) and struts negative (compression), matching
#: ``test_se_stability.test_tensegrity_prism_is_prestress_stabilized``.
_PRISM_RATIO = {
    ("b0", "b1"): 0.4597,
    ("b1", "b2"): 0.4597,
    ("b2", "b0"): 0.4597,
    ("t0", "t1"): 0.4597,
    ("t1", "t2"): 0.4597,
    ("t2", "t0"): 0.4597,
    ("b0", "t0"): 0.5176,
    ("b1", "t1"): 0.5176,
    ("b2", "t2"): 0.5176,
    ("b0", "t1"): -1.0,
    ("b1", "t2"): -1.0,
    ("b2", "t0"): -1.0,
}
#: 3-2-1 supports on the bottom triangle — statically determinate,
#: same as test_se_stability's `_PRISM_FIXED`.
_PRISM_FIXED_AXES = {"b0": ("x", "y", "z"), "b1": ("y", "z"), "b2": ("z",)}


def _prism_arrays(
    scale: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    members_named = _PRISM_CABLES + _PRISM_STRUTS
    names = list(_PRISM_NODES)
    index = {n: i for i, n in enumerate(names)}
    coords = np.array([_PRISM_NODES[n] for n in names]) * scale
    members = np.array([[index[a], index[b]] for a, b in members_named])
    lengths0 = np.array(
        [np.linalg.norm(coords[index[b]] - coords[index[a]]) for a, b in members_named]
    )
    idiom = np.array(
        ["tension_only"] * len(_PRISM_CABLES)
        + ["compression_only"] * len(_PRISM_STRUTS),
        dtype=object,
    )
    fixed = np.zeros((len(names), 3), dtype=bool)
    axis = {"x": 0, "y": 1, "z": 2}
    for name, axes in _PRISM_FIXED_AXES.items():
        for a in axes:
            fixed[index[name], axis[a]] = True
    return (
        coords,
        members,
        lengths0,
        idiom,
        fixed,
        np.array([_PRISM_RATIO[m] for m in members_named]),
    )


def _prism_problem(
    scale: float, rate_value: float, prestress_scale: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    coords, members, lengths0, idiom, fixed, ratio = _prism_arrays(scale)
    rate = np.full(members.shape[0], rate_value)
    t0_target = ratio * prestress_scale
    free_length = lengths0 - t0_target / rate
    return coords, members, rate, free_length, idiom, fixed


def test_prism_needs_its_prestress_first_order_mechanism_otherwise() -> None:
    # Without the self-stress, this exact twist is the textbook one
    # internal mechanism / one self-stress state — the elastic-only
    # stiffness is singular there (test_se_stability's own classify()
    # confirms m_internal=1, s=1 for this geometry+supports).
    coords, members, lengths0, idiom, fixed, _ratio = _prism_arrays(1.0)
    rate = np.full(members.shape[0], 50.0)
    with pytest.raises(ComplementarityError, match="singular"):
        solve_complementarity(coords, members, rate, lengths0, idiom, fixed)


def test_prism_all_ties_stay_taut_under_a_small_service_load() -> None:
    coords, members, rate, free_length, idiom, fixed = _prism_problem(
        scale=1.0, rate_value=50.0, prestress_scale=10.0
    )
    loads = np.zeros((coords.shape[0], 3))
    loads[3] = [0.05, -0.05, 0.02]  # small perturbation at node t0

    res = solve_complementarity(coords, members, rate, free_length, idiom, fixed, loads)

    assert res.iterations == 1  # nothing flips — every sign was legal from the start
    n_cables = len(_PRISM_CABLES)
    assert np.all(res.forces[:n_cables] > 0.0)
    assert np.all(res.forces[n_cables:] < 0.0)
    assert list(res.status) == ["taut"] * n_cables + ["bearing"] * len(_PRISM_STRUTS)
    assert res.residual < 1e-6


# ── geometric-stiffness term, isolated: a hand-computable force number ──
#
# Fixtures (a)-(c) all have zero prestress, so the geometric-stiffness
# term is only exercised indirectly (through the prism's singularity in
# (d), a sign/status check, not a number). This fixture isolates it: two
# collinear prestressed cables A-D, B-D along the x-axis contribute
# *zero* elastic stiffness transverse to their own axis (u_y == 0 for
# both, so rate*u_y**2 == 0) — the only y-direction stiffness they offer
# is the geometric term 2q (q = t0/L, isotropic). A third, unprestressed
# rod E-D running along y then reports a force driven entirely by how
# stiff that combined y-direction is, so a sign error in the geometric
# term changes a specific number, not just which member is active:
#
#   K_yy = rate_rod + 2q            (elastic rod + geometric cables)
#   d_y  = P_y / K_yy
#   force_rod = rate_rod * d_y = rate_rod * P_y / (rate_rod + 2q)
#
# A flipped-sign geometric term (rate_rod - 2q in the denominator) was
# checked by hand against this fixture during review: it changes
# force_rod to a different, easily-distinguished number (or, for these
# exact parameters, makes the reduced stiffness singular).
def test_geometric_stiffness_alone_sets_the_transverse_force() -> None:
    coords = np.array(
        [
            [-1.0, 0.0, 0.0],  # A (cable anchor)
            [1.0, 0.0, 0.0],  # B (cable anchor)
            [0.0, -1.0, 0.0],  # E (rod anchor)
            [0.0, 0.0, 0.0],  # D (free in y only)
        ]
    )
    members = np.array([[0, 3], [1, 3], [2, 3]])
    rate_cable, rate_rod = 10.0, 10.0
    rate = np.array([rate_cable, rate_cable, rate_rod])
    t0_cable = 5.0  # tension-positive prestress on both cables
    free_length = np.array(
        [1.0 - t0_cable / rate_cable, 1.0 - t0_cable / rate_cable, 1.0]
    )
    idiom = np.array(["tension_only", "tension_only", "bidirectional"], dtype=object)
    fixed = np.zeros((4, 3), dtype=bool)
    fixed[:3] = True
    fixed[3] = [True, False, True]

    p_y = 2.0
    loads = np.zeros((4, 3))
    loads[3, 1] = p_y

    res = solve_complementarity(coords, members, rate, free_length, idiom, fixed, loads)

    q = t0_cable / 1.0  # length of each cable is 1.0
    k_yy = rate_rod + 2 * q
    expected_d_y = p_y / k_yy
    expected_force_rod = rate_rod * expected_d_y

    assert res.displacements[3, 1] == pytest.approx(expected_d_y)
    assert res.forces[2] == pytest.approx(expected_force_rod)
    assert expected_force_rod == pytest.approx(1.0)  # the concrete number
    # the cables' own elongation is zero to first order (pure transverse
    # displacement along a collinear pair) — their force stays at t0.
    assert res.forces[0] == pytest.approx(t0_cable)
    assert res.forces[1] == pytest.approx(t0_cable)
    assert list(res.status) == ["taut", "taut", "taut"]
    assert res.residual < 1e-9


# ── units-agnosticism: same problem in m/N vs Å/nN, identical statuses ──


def test_units_agnosticism_m_newtons_vs_angstrom_nanonewtons() -> None:
    coords, members, rate, free_length, idiom, fixed = _prism_problem(
        scale=1.0, rate_value=50.0, prestress_scale=10.0
    )
    loads = np.zeros((coords.shape[0], 3))
    loads[3] = [0.05, -0.05, 0.02]
    res_si = solve_complementarity(
        coords, members, rate, free_length, idiom, fixed, loads
    )

    # 1 m -> 1e10 Å; 1 N -> 1e9 nN; rate [N/m] -> [nN/Å] scales by 1e-1.
    len_scale, force_scale = 1e10, 1e9
    rate_scale = force_scale / len_scale
    res_alt = solve_complementarity(
        coords * len_scale,
        members,
        rate * rate_scale,
        free_length * len_scale,
        idiom,
        fixed,
        loads * force_scale,
    )

    assert list(res_si.status) == list(res_alt.status)
    assert np.array_equal(res_si.active, res_alt.active)
    assert res_si.iterations == res_alt.iterations
    np.testing.assert_allclose(res_alt.forces, res_si.forces * force_scale, rtol=1e-6)
    np.testing.assert_allclose(
        res_alt.displacements, res_si.displacements * len_scale, rtol=1e-6
    )


# ── refusal paths ─────────────────────────────────────────────────────────


def test_cycling_active_set_is_refused_not_silently_accepted() -> None:
    # A genuinely cycling instance (active-set flip-flop is a known,
    # if uncommon, failure mode of member-removal/reinstatement schemes
    # for unilateral trusses) — found by randomized search over small
    # tension/compression-only problems, not hand-derived; kept exactly
    # as found rather than "simplified" into something that might
    # accidentally stop cycling.
    coords = np.array(
        [
            [0.18733918845096253, -0.5201490395530004, 0.0],
            [-1.9333114931110384, -1.340095553699519, 0.0],
            [0.43962966489590105, -1.671143362908699, 0.0],
        ]
    )
    members = np.array([[0, 2], [2, 0], [0, 1]])
    rate = np.array([14.35623401966957, 4.927724231210869, 19.363360824971572])
    free_length = np.array([1.4075287320095677, 1.1447503978276796, 2.6689447262484753])
    idiom = np.array(
        ["compression_only", "tension_only", "compression_only"], dtype=object
    )
    fixed = np.array([[False, False, True], [True, True, True], [True, True, True]])
    loads = np.array(
        [
            [4.126296644637995, -3.4666022590791954, 0.0],
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
        ]
    )
    with pytest.raises(ComplementarityError, match="cycl"):
        solve_complementarity(coords, members, rate, free_length, idiom, fixed, loads)


def test_no_legal_path_after_separation_is_refused() -> None:
    coords, members, fixed = _contact_and_brace()
    rate = np.array([10.0, 10.0])
    length0 = np.array([1.0, 1.0])
    # both members compression-only: neither can hold an uplift once
    # both must separate, and nothing else spans the free z-dof.
    idiom = np.array(["compression_only", "compression_only"], dtype=object)
    loads = np.zeros((2, 3))
    loads[1, 2] = 5.0
    with pytest.raises(ComplementarityError, match="no legal path|singular"):
        solve_complementarity(coords, members, rate, length0, idiom, fixed, loads)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda c, m, r, fl, i, f: (c[:, :2], m, r, fl, i, f), r"coords must be"),
        (
            lambda c, m, r, fl, i, f: (c, m[:, :1], r, fl, i, f),
            r"members must be",
        ),
        (lambda c, m, r, fl, i, f: (c, m, r[:1], fl, i, f), r"rate must be"),
        (
            lambda c, m, r, fl, i, f: (c, m, r, fl[:1], i, f),
            r"free_length must be",
        ),
        (
            lambda c, m, r, fl, i, f: (c, m, r, fl, i[:1], f),
            r"idiom must be",
        ),
        (
            lambda c, m, r, fl, i, f: (c, m, r, fl, i, f[:1]),
            r"fixed must be",
        ),
    ],
)
def test_shape_mismatches_reject(mutate: object, match: str) -> None:
    coords, members, fixed = _contact_and_brace()
    rate = np.array([10.0, 5.0])
    length0 = np.array([1.0, 1.0])
    idiom = np.array(["compression_only", "bidirectional"], dtype=object)
    args = mutate(coords, members, rate, length0, idiom, fixed)  # type: ignore[operator]
    with pytest.raises(ComplementarityError, match=match):
        solve_complementarity(*args)


def test_empty_problem_rejects() -> None:
    # Each degenerate axis alone must trip the guard (kills `or -> and`
    # on the b == 0 / j < 2 check): plenty of nodes but zero members,
    # and a member list against a single node.
    coords, _, fixed = _contact_and_brace()
    no_members = np.empty((0, 2), dtype=int)
    empty = np.empty((0,))
    no_idiom = np.empty((0,), dtype=object)
    with pytest.raises(ComplementarityError, match="nothing to solve"):
        solve_complementarity(coords, no_members, empty, empty, no_idiom, fixed)
    one_node = np.array([[0.0, 0.0, 0.0]])
    one_fixed = np.array([[True, True, True]])
    members = np.array([[0, 0]])
    rate = np.array([10.0])
    length0 = np.array([1.0])
    idiom = np.array(["bidirectional"], dtype=object)
    with pytest.raises(ComplementarityError, match="nothing to solve"):
        solve_complementarity(one_node, members, rate, length0, idiom, one_fixed)


def test_non_positive_rate_rejects() -> None:
    coords, members, fixed = _contact_and_brace()
    idiom = np.array(["compression_only", "bidirectional"], dtype=object)
    with pytest.raises(ComplementarityError, match="finite positive"):
        solve_complementarity(
            coords, members, np.array([0.0, 5.0]), np.array([1.0, 1.0]), idiom, fixed
        )


def test_non_positive_free_length_rejects() -> None:
    coords, members, fixed = _contact_and_brace()
    idiom = np.array(["compression_only", "bidirectional"], dtype=object)
    with pytest.raises(ComplementarityError, match="free_length"):
        solve_complementarity(
            coords, members, np.array([10.0, 5.0]), np.array([-1.0, 1.0]), idiom, fixed
        )


def test_unknown_idiom_rejects() -> None:
    coords, members, fixed = _contact_and_brace()
    idiom = np.array(["compression_only", "elastic-band"], dtype=object)
    with pytest.raises(ComplementarityError, match="unknown idiom"):
        solve_complementarity(
            coords, members, np.array([10.0, 5.0]), np.array([1.0, 1.0]), idiom, fixed
        )


def test_mixed_type_idiom_array_rejects_cleanly_not_typeerror() -> None:
    # a stray non-string entry (e.g. an int) makes the "unknown idioms"
    # set contain incomparable types — must not leak a bare TypeError
    # from sorting it.
    coords, members, fixed = _contact_and_brace()
    idiom = np.array([1, "compression_only"], dtype=object)
    with pytest.raises(ComplementarityError, match="unknown idiom"):
        solve_complementarity(
            coords, members, np.array([10.0, 5.0]), np.array([1.0, 1.0]), idiom, fixed
        )


def test_self_loop_rejects() -> None:
    coords, _members, fixed = _contact_and_brace()
    with pytest.raises(ComplementarityError, match="self-loop"):
        solve_complementarity(
            coords,
            np.array([[1, 1]]),
            np.array([10.0]),
            np.array([1.0]),
            np.array(["bidirectional"], dtype=object),
            fixed,
        )


def test_zero_length_member_rejects() -> None:
    coords = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    fixed = np.array([[True, True, True], [True, True, False]])
    with pytest.raises(ComplementarityError, match="zero length"):
        solve_complementarity(
            coords,
            np.array([[0, 1]]),
            np.array([10.0]),
            np.array([1.0]),
            np.array(["bidirectional"], dtype=object),
            fixed,
        )


def test_all_idioms_are_documented_and_valid_inputs() -> None:
    # every declared idiom string round-trips as an accepted input on a
    # trivial single-member problem with no load (t0 = 0 throughout).
    coords = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    members = np.array([[0, 1]])
    fixed = np.array([[True, True, True], [True, True, False]])
    for idm in IDIOMS:
        res = solve_complementarity(
            coords,
            members,
            np.array([10.0]),
            np.array([1.0]),
            np.array([idm], dtype=object),
            fixed,
        )
        assert res.forces[0] == 0.0
