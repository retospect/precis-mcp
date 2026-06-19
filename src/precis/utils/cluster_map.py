"""Hierarchical Self-Organizing-Map clustering over chunk embeddings.

Pure, dependency-light (numpy only) engine behind the ``clusterize``
worker (:mod:`precis.workers.clusterize`) and the precis-web cluster
grid (``/clusters``). No DB, no I/O — everything here operates on
in-memory float matrices and keyword histograms so it is cheap to
unit-test.

Why a SOM and not k-means / HDBSCAN
-----------------------------------
The product surface is a *grid* of tiles on a screen. A plain
clusterer gives you K clusters with no spatial meaning — tile (0,0)
is no more related to tile (0,1) than to any other. A Self-Organizing
Map produces a grid whose **adjacent cells are similar**, so panning
around the grid is a meaningful browse gesture and the fixed cell
count matches the screen budget instead of fighting a natural,
variable cluster count.

Scale strategy (see CLAUDE / the clusterize worker)
---------------------------------------------------
* ``train_som`` is a *batch* SOM — fully vectorised, no Python
  per-sample loop (minisom's online update does not survive ~1M
  vectors in a daily job).
* The worker trains on a bounded sample (:func:`build_hierarchy`),
  then assigns the *full* corpus by walking the learned centroids
  top-down (:func:`descend_to_leaf`) in batched numpy — so coverage
  is complete while training stays cheap.
* Word clouds use sibling-scoped class-based TF-IDF
  (:func:`ctfidf_words`): a cell's distinctive terms are scored
  against its *siblings*, not the whole corpus, so a cell three
  levels under "machine learning" does not just shout "learning".

All vectors are expected L2-normalised (:func:`l2_normalize`) so that
Euclidean nearest-centroid equals cosine nearest-centroid.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------
# Stoplist — "common words" the user asked to drop. Two layers: ordinary
# English function words + academic boilerplate that is uniformly common
# across a paper corpus and therefore carries no cluster signal. The
# sibling-scoped c-TF-IDF below also suppresses corpus-common terms
# statistically; this list is the cheap, certain first cut.
# --------------------------------------------------------------------------
_ENGLISH_STOP = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "if",
    "then",
    "else",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "about",
    "from",
    "into",
    "over",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "we",
    "our",
    "us",
    "they",
    "their",
    "them",
    "he",
    "she",
    "his",
    "her",
    "i",
    "you",
    "your",
    "which",
    "who",
    "whom",
    "what",
    "when",
    "where",
    "why",
    "how",
    "all",
    "any",
    "both",
    "each",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "can",
    "will",
    "just",
    "do",
    "does",
    "did",
    "done",
    "have",
    "has",
    "had",
    "having",
    "may",
    "might",
    "must",
    "shall",
    "should",
    "would",
    "could",
    "also",
    "between",
    "during",
    "before",
    "after",
    "above",
    "below",
    "up",
    "down",
    "out",
    "off",
    "again",
    "further",
    "once",
    "here",
    "there",
    "via",
    "per",
}
_ACADEMIC_STOP = {
    "paper",
    "study",
    "studies",
    "approach",
    "approaches",
    "method",
    "methods",
    "methodology",
    "result",
    "results",
    "model",
    "models",
    "modeling",
    "modelling",
    "propose",
    "proposed",
    "present",
    "presented",
    "show",
    "shown",
    "shows",
    "using",
    "use",
    "used",
    "based",
    "novel",
    "framework",
    "system",
    "systems",
    "data",
    "dataset",
    "datasets",
    "experiment",
    "experiments",
    "experimental",
    "analysis",
    "analyses",
    "performance",
    "task",
    "tasks",
    "problem",
    "problems",
    "work",
    "works",
    "technique",
    "techniques",
    "application",
    "applications",
    "figure",
    "table",
    "section",
    "appendix",
    "et",
    "al",
    "eg",
    "ie",
    "etc",
    "fig",
    "respectively",
    "however",
    "therefore",
    "thus",
    "given",
    "different",
    "various",
    "several",
    "two",
    "three",
    "new",
    "non",
}
STOPWORDS: frozenset[str] = frozenset(_ENGLISH_STOP | _ACADEMIC_STOP)


# --------------------------------------------------------------------------
# Vector helpers
# --------------------------------------------------------------------------
def l2_normalize(mat: np.ndarray) -> np.ndarray:
    """Return ``mat`` with each row scaled to unit L2 norm.

    Zero rows are left as zero (norm clamped to 1 to avoid nan). With
    unit rows, Euclidean nearest-centroid == cosine nearest-centroid,
    which is what every BMU lookup below assumes.
    """
    mat = np.asarray(mat, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return mat / norms


def _grid_coords(n_rows: int, n_cols: int) -> np.ndarray:
    """(cells, 2) integer row/col coordinates in row-major order."""
    return np.array(
        [(c // n_cols, c % n_cols) for c in range(n_rows * n_cols)],
        dtype=np.float32,
    )


def assign_bmu(
    vectors: np.ndarray, weights: np.ndarray, *, batch: int = 8192
) -> np.ndarray:
    """Best-matching-unit index per row of ``vectors``.

    Returns an ``int32`` array of length ``len(vectors)`` holding the
    nearest-centroid (smallest Euclidean distance) cell index. Computed
    in batches so the (N × cells) distance block never blows memory.
    Uses ``||w||² − 2·x·w`` (the ``||x||²`` term is constant across
    cells for a fixed row and so drops out of the argmin).
    """
    weights = np.asarray(weights, dtype=np.float32)
    wn = np.einsum("ij,ij->i", weights, weights)  # (cells,)
    out = np.empty(len(vectors), dtype=np.int32)
    for i in range(0, len(vectors), batch):
        xb = vectors[i : i + batch]
        dist = wn[None, :] - 2.0 * (xb @ weights.T)
        out[i : i + batch] = np.argmin(dist, axis=1)
    return out


def train_som(
    vectors: np.ndarray,
    *,
    n_rows: int,
    n_cols: int,
    iters: int = 30,
    seed: int = 0,
    init: np.ndarray | None = None,
) -> np.ndarray:
    """Train a batch SOM; return ``(n_rows*n_cols, dim)`` weights.

    Batch (not online) update: each iteration computes BMUs for *all*
    samples at once, then sets every cell to the neighbourhood-weighted
    mean of the samples — a handful of vectorised matmuls, no Python
    per-sample loop. ``init`` warm-starts from prior weights (used by
    the worker for day-to-day grid stability); otherwise cells seed
    from random samples.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    cells = n_rows * n_cols
    rng = np.random.default_rng(seed)

    if init is not None:
        weights = np.asarray(init, dtype=np.float32).copy()
    else:
        pick = rng.choice(len(vectors), size=cells, replace=len(vectors) < cells)
        weights = vectors[pick].copy()

    coords = _grid_coords(n_rows, n_cols)
    # Pairwise squared grid distance between cells (cells, cells).
    grid_d2 = ((coords[:, None, :] - coords[None, :, :]) ** 2).sum(axis=2)

    sigma0 = max(n_rows, n_cols) / 2.0
    sigma_end = 0.5
    for t in range(max(1, iters)):
        frac = t / max(1, iters - 1)
        sigma = sigma0 * (sigma_end / sigma0) ** frac if sigma0 > 0 else sigma_end
        labels = assign_bmu(vectors, weights)
        # Neighbourhood kernel h[bmu, cell]; A[n, cell] = weight of
        # sample n toward cell, then weighted mean per cell.
        kernel = np.exp(-grid_d2 / (2.0 * sigma * sigma))  # (cells, cells)
        contrib = kernel[labels]  # (N, cells)
        numer = contrib.T @ vectors  # (cells, dim)
        denom = contrib.sum(axis=0)[:, None]  # (cells, 1)
        denom[denom == 0.0] = 1.0
        weights = (numer / denom).astype(np.float32)
    return weights


