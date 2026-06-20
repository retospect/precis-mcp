"""``clusterize`` worker pass — daily hierarchical SOM cluster maps.

One pass rebuilds at most one *scope* ('paper' or 'memory') and is
time-gated: a scope is rebuilt only when its newest green run is older
than ``PRECIS_CLUSTER_INTERVAL_HOURS`` (default 20h). So dropping the
pass into the per-minute system worker still yields one rebuild a day
per scope, idle the rest of the time — same shape as the dedup-window
reviewers.

Pipeline per rebuild (see :mod:`precis.utils.cluster_map` for the math):

1. Count candidate chunks (embedded + keyworded, of the scope's kind).
2. Load a *bounded sample* (modulo-strided on ``chunk_id``) into RAM
   and train the hierarchy on it — training never sees the full ~1M.
3. Stream the *full* candidate set in batches, descend each vector to
   its leaf via the learned centroids, and COPY the assignments — so
   coverage is complete while training stays cheap.
4. Roll leaf keyword histograms up the tree and compute sibling-scoped
   c-TF-IDF word clouds; update the cells.
5. Mark the run green and prune older runs for the scope.

numpy is the only added dependency; if it is somehow absent the pass
degrades to a no-op with a single warning rather than crashing the
system worker.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

EMBEDDER = "bge-m3"
_VECTOR_BATCH = 5_000
_TOP_TERMS_PER_LEAF = 200


@dataclass(frozen=True)
class ScopeCfg:
    """Per-scope clustering shape. The two maps are deliberately
    asymmetric — the paper corpus (~1M chunks) wants a deep tree; the
    memory corpus (a few thousand) wants a single shallow grid or it
    is mostly empty tiles."""

    kind: str
    grid: tuple[int, int]
    max_depth: int
    min_chunks: int
    max_train: int


SCOPES: dict[str, ScopeCfg] = {
    "paper": ScopeCfg(
        kind="paper", grid=(3, 3), max_depth=3, min_chunks=200, max_train=50_000
    ),
    "memory": ScopeCfg(
        kind="memory", grid=(3, 3), max_depth=1, min_chunks=40, max_train=20_000
    ),
}


def _interval_hours() -> float:
    try:
        return float(os.environ.get("PRECIS_CLUSTER_INTERVAL_HOURS", "20"))
    except ValueError:
        return 20.0


#: A scope already has a ``building`` run younger than this → another host
#: is (probably) mid-rebuild, so this host skips it. Without this guard the
#: time-gate keys only on the newest *green* run; until the first build
#: completes there is none, so EVERY host every rotation starts a fresh
#: whole-corpus SOM rebuild (a thundering herd — observed as 4 hosts each
#: training over ~900k vectors within the same few seconds). The window
#: must exceed a realistic full build; a stuck/crashed builder past it is
#: taken over.
_BUILD_STALE_HOURS = 2.0

#: After a ``failed`` newest run, wait this long before retrying the scope
#: — bounds the retry rate when a scope rebuild is genuinely broken.
_FAILED_BACKOFF_HOURS = 0.5


# --------------------------------------------------------------------------
# SQL fragments
# --------------------------------------------------------------------------
_CANDIDATE_WHERE = """
      FROM chunks c
      JOIN chunk_embeddings ce
        ON ce.chunk_id = c.chunk_id AND ce.embedder = %(embedder)s
       AND ce.status = 'ok'
      JOIN refs r ON r.ref_id = c.ref_id
     WHERE r.kind = %(kind)s
       AND r.deleted_at IS NULL
       AND c.keywords IS NOT NULL
