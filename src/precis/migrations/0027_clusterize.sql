-- 0027_clusterize.sql — hierarchical SOM cluster maps over chunk embeddings.
--
-- Backs the `clusterize` worker pass (precis.workers.clusterize) and the
-- precis-web `/clusters` grid. A *run* is one daily rebuild for one scope
-- ('paper' | 'memory'); it owns a tree of *cells* (the SOM grid tiles, one
-- row per node incl. empty tiles so the grid is always full) and a per-chunk
-- *assignment* to a leaf cell.
--
--   cluster_runs        one row per (scope, rebuild). status flips
--                       building -> ok|failed. The current map for a scope
--                       is the newest status='ok' row.
--   cluster_cells       (run_id, path) tree. `path` is dot-joined cell
--                       indices, e.g. '4.7.1'. `centroid` is the learned
--                       SOM weight (full-corpus descent + day-to-day warm
--                       start); `words` is the precomputed c-TF-IDF cloud.
--   cluster_assignments (run_id, chunk_id) -> leaf_path. Members of any
--                       ancestor cell are a `leaf_path LIKE 'prefix%'`
--                       prefix scan (varchar_pattern_ops index).
--
-- Old runs are pruned by the worker after a green rebuild; ON DELETE CASCADE
-- drops their cells + assignments. `grid_row`/`grid_col` (not `row`/`col`)
-- avoid the SQL reserved word `row`.
--
-- Forward-only (ADR 0005). Idempotent (IF NOT EXISTS throughout).

BEGIN;

CREATE TABLE IF NOT EXISTS cluster_runs (
    run_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope       TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'building',
    params      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    n_vectors   INTEGER     NOT NULL DEFAULT 0,
    note        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS cluster_runs_current_idx
    ON cluster_runs (scope, finished_at DESC)
    WHERE status = 'ok';

CREATE TABLE IF NOT EXISTS cluster_cells (
    run_id      BIGINT  NOT NULL REFERENCES cluster_runs(run_id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    parent_path TEXT,
    depth       INTEGER NOT NULL,
    grid_row    INTEGER NOT NULL,
    grid_col    INTEGER NOT NULL,
    is_leaf     BOOLEAN NOT NULL DEFAULT true,
    n_chunks    INTEGER NOT NULL DEFAULT 0,
    n_refs      INTEGER NOT NULL DEFAULT 0,
    words       JSONB   NOT NULL DEFAULT '[]'::jsonb,
    centroid    vector(1024),
    PRIMARY KEY (run_id, path)
);

CREATE INDEX IF NOT EXISTS cluster_cells_parent_idx
    ON cluster_cells (run_id, parent_path);

CREATE TABLE IF NOT EXISTS cluster_assignments (
    run_id    BIGINT NOT NULL REFERENCES cluster_runs(run_id) ON DELETE CASCADE,
    chunk_id  BIGINT NOT NULL,
    ref_id    BIGINT NOT NULL,
    leaf_path TEXT   NOT NULL,
    PRIMARY KEY (run_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS cluster_assignments_leaf_idx
    ON cluster_assignments (run_id, leaf_path varchar_pattern_ops);

CREATE INDEX IF NOT EXISTS cluster_assignments_ref_idx
    ON cluster_assignments (run_id, ref_id);

COMMIT;

-- End of 0027_clusterize.sql
