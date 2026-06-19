"""Unit tests for the pure hierarchical-SOM engine (no DB).

Covers the logic-dense bits behind the ``clusterize`` worker:
normalisation, batch-SOM separation of well-separated blobs, adaptive
hierarchy build, top-down descent consistency, and sibling-scoped
c-TF-IDF word clouds.
"""

from __future__ import annotations

import numpy as np

from precis.utils.cluster_map import (
    Cell,
    ancestors,
    assign_bmu,
    build_hierarchy,
    ctfidf_words,
    descend_to_leaf,
    l2_normalize,
    linear_sum_assignment,
    rollup_histograms,
    stability_report,
    train_som,
)


def _blobs(
    seed: int = 0, per: int = 60, dim: int = 12
) -> tuple[np.ndarray, np.ndarray]:
    """Three well-separated unit-ish blobs along distinct axes."""
    rng = np.random.default_rng(seed)
    centers = np.zeros((3, dim), dtype=np.float32)
    centers[0, 0] = 10.0
    centers[1, 1] = 10.0
    centers[2, 2] = 10.0
    pts, labels = [], []
    for k in range(3):
        pts.append(centers[k] + rng.normal(scale=0.2, size=(per, dim)))
        labels.append(np.full(per, k))
    X = l2_normalize(np.vstack(pts).astype(np.float32))
    return X, np.concatenate(labels)


def test_l2_normalize_unit_rows_and_zero_safe() -> None:
    m = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)
    out = l2_normalize(m)
    assert np.allclose(np.linalg.norm(out[0]), 1.0)
    assert np.allclose(out[1], 0.0)  # zero row stays zero, no nan


def _assert_pure(assignment, truth, *, min_purity: float = 0.9) -> None:
    """Every occupied cell is dominated by one blob, and no two blobs
    share a cell. (A SOM may spread one blob over several cells — that
    sub-structure is fine — but cells must not *mix* blobs.)"""
    cell_to_blobs: dict[object, list[int]] = {}
    for cell, blob in zip(assignment, truth, strict=True):
        cell_to_blobs.setdefault(cell, []).append(int(blob))
    owners: set[int] = set()
    for members in cell_to_blobs.values():
        counts = np.bincount(members)
        dominant = int(counts.argmax())
        assert counts[dominant] / len(members) >= min_purity
        owners.add(dominant)
    assert len(owners) == 3  # all three blobs surfaced (in ≥3 pure cells)


def test_som_separates_blobs() -> None:
    X, truth = _blobs()
    weights = train_som(X, n_rows=3, n_cols=3, iters=60, seed=1)
    labels = assign_bmu(X, weights)
    _assert_pure(labels, truth)


def test_build_hierarchy_full_grid_and_pure_leaves() -> None:
    X, truth = _blobs()
    cells, leaves = build_hierarchy(X, grid=(3, 3), max_depth=1, min_chunks=10, seed=2)
    # Depth-1 ⇒ a single full 3×3 grid of leaves.
    assert len(cells) == 9
    assert all(c.is_leaf for c in cells)
    _assert_pure(leaves, truth)


def test_adaptive_depth_stops_on_sparse_branch() -> None:
    X, _ = _blobs(per=5)  # 15 points, below any reasonable min_chunks
    cells, _ = build_hierarchy(X, grid=(3, 3), max_depth=3, min_chunks=200, seed=3)
    # No branch had enough members to split → flat single grid.
    assert all(c.depth == 0 and c.is_leaf for c in cells)


def test_descend_matches_build_at_depth_one() -> None:
    X, _ = _blobs()
    cells, leaves = build_hierarchy(X, grid=(3, 3), max_depth=1, min_chunks=10, seed=4)
    redescended = descend_to_leaf(X, cells, grid=(3, 3))
    assert list(redescended) == list(leaves)


def test_ancestors() -> None:
    assert ancestors("4") == ["4"]
    assert ancestors("4.7.1") == ["4", "4.7", "4.7.1"]