# --------------------------------------------------------------------------
# Hierarchy
# --------------------------------------------------------------------------
@dataclass
class Cell:
    """One tile in the hierarchical grid.

    ``path`` is the dot-joined chain of cell indices from the root,
    e.g. ``"4.7.1"`` — top-level cell 4, its child 7, its leaf 1.
    ``parent`` is the path with the last component stripped (``None``
    for top level). ``centroid`` is the learned SOM weight (used both
    for full-corpus descent and warm-start), in the *normalised* space.
    """

    path: str
    parent: str | None
    depth: int
    grid_row: int
    grid_col: int
    centroid: np.ndarray
    is_leaf: bool = True
    n_chunks: int = 0
    n_refs: int = 0
    words: list[tuple[str, float]] = field(default_factory=list)


def _child_path(prefix: str, cell_index: int) -> str:
    return f"{prefix}.{cell_index}" if prefix else str(cell_index)


def build_hierarchy(
    vectors: np.ndarray,
    *,
    grid: tuple[int, int] = (3, 3),
    max_depth: int = 3,
    min_chunks: int = 200,
    max_train: int = 50_000,
    iters: int = 30,
    seed: int = 0,
    prior: Mapping[str, np.ndarray] | None = None,
) -> tuple[list[Cell], np.ndarray]:
    """Recursively train SOMs to build an adaptive-depth grid tree.

    Returns ``(cells, leaf_paths)`` where ``cells`` is every node in
    the tree (internal + leaf, including empty tiles so the grid is
    always full) and ``leaf_paths[i]`` is the leaf path of input row
    ``i``.

    Adaptive depth: a cell stops subdividing once it holds fewer than
    ``min_chunks`` members or reaches ``max_depth`` — so sparse
    branches don't spawn near-empty sub-grids. Training subsamples to
    ``max_train`` rows per node (assignment still covers every member
    of the node).

    Stability: pass ``prior`` (path -> previous run's centroid) to
    **warm-start** each node's SOM from the matching prior grid. Because
    a batch SOM started at the prior weights only evolves locally, cell
    ``i`` keeps both its identity *and* its grid position day to day —
    so a tile's address ("4.7.1") stays put as the corpus drifts. This
    is why we warm-start rather than train cold and Hungarian-relabel:
    relabeling would preserve identity but scramble the spatial topology
    (adjacent-tiles-are-similar) that the SOM exists to provide. Nodes
    with no prior (first run, a newly-split branch) fall back to a cold
    or partially-seeded init. Continuity can be measured after the fact
    with :func:`stability_report`.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    n_rows, n_cols = grid
    cells_n = n_rows * n_cols
    cells: list[Cell] = []
    leaf_paths = np.empty(len(vectors), dtype=object)
    rng = np.random.default_rng(seed)

    def _warm_init(prefix: str, train: np.ndarray) -> np.ndarray | None:
        """Grid-ordered init from ``prior``; missing slots seeded from
        random training rows. Returns ``None`` if no prior covers this
        node (⇒ cold init inside ``train_som``)."""
        if prior is None:
            return None
        rows: list[np.ndarray | None] = []
        have = False
        for c in range(cells_n):
            pc = prior.get(_child_path(prefix, c))
            if pc is not None:
                rows.append(np.asarray(pc, dtype=np.float32))
                have = True
            else:
                rows.append(None)
        if not have:
            return None
        filled = [
            r if r is not None else train[int(rng.integers(len(train)))] for r in rows
        ]
        return np.stack(filled).astype(np.float32)

    def recurse(idx: np.ndarray, prefix: str, depth: int) -> None:
        sub = vectors[idx]
        train = sub
        if len(sub) > max_train:
            train = sub[rng.choice(len(sub), size=max_train, replace=False)]
        weights = train_som(
            train,
            n_rows=n_rows,
            n_cols=n_cols,
            iters=iters,
            seed=seed + depth,
            init=_warm_init(prefix, train),
        )
        labels = assign_bmu(sub, weights)
        for c in range(n_rows * n_cols):
            path = _child_path(prefix, c)
            members = idx[labels == c]
            cell = Cell(
                path=path,
                parent=prefix or None,
                depth=depth,
                grid_row=c // n_cols,
                grid_col=c % n_cols,
                centroid=weights[c].astype(np.float32),
                n_chunks=len(members),
            )
            can_split = depth + 1 < max_depth and len(members) >= max(
                min_chunks, n_rows * n_cols
            )
            if can_split:
                cell.is_leaf = False
                cells.append(cell)
                recurse(members, path, depth + 1)
            else:
                cell.is_leaf = True
                cells.append(cell)
                leaf_paths[members] = path

    recurse(np.arange(len(vectors)), "", 0)
    return cells, leaf_paths


def descend_to_leaf(
    vectors: np.ndarray, cells: Sequence[Cell], *, grid: tuple[int, int]
) -> np.ndarray:
    """Assign arbitrary vectors to leaf paths using learned centroids.

    Walks the tree top-down: at each level pick the BMU among the
    present sibling centroids, descend into it if internal, stop at a
    leaf. Fully batched (one ``assign_bmu`` per visited internal node).
    Used by the worker to assign the *full* corpus after training the
    structure on a sample. Returns ``leaf_paths`` parallel to
    ``vectors``.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    n_rows, n_cols = grid
    by_path = {c.path: c for c in cells}
    leaf_paths = np.empty(len(vectors), dtype=object)

    def go(idx: np.ndarray, prefix: str) -> None:
        present = [
            by_path[p]
            for c in range(n_rows * n_cols)
            if (p := _child_path(prefix, c)) in by_path
        ]
        if not present:
            return
        weights = np.stack([c.centroid for c in present])
        labels = assign_bmu(vectors[idx], weights)
        for li, cell in enumerate(present):
            members = idx[labels == li]
            if len(members) == 0:
                continue
            if cell.is_leaf:
                leaf_paths[members] = cell.path
            else:
                go(members, cell.path)

    go(np.arange(len(vectors)), "")
    return leaf_paths


