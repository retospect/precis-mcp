"""``hexfold.place.place_graph``: joint rigid placement across a part-graph
cycle (slice 3, 2026-09-28 -- docs/backlog/hexfold-integration.md "Seed
placement residuals ... fixed 2026-09-28").

Synthetic instances only, no ``build()`` involved: every instance shares
the same small asymmetric 4-point local cloud ``_P`` (non-coplanar, so a
Kabsch fit determines a unique 3D rotation), and edges use the exact
target convention :func:`hexfold.place.place_graph` documents -- ``v``'s
paired atoms fit to ``u``'s paired atoms offset by ``sigma`` along ``u``'s
own local normal, rotated into ``u``'s current frame.  A legitimate
two-sided registration (matching a real fuse) needs ``n_v`` close to
``-n_u`` (the two sides' rims face each other); the equilateral-triangle
fixture below is built that way on purpose (see its own docstring).
"""

from __future__ import annotations

import numpy as np

from hexfold.place import place_graph

_P = np.array(
    [
        [0.3, 0.1, 0.05],
        [0.1, -0.2, 0.15],
        [-0.2, 0.05, -0.1],
        [0.05, 0.15, 0.2],
    ]
)
_IDENTITY_PAIRS = [(i, i) for i in range(4)]


def _rotz(v: np.ndarray, deg: float) -> np.ndarray:
    th = np.radians(deg)
    c, s = np.cos(th), np.sin(th)
    r = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return r @ v


def _rot_angle_deg(r: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(r) - 1) / 2, -1.0, 1.0))))


def test_tree_converges_in_one_sweep_and_matches_the_chain() -> None:
    """A 3-instance path (A-B-C, no closing edge) has exactly one
    constraint per non-root instance: the BFS initial guess already
    satisfies every edge exactly, so the first sweep finds nothing moved.
    """
    rims = {"A": _P.copy(), "B": _P.copy(), "C": _P.copy()}
    side = 2.0
    n_ab = np.array([1.0, 0.0, 0.0])
    n_bc = np.array([-0.5, np.sqrt(3) / 2, 0.0])
    edges = [
        ("A", "B", _IDENTITY_PAIRS, (n_ab, -n_ab)),
        ("B", "C", _IDENTITY_PAIRS, (n_bc, -n_bc)),
    ]
    result = place_graph(rims, edges, "A", sigma=side)
    assert result.sweeps == 1
    assert all(r < 1e-9 for r in result.residuals)
    r_a, t_a = result.transforms["A"]
    assert np.array_equal(r_a, np.eye(3))
    assert np.allclose(t_a, 0.0)
    _r_b, t_b = result.transforms["B"]
    assert np.allclose(t_b, [2.0, 0.0, 0.0], atol=1e-9)
    _r_c, t_c = result.transforms["C"]
    assert np.allclose(t_c, [1.0, np.sqrt(3), 0.0], atol=1e-9)


def _triangle_edges(tilt_deg: float, side: float = 2.0) -> list:
    """An equilateral triangle A-B-C-A, side ``side``, all three edges
    using identity pairing and antiparallel per-side normals along the
    triangle's own edge directions -- consistent (residual 0) at
    ``tilt_deg=0``; the closing C->A edge's normal is rotated by
    ``tilt_deg`` about z, injecting a controlled, continuous rotational
    inconsistency independent of the (already consistent) A-B/B-C legs.
    """
    n_ab = np.array([1.0, 0.0, 0.0])
    n_bc = np.array([-0.5, np.sqrt(3) / 2, 0.0])
    n_ca = _rotz(np.array([-0.5, -np.sqrt(3) / 2, 0.0]), tilt_deg)
    return [
        ("A", "B", _IDENTITY_PAIRS, (n_ab, -n_ab)),
        ("B", "C", _IDENTITY_PAIRS, (n_bc, -n_bc)),
        ("C", "A", _IDENTITY_PAIRS, (n_ca, -n_ca)),
    ]


