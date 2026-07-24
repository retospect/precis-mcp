"""Rung 6b of the paper-writing pipeline — deterministic residual-paper
clustering (docs/design/paper-writing-pipeline.md §Integrate — the tick
body, step 3: Residual → section: "cluster deferred papers (gist embedding
+ KeyBERT labels, deterministic); the model judges a digest (label + 3-5
exemplar titles), not raw titles; skip clustering when residual <~15").

Sibling to rung 6a (:mod:`precis.quest.placement`) — the papers this module
receives are exactly those :func:`precis.quest.placement.residual_paper_ids`
returned: no section centroid cleared the floor for them. This slice is the
DETERMINISTIC clustering + digest-prep only. Pure geometry (gist embedding
cosine distance) + keyword frequency over already-computed
``chunks.keywords`` — **no model call**; the model judging the digest is a
later slice. No MCP surface — consumed by the weave tick (rung 6e).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np

#: Cosine-distance threshold at which :class:`AgglomerativeClustering`
#: (average linkage) stops merging. The design (§Residual sub-questions:
#: "Cluster algorithm/threshold") flags the exact value as an open tunable,
#: not yet validated against a real residual set — 0.5 is a starting
#: default for bge-m3 cosine geometry.
DEFAULT_CLUSTER_DISTANCE = 0.5

#: Below this many clusterable papers, skip segmentation and return one
#: cluster holding them all — the design's "skip clustering when residual
#: <~15" (too few points to meaningfully split). Same open-tunable status
#: as ``DEFAULT_CLUSTER_DISTANCE`` above.
DEFAULT_MIN_TO_CLUSTER = 15

#: How many keywords a cluster's ``label`` keeps.
_LABEL_TOP_K = 5

#: How many exemplar titles (nearest cluster centroid) a digest keeps.
_MAX_EXEMPLARS = 5


def _gist_vectors(store: Any, paper_ref_ids: list[int]) -> dict[int, list[float]]:
    """``{paper_ref_id: gist vector}`` for the subset that has one.

    Same primitive rung 6a uses (``seed_chunk_for_ref`` -> ``get_chunk_
    vector``): prefers a paper's ``card_combined`` chunk, falls back to
    its head body chunk. A paper with neither is simply absent — see
    :func:`unclusterable_paper_ids`.
    """
    out: dict[int, list[float]] = {}
    for pid in paper_ref_ids:
        seed_cid = store.seed_chunk_for_ref(pid)
        vec = store.get_chunk_vector(seed_cid) if seed_cid is not None else None
        if vec is not None:
            out[pid] = vec
    return out


def unclusterable_paper_ids(store: Any, paper_ref_ids: list[int]) -> list[int]:
    """The subset of ``paper_ref_ids`` with no gist vector at all.

    :func:`cluster_residual` silently drops these from every cluster (they
    can't be placed in cosine space), so the weave tick calls this
    separately to keep them discoverable rather than letting them vanish
    from the residual set.
    """
    have = _gist_vectors(store, paper_ref_ids)
    return [pid for pid in paper_ref_ids if pid not in have]


def _keyword_label(store: Any, member_ids: list[int]) -> list[str]:
    """Top ``_LABEL_TOP_K`` keywords across ``member_ids``' body chunks.

    Frequency count over already-computed ``chunks.keywords`` (the
    ``chunk_keywords`` worker's output — read directly, never recomputed
    here). Papers with no keywords contribute nothing.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT keywords FROM chunks "
            "WHERE ref_id = ANY(%s) AND ord >= 0 AND keywords IS NOT NULL",
            (member_ids,),
        ).fetchall()
    counts: Counter[str] = Counter()
    for (kws,) in rows:
        counts.update(kws or [])
    return [term for term, _n in counts.most_common(_LABEL_TOP_K)]


def _titles(store: Any, ref_ids: list[int]) -> dict[int, str]:
    refs = store.fetch_refs_by_ids(ref_ids)
    return {rid: ref.title for rid, ref in refs.items()}