"""


def _parse_vec(text: str) -> list[float]:
    """Parse pgvector text ``"[a, b, ...]"`` into floats."""
    s = text.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]


def _vec_literal(vec: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(x):.6g}" for x in vec) + "]"


# --------------------------------------------------------------------------
# Pass entry point
# --------------------------------------------------------------------------
def run_clusterize_pass(store, *, batch_size: int = 50) -> dict[str, int]:
    """Rebuild the first *due* scope, if any.

    Returns ``{"claimed": n, "ok": k, "failed": f}`` where ``claimed``
    is 1 when a rebuild ran this call (0 = nothing due, the steady
    state). ``batch_size`` is accepted for run-loop uniformity and
    ignored — a rebuild is a single whole-scope job, not a queue drain.
    """
    try:
        import numpy  # noqa: F401
    except ImportError:
        log.warning("clusterize: numpy unavailable; pass is a no-op")
        return {"claimed": 0, "ok": 0, "failed": 0}

    due = _due_scope(store)
    if due is None:
        return {"claimed": 0, "ok": 0, "failed": 0}

    log.info("clusterize: rebuilding scope=%s", due)
    try:
        n = _rebuild_scope(store, SCOPES[due])
        log.info("clusterize: scope=%s rebuilt over %d vectors", due, n)
        return {"claimed": 1, "ok": 1, "failed": 0}
    except Exception:
        log.exception("clusterize: scope=%s rebuild failed", due)
        return {"claimed": 1, "ok": 0, "failed": 1}


def _due_scope(store) -> str | None:
    """First scope whose newest green run predates the interval.

    Returns ``None`` (whole pass becomes a no-op) when the ``cluster_*``
    tables are absent. This pass ships with the worker code, but its
    schema (migration ``0027_clusterize.sql``) is a separate deploy
    step; a node running fresh code against a not-yet-migrated DB would
    otherwise raise ``UndefinedTable`` out of the ref-pass on *every*
    runner rotation, crash-spamming the logs (~3k ERROR/host/6h in a
    real incident). Degrade to a no-op and self-enable once the
    migration lands.
    """
    import psycopg

    horizon = _interval_hours()
    try:
        with store.pool.connection() as conn:
            for scope in SCOPES:
                # Inspect the *newest* run of any status — not just the
                # newest green one — so an in-flight build by another
                # host suppresses re-entry (no thundering herd).
                row = conn.execute(
                    """
                    SELECT status,
                           EXTRACT(EPOCH FROM (now()
                               - COALESCE(finished_at, started_at))) / 3600.0
                      FROM cluster_runs
                     WHERE scope = %s
                     ORDER BY run_id DESC
                     LIMIT 1
                    """,
                    (scope,),
                ).fetchone()
                if _scope_is_due(row, horizon):
                    return scope
    except psycopg.errors.UndefinedTable:
        _warn_missing_schema_once()
        return None
    return None


def _scope_is_due(row: tuple | None, horizon: float) -> bool:
    """Decide whether a scope needs a rebuild from its newest run.

    * no run yet → due (first build).
    * newest ``ok`` → due once older than the rebuild interval.
    * newest ``building`` → NOT due while fresh (another host owns it);
      due again only if stale past ``_BUILD_STALE_HOURS`` (crashed
      builder → take over).
    * newest ``failed`` → due after ``_FAILED_BACKOFF_HOURS`` (bounded
      retry, not every rotation).
    """
    if row is None:
        return True
    status, age_h = row[0], (float(row[1]) if row[1] is not None else 0.0)
    if status == "ok":
        return age_h >= horizon
    if status == "building":
        return age_h >= _BUILD_STALE_HOURS
    # 'failed' or any unexpected terminal state.
    return age_h >= _FAILED_BACKOFF_HOURS


#: Module-level latch so the "schema absent" path warns once per process
#: instead of on every rotation — the whole point is to *stop* the flood.
_SCHEMA_WARNED = False


def _warn_missing_schema_once() -> None:
    global _SCHEMA_WARNED
    if not _SCHEMA_WARNED:
        log.warning(
            "clusterize: cluster_* tables absent (migration 0027 not "
            "applied); pass is a no-op until the schema lands"
        )
        _SCHEMA_WARNED = True


def _load_prior_centroids(store, scope: str):
    """Map ``path -> centroid`` from the current green run for ``scope``.

    Returns ``{}`` when there is no prior run (first build) — callers
    treat that as a cold start. Used to warm-start the SOM so tile
    addresses persist across daily rebuilds.
    """
    import numpy as np

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT cc.path, cc.centroid::text
              FROM cluster_cells cc
             WHERE cc.centroid IS NOT NULL
               AND cc.run_id = (
                   SELECT run_id FROM cluster_runs
                    WHERE scope = %s AND status = 'ok'
                    ORDER BY finished_at DESC LIMIT 1
               )
            """,
            (scope,),
        ).fetchall()
    return {r[0]: np.array(_parse_vec(r[1]), dtype=np.float32) for r in rows}