# --------------------------------------------------------------------------
# Word clouds — sibling-scoped class-based TF-IDF
# --------------------------------------------------------------------------
def ancestors(path: str) -> list[str]:
    """All prefixes of ``path`` including itself, root-first.

    ``"4.7.1"`` -> ``["4", "4.7", "4.7.1"]``.
    """
    parts = path.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def rollup_histograms(
    leaf_hist: Mapping[str, Mapping[str, int]],
) -> dict[str, Counter[str]]:
    """Sum leaf keyword histograms into every ancestor path.

    A cell's histogram is the union of its descendant leaves', so
    internal tiles get word clouds too. Input maps leaf path -> {term:
    count}; output maps *every* path (leaf + ancestor) -> Counter.
    """
    out: dict[str, Counter[str]] = {}
    for leaf, hist in leaf_hist.items():
        for path in ancestors(leaf):
            bucket = out.setdefault(path, Counter())
            for term, n in hist.items():
                bucket[term] += n
    return out


def _normalise_term(term: str) -> str:
    return term.strip().lower()


def ctfidf_words(
    hist_by_path: Mapping[str, Mapping[str, int]],
    cells: Sequence[Cell],
    *,
    top_k: int = 25,
    stop: Iterable[str] = STOPWORDS,
    min_chars: int = 2,
) -> dict[str, list[tuple[str, float]]]:
    """Score each cell's distinctive terms against its siblings.

    Class-based TF-IDF (BERTopic-style) where the "corpus" for a given
    cell is its sibling group, not the whole tree::

        tf   = count(term, cell) / total_terms(cell)
        idf  = log(1 + avg_terms_per_sibling / freq(term across siblings))
        score = tf * idf

    Terms in ``stop`` or shorter than ``min_chars`` are dropped. Returns
    path -> ``[(term, score), ...]`` truncated to ``top_k``, sorted
    descending.
    """
    stopset = {_normalise_term(s) for s in stop}
    # Group cell paths by parent so siblings are scored together.
    groups: dict[str | None, list[str]] = {}
    for cell in cells:
        groups.setdefault(cell.parent, []).append(cell.path)

    result: dict[str, list[tuple[str, float]]] = {}
    for sibling_paths in groups.values():
        # Per-cell cleaned histograms + sibling-wide term frequency.
        cleaned: dict[str, Counter[str]] = {}
        group_freq: Counter[str] = Counter()
        for path in sibling_paths:
            raw = hist_by_path.get(path, {})
            cnt: Counter[str] = Counter()
            for term, n in raw.items():
                norm = _normalise_term(term)
                if len(norm) < min_chars or norm in stopset:
                    continue
                cnt[norm] += n
            cleaned[path] = cnt
            group_freq.update(cnt)

        sizes = [sum(c.values()) for c in cleaned.values()]
        nonempty = [s for s in sizes if s > 0]
        avg_terms = (sum(nonempty) / len(nonempty)) if nonempty else 0.0

        for path, cnt in cleaned.items():
            total = sum(cnt.values())
            if total == 0:
                result[path] = []
                continue
            scored: list[tuple[str, float]] = []
            for term, n in cnt.items():
                tf = n / total
                idf = math.log(1.0 + avg_terms / max(1, group_freq[term]))
                scored.append((term, tf * idf))
            scored.sort(key=lambda kv: kv[1], reverse=True)
            result[path] = scored[:top_k]
    return result