def test_consistent_cycle_closes_at_zero_residual_in_one_sweep() -> None:
    """The untilted triangle is a genuinely closed, consistent cycle (3
    edges, 3 nodes -- one redundant edge) yet has an exact rigid solution
    (equilateral triangle, side ``sigma``): the joint solve finds it with
    zero residual, still in one sweep (the BFS initial guess is already
    exact for every edge, same as the tree case)."""
    rims = {"A": _P.copy(), "B": _P.copy(), "C": _P.copy()}
    result = place_graph(rims, _triangle_edges(0.0), "A", sigma=2.0)
    assert result.sweeps == 1
    assert all(r < 1e-9 for r in result.residuals)


def test_inconsistent_cycle_converges_and_spreads_the_residual() -> None:
    """A 15-degree twist injected into just the closing edge makes the
    triangle over-constrained (no rigid solution satisfies all three
    edges exactly): the joint solve converges to a *shared* nonzero
    residual across all three edges -- never zero on two edges and the
    whole defect dumped on the third, which is what the old single
    -edge-per-node BFS would do by simply dropping the closing edge."""
    rims = {"A": _P.copy(), "B": _P.copy(), "C": _P.copy()}
    result = place_graph(rims, _triangle_edges(15.0), "A", sigma=2.0, tol=1e-6)
    assert 1 < result.sweeps < 50
    assert all(r > 0.05 for r in result.residuals), result.residuals
    # spread, not concentrated: no edge carries much more than the others
    assert max(result.residuals) / min(result.residuals) < 1.5, result.residuals
    # instances beyond the immediate tilted edge also moved -- the fix
    # isn't localised to a single instance
    _r_b, t_b = result.transforms["B"]
    assert not np.allclose(t_b, [2.0, 0.0, 0.0], atol=1e-6)


def test_deterministic() -> None:
    rims = {"A": _P.copy(), "B": _P.copy(), "C": _P.copy()}
    edges = _triangle_edges(15.0)
    r1 = place_graph(rims, edges, "A", sigma=2.0)
    r2 = place_graph(rims, edges, "A", sigma=2.0)
    assert r1.sweeps == r2.sweeps
    assert r1.residuals == r2.residuals
    for inst in ("A", "B", "C"):
        r_a, t_a = r1.transforms[inst]
        r_b, t_b = r2.transforms[inst]
        assert np.array_equal(r_a, r_b)
        assert np.array_equal(t_a, t_b)


def test_root_stays_at_identity() -> None:
    rims = {"A": _P.copy(), "B": _P.copy(), "C": _P.copy()}
    result = place_graph(rims, _triangle_edges(15.0), "A", sigma=2.0)
    r_a, t_a = result.transforms["A"]
    assert np.array_equal(r_a, np.eye(3))
    assert np.array_equal(t_a, np.zeros(3))


def test_two_parallel_edges_between_the_same_pair_average_their_disagreement() -> None:
    """The ``tube_ring_closure.hx`` shape in miniature: a 4-fold
    symmetric ring, two edges between the same pair of instances
    registered at different phases (k=0 vs k=1, exactly the ``a.out
    --fuse k=1--> b.in`` / ``a.in --fuse k=0--> b.out`` mismatch that
    file's own comment names) -- neither phase wins outright; both sides
    settle on a shared, roughly equal residual (matching phases give
    a clean one-sweep, zero-residual result, the control case below)."""
    ang = np.array([0.0, 90.0, 180.0, 270.0]) * np.pi / 180
    ring = np.stack([np.cos(ang), np.sin(ang), np.zeros(4)], axis=1)
    rims = {"A": ring.copy(), "B": ring.copy()}
    n = np.array([0.0, 0.0, 1.0])

    def pairs_k(k: int) -> list[tuple[int, int]]:
        return [(i, (k - i) % 4) for i in range(4)]

    mismatched = [
        ("A", "B", pairs_k(0), (n, -n)),
        ("A", "B", pairs_k(1), (n, -n)),
    ]
    result = place_graph(rims, mismatched, "A", sigma=1.0, tol=1e-6)
    assert result.sweeps > 1
    assert all(r > 0.01 for r in result.residuals)
    assert max(result.residuals) / min(result.residuals) < 1.5

    matched = [
        ("A", "B", pairs_k(0), (n, -n)),
        ("A", "B", pairs_k(0), (n, -n)),
    ]
    control = place_graph(rims, matched, "A", sigma=1.0)
    assert control.sweeps == 1
    assert all(r < 1e-9 for r in control.residuals)