def _rebuild_scope(store, cfg: ScopeCfg) -> int:
    """Full rebuild for one scope. Returns the candidate vector count."""
    import numpy as np

    from precis.utils.cluster_map import (
        build_hierarchy,
        ctfidf_words,
        descend_to_leaf,
        l2_normalize,
        rollup_histograms,
        stability_report,
    )

    params = {"embedder": EMBEDDER, "kind": cfg.kind}

    # 1. Count candidates + load the previous run's centroids (for
    #    warm-start). Loaded now, before pruning runs the old map away.
    with store.pool.connection() as conn:
        total = conn.execute("SELECT count(*) " + _CANDIDATE_WHERE, params).fetchone()[
            0
        ]
    prior = _load_prior_centroids(store, cfg.kind)

    # Open the run row up front so failures leave a 'building'/'failed'
    # trail and the time-gate doesn't thrash on an empty corpus.
    run_params = {
        "grid": list(cfg.grid),
        "max_depth": cfg.max_depth,
        "min_chunks": cfg.min_chunks,
        "max_train": cfg.max_train,
    }
    with store.pool.connection() as conn:
        run_id = conn.execute(
            """
            INSERT INTO cluster_runs (scope, status, params, n_vectors)
            VALUES (%s, 'building', %s, %s)
            RETURNING run_id
            """,
            (cfg.kind, Jsonb(run_params), total),
        ).fetchone()[0]

    if total == 0:
        _finish_run(store, run_id, cfg.kind, note="no candidate vectors")
        return 0

    try:
        # 2. Load training sample (modulo-strided) into a matrix.
        stride = max(1, (total + cfg.max_train - 1) // cfg.max_train)
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ce.vector::text "
                + _CANDIDATE_WHERE
                + " AND c.chunk_id %% %(stride)s = 0",
                {**params, "stride": stride},
            ).fetchall()
        sample = l2_normalize(
            np.array([_parse_vec(r[0]) for r in rows], dtype=np.float32)
        )
        log.info(
            "clusterize: scope=%s total=%d stride=%d sample=%d",
            cfg.kind,
            total,
            stride,
            len(sample),
        )

        # 3. Build the hierarchy from the sample, warm-started from the
        #    prior run so tile addresses stay stable day to day.
        cells, _ = build_hierarchy(
            sample,
            grid=cfg.grid,
            max_depth=cfg.max_depth,
            min_chunks=cfg.min_chunks,
            max_train=cfg.max_train,
            prior=prior,
        )
        _write_cells(store, run_id, cells)

        # 4. Stream the full set; descend; COPY assignments.
        _assign_all(store, run_id, cfg, params, cells, descend_to_leaf)

        # 5. Word clouds + per-cell counts, then finish.
        _compute_word_clouds(store, run_id, cells, rollup_histograms, ctfidf_words)
        stab = stability_report(cells, prior)
        if stab["n"]:
            note = (
                f"stability: self_cos={stab['self_cos']:.3f} "
                f"identity={stab['identity']:.2f} over {int(stab['n'])} tiles"
            )
        else:
            note = "stability: n/a (first run, no prior)"
        log.info("clusterize: scope=%s %s", cfg.kind, note)
        _finish_run(store, run_id, cfg.kind, n_vectors=total, note=note)
        return total
    except Exception:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE cluster_runs SET status='failed', finished_at=now() "
                "WHERE run_id=%s",
                (run_id,),
            )
        raise


def _write_cells(store, run_id: int, cells) -> None:
    with store.pool.connection() as conn:
        with conn.cursor() as cur:
            for cell in cells:
                cur.execute(
                    """
                    INSERT INTO cluster_cells
                        (run_id, path, parent_path, depth, grid_row, grid_col,
                         is_leaf, n_chunks, centroid)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    """,
                    (
                        run_id,
                        cell.path,
                        cell.parent,
                        cell.depth,
                        cell.grid_row,
                        cell.grid_col,
                        cell.is_leaf,
                        cell.n_chunks,
                        _vec_literal(cell.centroid),
                    ),
                )


def _assign_all(
    store, run_id: int, cfg: ScopeCfg, params, cells, descend_to_leaf
) -> None:
    """Stream every candidate chunk, descend to its leaf, COPY the row."""
    import numpy as np

    from precis.utils.cluster_map import l2_normalize

    read_conn = store.pool.getconn()
    write_conn = store.pool.getconn()
    try:
        with (
            write_conn.cursor() as wcur,
            wcur.copy(
                "COPY cluster_assignments (run_id, chunk_id, ref_id, leaf_path) "
                "FROM STDIN"
            ) as copy,
        ):
            with read_conn.cursor(name="clusterize_stream") as rcur:
                rcur.itersize = _VECTOR_BATCH
                rcur.execute(
                    "SELECT c.chunk_id, c.ref_id, ce.vector::text " + _CANDIDATE_WHERE,
                    params,
                )
                batch: list[tuple[int, int]] = []
                vecs: list[list[float]] = []

                def flush() -> None:
                    if not batch:
                        return
                    mat = l2_normalize(np.array(vecs, dtype=np.float32))
                    leaves = descend_to_leaf(mat, cells, grid=cfg.grid)
                    for (chunk_id, ref_id), leaf in zip(batch, leaves, strict=True):
                        if leaf is None:
                            continue
                        copy.write_row((run_id, chunk_id, ref_id, leaf))
                    batch.clear()
                    vecs.clear()

                for chunk_id, ref_id, vtext in rcur:
                    batch.append((chunk_id, ref_id))
                    vecs.append(_parse_vec(vtext))
                    if len(batch) >= _VECTOR_BATCH:
                        flush()
                flush()
        write_conn.commit()
        read_conn.commit()
    finally:
        store.pool.putconn(read_conn)
        store.pool.putconn(write_conn)