# --------------------------------------------------------------------------
# Stability measurement — did warm-start keep cell identity?
# --------------------------------------------------------------------------
def linear_sum_assignment(cost: np.ndarray) -> tuple[list[int], list[int]]:
    """Minimum-cost perfect matching (Hungarian / Kuhn–Munkres).

    Square ``cost`` only (we match a node's cells one-to-one). Returns
    ``(rows, cols)`` such that ``rows[k]`` is matched to ``cols[k]`` and
    the total cost is minimised. O(n³); n is a grid's cell count (≤ a
    few dozen), so this is instant. Pure-numpy so no scipy dependency.
    """
    cost = np.asarray(cost, dtype=np.float64)
    n = cost.shape[0]
    if cost.shape[1] != n:
        raise ValueError("linear_sum_assignment requires a square cost matrix")
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = row matched to column j (1-indexed)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    row_to_col = [0] * n
    for j in range(1, n + 1):
        if p[j] != 0:
            row_to_col[p[j] - 1] = j - 1
    return list(range(n)), row_to_col


def stability_report(
    cells: Sequence[Cell], prior: Mapping[str, np.ndarray] | None
) -> dict[str, float]:
    """How well did this run hold the previous run's top-level identities?

    Compares each top-level (``parent is None``) cell against the prior
    centroid at the *same* path, over paths present in both runs:

    * ``self_cos``  — mean cosine of cell ``i`` to prior cell ``i``
      (1.0 ⇒ the tile barely moved).
    * ``identity``  — fraction of cells whose *optimal* (Hungarian)
      match is itself; < 1.0 means some tiles would have been better
      labelled as a sibling, i.e. warm-start let them cross.
    * ``n``         — number of matched top-level tiles (0 ⇒ no prior).

    Cheap, read-only diagnostics — surfaced in the run's ``note`` so the
    prod cadence can be watched without re-deriving anything.
    """
    if not prior:
        return {"n": 0.0, "self_cos": 0.0, "identity": 0.0}
    tops = [c for c in cells if c.parent is None]
    paths = [c.path for c in tops if c.path in prior]
    if not paths:
        return {"n": 0.0, "self_cos": 0.0, "identity": 0.0}
    by_path = {c.path: c for c in tops}
    new_mat = l2_normalize(np.stack([by_path[p].centroid for p in paths]))
    pri_mat = l2_normalize(np.stack([np.asarray(prior[p]) for p in paths]))
    self_cos = float(np.mean(np.einsum("ij,ij->i", new_mat, pri_mat)))
    # Cost = cosine distance; identity-preserving ⇒ argmin on the diagonal.
    cost = 1.0 - (new_mat @ pri_mat.T)
    _, matched = linear_sum_assignment(cost)
    identity = float(
        np.mean([1.0 if matched[i] == i else 0.0 for i in range(len(paths))])
    )
    return {"n": float(len(paths)), "self_cos": self_cos, "identity": identity}
