"""Integration test for the ``clusterize`` worker pass (real test DB).

Seeds three themed paper "blobs" — distinct keyword vocab + separated
1024-d embeddings — runs one rebuild, and checks the run row, the full
9-tile grid, complete assignment coverage, themed word clouds, and the
time-gate (a second call is a no-op).
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from precis.store import Store
from precis.workers.clusterize import _finish_run, run_clusterize_pass

pytestmark = pytest.mark.usefixtures("store")


# Three themes: a separated embedding axis + distinctive keywords, plus a
# shared term ("learning") and a stopword ("method") that the word clouds
# must respectively down-rank and drop.
_THEMES = [
    (0, ["transformer", "attention", "learning", "method"]),
    (1, ["diffusion", "sampling", "learning", "method"]),
    (2, ["graph", "message passing", "learning", "method"]),
]


def _vec_literal(axis: int, rng: np.random.Generator) -> str:
    v = rng.normal(scale=0.15, size=1024).astype(np.float32)
    v[axis] += 10.0
    return "[" + ",".join(f"{x:.6g}" for x in v) + "]"


def _seed_theme(store: Store, axis: int, keywords: list[str], *, n: int, rng) -> None:
    with store.pool.connection() as conn:
        ref_id = conn.execute(
            "INSERT INTO refs (kind, set_by, title) "
            "VALUES ('paper', 'system', %s) RETURNING ref_id",
            (f"theme-{axis} paper",),
        ).fetchone()[0]
        for i in range(n):
            chunk_id = conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, keywords) "
                "VALUES (%s, 'system', %s, 'paragraph', %s, %s) RETURNING chunk_id",
                (ref_id, i, f"theme {axis} body {i}", keywords),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
                "VALUES (%s, 'bge-m3', %s::vector, 'ok')",
                (chunk_id, _vec_literal(axis, rng)),
            )
        conn.commit()


@pytest.fixture
def seeded(store: Store) -> Iterator[Store]:
    rng = np.random.default_rng(0)
    for axis, kws in _THEMES:
        _seed_theme(store, axis, kws, n=12, rng=rng)
    yield store


def test_clusterize_builds_map(seeded: Store) -> None:
    store = seeded
    result = run_clusterize_pass(store)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}

    with store.pool.connection() as conn:
        run = conn.execute(
            "SELECT run_id, status, n_vectors FROM cluster_runs "
            "WHERE scope='paper' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert run is not None
        run_id, status, n_vectors = run
        assert status == "ok"
        assert n_vectors == 36  # 3 themes × 12 chunks

        # A full 3×3 grid (no split — below min_chunks), all leaves.
        cells = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_leaf) "
            "FROM cluster_cells WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        assert cells == (9, 9)

        # Every chunk assigned exactly once.
        n_assigned = conn.execute(
            "SELECT count(*) FROM cluster_assignments WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        assert n_assigned == 36

        # Word clouds: themed terms surface; the stopword never does.
        all_words = (
            conn.execute(
                "SELECT jsonb_agg(w->>'w') "
                "FROM cluster_cells, jsonb_array_elements(words) AS w "
                "WHERE run_id=%s",
                (run_id,),
            ).fetchone()[0]
            or []
        )
        assert "method" not in all_words  # stoplist dropped it
        assert any(t in all_words for t in ("transformer", "diffusion", "graph"))


def _theme_path(store: Store, run_id: int, axis: int) -> str:
    """Dominant leaf path of theme ``axis``'s chunks in ``run_id``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT a.leaf_path FROM cluster_assignments a "
            "JOIN refs r ON r.ref_id = a.ref_id "
            "WHERE a.run_id = %s AND r.title = %s",
            (run_id, f"theme-{axis} paper"),
        ).fetchall()
    from collections import Counter

    return Counter(r[0] for r in rows).most_common(1)[0][0]


def test_warm_start_keeps_addresses_across_runs(seeded: Store, monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_CLUSTER_INTERVAL_HOURS", "0")  # always due
    store = seeded

    assert run_clusterize_pass(store)["claimed"] == 1
    with store.pool.connection() as conn:
        run1 = conn.execute(
            "SELECT run_id FROM cluster_runs WHERE scope='paper' AND status='ok'"
        ).fetchone()[0]
    addr1 = {axis: _theme_path(store, run1, axis) for axis in (0, 1, 2)}

    # Perturb the corpus (more of theme 0), then rebuild — warm-started
    # from run1 because the prior is loaded before the prune.
    rng = np.random.default_rng(7)
    _seed_theme(store, 0, _THEMES[0][1], n=6, rng=rng)
    assert run_clusterize_pass(store)["claimed"] == 1

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT run_id, note FROM cluster_runs "
            "WHERE scope='paper' AND status='ok' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    run2, note = row
    assert run2 != run1
    # Stability metric recorded, and the map held identity (mild drift).
    assert note.startswith("stability: self_cos=")
    assert "identity=1.00" in note
    # Each theme keeps its tile address across the rebuild.
    for axis in (0, 1, 2):
        assert _theme_path(store, run2, axis) == addr1[axis]


def test_time_gate_blocks_immediate_rebuild(seeded: Store) -> None:
    store = seeded
    assert run_clusterize_pass(store)["claimed"] == 1
    # Paper just rebuilt; the only other scope (memory) has no candidates
    # so its rebuild finishes instantly green — after that nothing is due.
    run_clusterize_pass(store)  # memory → ok (0 vectors)
    assert run_clusterize_pass(store)["claimed"] == 0


def _insert_run(store: Store, scope: str, status: str) -> int:
    with store.pool.connection() as conn:
        run_id = conn.execute(
            "INSERT INTO cluster_runs (scope, status) VALUES (%s, %s) "
            "RETURNING run_id",
            (scope, status),
        ).fetchone()[0]
        conn.commit()
    return int(run_id)


def _run_exists(store: Store, run_id: int) -> bool:
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM cluster_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
            is not None
        )


def test_finish_run_spares_concurrent_building_runs(store: Store) -> None:
    """_finish_run must not reap a peer's in-flight 'building' run.

    clusterize runs on every node, so several hosts rebuild a scope at
    once. A still-'building' run — even one with a *lower* run_id (an
    earlier-started but slower build, e.g. the ~50k-vector paper SOM) —
    must survive a faster peer finishing, or that peer's prune deletes
    the row mid-COPY and the build dies with a cluster_assignments FK
    violation. Only already-green runs may be pruned.
    """
    old_green = _insert_run(store, "paper", "ok")  # superseded green
    slow_peer = _insert_run(store, "paper", "building")  # in-flight, lower id
    winner = _insert_run(store, "paper", "building")  # finishes first

    _finish_run(store, winner, "paper", n_vectors=123)

    assert _run_exists(store, winner)  # the run we just finished
    assert _run_exists(store, slow_peer)  # the bug: must NOT be reaped
    assert not _run_exists(store, old_green)  # superseded green still pruned