def _compute_word_clouds(
    store, run_id: int, cells, rollup_histograms, ctfidf_words
) -> None:
    """Pull per-leaf keyword histograms + counts, roll up, c-TF-IDF,
    write back ``words`` / ``n_chunks`` / ``n_refs`` per cell."""
    with store.pool.connection() as conn:
        hist_rows = conn.execute(
            """
            WITH kw AS (
                SELECT a.leaf_path AS p, lower(t.kw) AS kw, count(*) AS n
                  FROM cluster_assignments a
                  JOIN chunks c ON c.chunk_id = a.chunk_id
                  CROSS JOIN LATERAL unnest(c.keywords) AS t(kw)
                 WHERE a.run_id = %s
                 GROUP BY a.leaf_path, lower(t.kw)
            ), ranked AS (
                SELECT p, kw, n,
                       row_number() OVER (PARTITION BY p ORDER BY n DESC) AS rk
                  FROM kw
            )
            SELECT p, kw, n FROM ranked WHERE rk <= %s
            """,
            (run_id, _TOP_TERMS_PER_LEAF),
        ).fetchall()
        count_rows = conn.execute(
            """
            SELECT leaf_path, count(*), count(DISTINCT ref_id)
              FROM cluster_assignments
             WHERE run_id = %s
             GROUP BY leaf_path
            """,
            (run_id,),
        ).fetchall()

    leaf_hist: dict[str, dict[str, int]] = {}
    for path, kw, n in hist_rows:
        leaf_hist.setdefault(path, {})[kw] = int(n)

    # Roll leaf counts up to ancestors. n_chunks is exact (each chunk in
    # one leaf); n_refs is leaf-exact, ancestor-approximate (a ref may
    # span sibling leaves) — the web view recomputes exact on drill-in.
    from precis.utils.cluster_map import ancestors

    roll_chunks: Counter[str] = Counter()
    roll_refs: Counter[str] = Counter()
    for leaf_path, n_chunks, n_refs in count_rows:
        for anc in ancestors(leaf_path):
            roll_chunks[anc] += int(n_chunks)
            roll_refs[anc] += int(n_refs)

    hist_by_path = rollup_histograms(leaf_hist)
    words_by_path = ctfidf_words(hist_by_path, cells)

    with store.pool.connection() as conn:
        with conn.cursor() as cur:
            for cell in cells:
                words = [
                    {"w": w, "s": round(float(s), 5)}
                    for w, s in words_by_path.get(cell.path, [])
                ]
                cur.execute(
                    "UPDATE cluster_cells "
                    "SET words=%s, n_chunks=%s, n_refs=%s "
                    "WHERE run_id=%s AND path=%s",
                    (
                        Jsonb(words),
                        roll_chunks.get(cell.path, cell.n_chunks),
                        roll_refs.get(cell.path, 0),
                        run_id,
                        cell.path,
                    ),
                )


def _finish_run(
    store, run_id: int, scope: str, *, n_vectors: int = 0, note: str | None = None
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE cluster_runs "
            "SET status='ok', finished_at=now(), n_vectors=%s, note=%s "
            "WHERE run_id=%s",
            (n_vectors, note, run_id),
        )
        # Prune *superseded green* runs for this scope (cascades to
        # cells + assignments). The ``status='ok'`` guard is the load-
        # bearing one: clusterize runs on every cluster node, so several
        # hosts rebuild the same scope concurrently. A still-'building'
        # run — at ANY run_id, lower or higher — is a peer's in-flight
        # build; deleting it mid-COPY violates cluster_assignments'
        # run_id FK (the observed ForeignKeyViolation). The earlier
        # ``run_id < %s`` guard was insufficient: a *lower* id can be an
        # earlier-started-but-slower build (exactly the paper scope,
        # ~50k vectors), which a faster higher-id run would still reap.
        # Only ever delete runs that have themselves finished green; the
        # newest green wins (the current-map query orders by finished_at
        # DESC), and any older green is reaped by the next finish.
        conn.execute(
            "DELETE FROM cluster_runs WHERE scope=%s AND run_id < %s AND status='ok'",
            (scope, run_id),
        )