def test_rollup_histograms_sums_into_ancestors() -> None:
    rolled = rollup_histograms({"4.0": {"a": 3}, "4.1": {"a": 2, "b": 5}})
    assert rolled["4"] == {"a": 5, "b": 5}
    assert rolled["4.0"] == {"a": 3}


def test_linear_sum_assignment_optimal() -> None:
    cost = np.array([[4.0, 1.0, 3.0], [2.0, 0.0, 5.0], [3.0, 2.0, 2.0]])
    rows, cols = linear_sum_assignment(cost)
    assert set(cols) == {0, 1, 2}  # a permutation
    assert sum(cost[r][c] for r, c in zip(rows, cols, strict=True)) == 5.0


def test_warm_start_aligns_map_to_prior() -> None:
    # Establish a reference map, then rebuild on a *different sample* of
    # the same structure. Warm-starting from the prior keeps tile i where
    # prior tile i was; a cold build with a fresh seed is free to permute
    # the indices. So the warm map aligns to the prior far better than the
    # cold one — that alignment is what holds tile addresses stable.
    X1, _ = _blobs(seed=0)
    prior_cells, _ = build_hierarchy(
        X1, grid=(3, 3), max_depth=1, min_chunks=10, seed=2
    )
    prior = {c.path: c.centroid for c in prior_cells}

    X2, _ = _blobs(seed=99)  # same axes, different noise draw
    warm, _ = build_hierarchy(
        X2, grid=(3, 3), max_depth=1, min_chunks=10, seed=7, prior=prior
    )
    cold, _ = build_hierarchy(X2, grid=(3, 3), max_depth=1, min_chunks=10, seed=7)

    rep_warm = stability_report(warm, prior)
    rep_cold = stability_report(cold, prior)
    assert rep_warm["identity"] == 1.0
    assert rep_warm["self_cos"] > rep_cold["self_cos"]


def test_stability_report_identity_after_warm_start() -> None:
    X1, _ = _blobs(seed=0)
    prior_cells, _ = build_hierarchy(
        X1, grid=(3, 3), max_depth=1, min_chunks=10, seed=2
    )
    prior = {c.path: c.centroid for c in prior_cells}

    X2, _ = _blobs(seed=99)
    cells2, _ = build_hierarchy(
        X2, grid=(3, 3), max_depth=1, min_chunks=10, seed=7, prior=prior
    )
    rep = stability_report(cells2, prior)
    assert rep["n"] == 9.0
    assert rep["identity"] == 1.0  # every tile's optimal match is itself
    assert rep["self_cos"] > 0.95


def test_stability_report_no_prior() -> None:
    X, _ = _blobs()
    cells, _ = build_hierarchy(X, grid=(3, 3), max_depth=1, min_chunks=10)
    assert stability_report(cells, None) == {"n": 0.0, "self_cos": 0.0, "identity": 0.0}
    assert stability_report(cells, {}) == {"n": 0.0, "self_cos": 0.0, "identity": 0.0}


def test_ctfidf_suppresses_shared_and_stopwords() -> None:
    cells = [
        Cell(
            path="0", parent=None, depth=0, grid_row=0, grid_col=0, centroid=np.zeros(2)
        ),
        Cell(
            path="1", parent=None, depth=0, grid_row=0, grid_col=1, centroid=np.zeros(2)
        ),
    ]
    hist = {
        "0": {"transformer": 8, "attention": 6, "learning": 5},
        "1": {"diffusion": 9, "sampling": 6, "learning": 5, "the": 30},
    }
    words = ctfidf_words(hist, cells, top_k=5)
    top0 = [w for w, _ in words["0"]]
    top1 = [w for w, _ in words["1"]]
    # The cell's distinctive term outranks the shared "learning".
    assert top0[0] in {"transformer", "attention"}
    assert top0.index("transformer") < top0.index("learning")
    # Stopword dropped entirely.
    assert "the" not in top1
    assert top1[0] in {"diffusion", "sampling"}