def _rank_by_cosine_to_centroid(member_vecs: np.ndarray[Any, Any]) -> list[int]:
    """Row indices of ``member_vecs``, nearest-to-centroid first.

    Centroid is the mean of the members' (not-necessarily-unit) gist
    vectors; ranks by cosine similarity to it, mirroring
    :func:`precis.quest.placement._cosine`'s explicit norm handling
    (rather than assuming pre-normalised rows) so a degenerate all-zero
    centroid never divides by zero.
    """
    centroid = np.mean(member_vecs, axis=0)
    denom = np.linalg.norm(member_vecs, axis=1) * np.linalg.norm(centroid)
    denom[denom == 0.0] = 1.0
    sims = (member_vecs @ centroid) / denom
    return [int(i) for i in np.argsort(-sims)]


def _build_cluster(
    store: Any,
    member_ids: list[int],
    member_vecs: np.ndarray[Any, Any],
    titles: dict[int, str],
) -> dict[str, Any]:
    order = _rank_by_cosine_to_centroid(member_vecs)
    ranked_ids = [member_ids[i] for i in order]
    exemplar_titles = [
        titles[rid] for rid in ranked_ids[:_MAX_EXEMPLARS] if rid in titles
    ]
    return {
        "paper_ref_ids": list(member_ids),
        "size": len(member_ids),
        "label": _keyword_label(store, member_ids),
        "exemplar_titles": exemplar_titles,
    }


def cluster_residual(
    store: Any,
    paper_ref_ids: list[int],
    *,
    distance_threshold: float = DEFAULT_CLUSTER_DISTANCE,
    min_to_cluster: int = DEFAULT_MIN_TO_CLUSTER,
) -> list[dict[str, Any]]:
    """Cluster a dossier's residual papers (§Integrate step 3).

    1. Gathers each paper's gist vector (:func:`_gist_vectors`); papers with
       none are dropped here (see :func:`unclusterable_paper_ids` to recover
       them).
    2. **Below-threshold shortcut** — fewer than ``min_to_cluster``
       clusterable papers ⇒ a single cluster holding them all (too few to
       segment meaningfully).
    3. Otherwise, :class:`~sklearn.cluster.AgglomerativeClustering` (cosine
       metric, average linkage, ``distance_threshold``-gated stop — no
       fixed ``k``) over the stacked gist vectors.
    4. Each cluster becomes a **digest** dict the model judges later,
       never raw member titles:

       * ``paper_ref_ids`` — the members.
       * ``size`` — ``len(paper_ref_ids)``.
       * ``label`` — top keywords across members' body-chunk
         ``chunks.keywords`` (frequency count).
       * ``exemplar_titles`` — up to 5 member titles nearest the cluster
         centroid.

    Returns clusters sorted by ``size`` descending. ``[]`` when nothing in
    ``paper_ref_ids`` has a gist vector.
    """
    gist = _gist_vectors(store, paper_ref_ids)
    ids = list(gist)
    if not ids:
        return []
    titles = _titles(store, ids)
    vecs = np.asarray([gist[pid] for pid in ids], dtype=np.float64)

    if len(ids) < min_to_cluster:
        clusters = [_build_cluster(store, ids, vecs, titles)]
    else:
        # sklearn arrives only via the [paper]/[embed] extras, not core — a
        # lazy guarded import (matching scripts/classify/_cluster_papers.py) so
        # importing this module never hard-fails on a worker profile without
        # it; the error only surfaces if clustering is actually attempted there.
        try:
            from sklearn.cluster import AgglomerativeClustering
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "residual clustering needs scikit-learn (install the [embed] "
                "or [paper] extra); the weave-tick worker profile must carry it"
            ) from exc
        model = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=distance_threshold,
            metric="cosine",
            linkage="average",
        )
        labels = model.fit_predict(vecs)
        clusters = []
        for label in sorted(set(int(lb) for lb in labels)):
            idxs = [i for i, lb in enumerate(labels) if int(lb) == label]
            member_ids = [ids[i] for i in idxs]
            member_vecs = vecs[idxs]
            clusters.append(_build_cluster(store, member_ids, member_vecs, titles))

    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


__all__ = [
    "DEFAULT_CLUSTER_DISTANCE",
    "DEFAULT_MIN_TO_CLUSTER",
    "cluster_residual",
    "unclusterable_paper_ids",
]
