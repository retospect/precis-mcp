"""Rung 6b of the paper-writing pipeline: deterministic residual-paper
clustering (docs/design/paper-writing-pipeline.md §Integrate step 3).
Controlled one-hot / grouped vectors stand in for real bge-m3 embeddings —
same technique as ``tests/test_placement.py`` — so cosine-distance
agglomeration is exercised without a model in the loop.
"""

from __future__ import annotations

import numpy as np

from precis.quest.residual_cluster import (
    DEFAULT_CLUSTER_DISTANCE,
    DEFAULT_MIN_TO_CLUSTER,
    cluster_residual,
    unclusterable_paper_ids,
)
from precis.store import Store

_DEFAULT_EMBEDDER = "bge-m3"  # the migration-seeded default (embedders.dim=1024)
_DIM = 1024


def _onehot(i: int, *, jitter: float = 0.0) -> list[float]:
    """A near-``e_i`` unit vector; ``jitter`` nudges a second coordinate so
    members of the same group aren't bit-identical (still cosine-tight)."""
    v = [0.0] * _DIM
    v[i] = 1.0
    if jitter:
        v[(i + 1) % _DIM] = jitter
    norm = float(np.linalg.norm(v))
    return [x / norm for x in v]


def _embed(store: Store, chunk_id: int, vec: list[float]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (chunk_id, _DEFAULT_EMBEDDER, vec),
        )


def _body_chunk_with_keywords(
    store: Store, ref_id: int, ord_: int, keywords: list[str]
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, keywords, meta) "
            "VALUES (%s, %s, 'paragraph', 'body text', %s, '{}'::jsonb)",
            (ref_id, ord_, keywords),
        )


def _paper(
    store: Store,
    slug: str,
    vec: list[float] | None,
    *,
    keywords: list[str] | None = None,
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"Title of {slug}", meta={})
    if vec is not None:
        cid = store.upsert_card_combined(ref.id, "gist")
        _embed(store, cid, vec)
    if keywords is not None:
        _body_chunk_with_keywords(store, ref.id, 0, keywords)
    return ref.id


def test_two_well_separated_groups_cluster_separately(store: Store) -> None:
    group_a = [
        _paper(store, f"a{i}", _onehot(0, jitter=0.01 * i), keywords=["alpha", "beta"])
        for i in range(6)
    ]
    group_b = [
        _paper(store, f"b{i}", _onehot(500, jitter=0.01 * i), keywords=["gamma"])
        for i in range(6)
    ]

    clusters = cluster_residual(store, group_a + group_b, min_to_cluster=2)

    assert len(clusters) == 2
    members_by_cluster = [set(c["paper_ref_ids"]) for c in clusters]
    assert set(group_a) in members_by_cluster
    assert set(group_b) in members_by_cluster


def test_exemplar_titles_come_from_members_and_are_capped(store: Store) -> None:
    ids = [_paper(store, f"e{i}", _onehot(0, jitter=0.001 * i)) for i in range(8)]

    clusters = cluster_residual(store, ids, min_to_cluster=2)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert 1 <= len(cluster["exemplar_titles"]) <= 5
    valid_titles = {f"Title of e{i}" for i in range(8)}
    assert set(cluster["exemplar_titles"]) <= valid_titles


def test_label_reflects_member_keywords(store: Store) -> None:
    ids = [
        _paper(store, f"k{i}", _onehot(0, jitter=0.001 * i), keywords=["catalysis"])
        for i in range(5)
    ]
    # One member with a distinct, less-frequent keyword — should not crowd
    # out the majority term from the top of the label.
    ids.append(_paper(store, "k5", _onehot(0, jitter=0.005), keywords=["outlier"]))

    clusters = cluster_residual(store, ids, min_to_cluster=2)

    assert len(clusters) == 1
    assert "catalysis" in clusters[0]["label"]


def test_below_min_to_cluster_returns_single_cluster(store: Store) -> None:
    # Three papers, deliberately orthogonal, would split into 3 clusters
    # above threshold — but min_to_cluster=15 forces the "too few to
    # segment" shortcut.
    ids = [
        _paper(store, "s0", _onehot(0)),
        _paper(store, "s1", _onehot(300)),
        _paper(store, "s2", _onehot(700)),
    ]

    clusters = cluster_residual(store, ids, min_to_cluster=15)

    assert len(clusters) == 1
    assert set(clusters[0]["paper_ref_ids"]) == set(ids)
    assert clusters[0]["size"] == 3


def test_unembedded_paper_excluded_and_surfaced_as_unclusterable(
    store: Store,
) -> None:
    embedded = [_paper(store, f"u{i}", _onehot(0, jitter=0.001 * i)) for i in range(5)]
    unembedded = _paper(store, "u_none", None)

    clusters = cluster_residual(store, embedded + [unembedded], min_to_cluster=2)

    all_clustered_ids = {pid for c in clusters for pid in c["paper_ref_ids"]}
    assert unembedded not in all_clustered_ids
    assert all_clustered_ids == set(embedded)
    assert unclusterable_paper_ids(store, embedded + [unembedded]) == [unembedded]


def test_no_clusterable_papers_returns_empty_list(store: Store) -> None:
    unembedded = _paper(store, "empty0", None)

    assert cluster_residual(store, [unembedded]) == []


def test_clusters_sorted_by_size_descending(store: Store) -> None:
    big = [_paper(store, f"big{i}", _onehot(0, jitter=0.001 * i)) for i in range(6)]
    small = [
        _paper(store, f"small{i}", _onehot(500, jitter=0.001 * i)) for i in range(2)
    ]

    clusters = cluster_residual(store, big + small, min_to_cluster=2)

    assert [c["size"] for c in clusters] == sorted(
        (c["size"] for c in clusters), reverse=True
    )
    assert clusters[0]["size"] >= clusters[-1]["size"]


def test_default_constants() -> None:
    assert DEFAULT_CLUSTER_DISTANCE == 0.5
    assert DEFAULT_MIN_TO_CLUSTER == 15
