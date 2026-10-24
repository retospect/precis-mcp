-- 0242_chunk_embeddings_kind_ready.sql
--
-- Per-kind fill markers for the kind-scoped ANN router (gr513663, option
-- (a)): let a kind use its partial HNSW index as soon as ITS rows are
-- filled, instead of waiting for the table-wide
-- ``chunk_embeddings_ref_kind_filled`` CHECK to validate (which needs the
-- 3.41M-row paper fill — 20–45 h — before ANY kind routes).
--
-- A kind is "filled" when no ``chunk_embeddings`` row with
-- ``ref_id IS NULL OR kind IS NULL`` resolves to it through
-- chunks→refs. Probing that per query needs the two-join scan the router
-- cannot afford, so the answer is materialised here:
-- ``precis backfill-embedding-kinds`` upserts one row per kind of
-- ``_ANN_PARTIAL_KINDS`` whose fill it has verified complete, and
-- ``store/_chunks_ops.py::_ann_kind_state`` reads the table (a handful of
-- rows, cached 60 s) next to the pg_index probe — guarded by
-- ``has_table_privilege`` so a search role without SELECT falls back to
-- the CHECK gate instead of erroring (grants are provisioned out of
-- tree). Rows are never deleted. The copy is *almost* monotone — the 0226
-- trigger fills on every write and the composite FK cascades keep a
-- filled row filled — except a ref re-kinded while it still carries
-- unfilled rows: a NULL row resolves through the ref's CURRENT kind at
-- fill time, so the re-kind hands its stragglers to the new kind and
-- leaves that kind's marker stale (tracked as a follow-up; re-running
-- the backfill does not clear stale markers). A row that can never
-- resolve to a ref is invisible to every route anyway. The validated
-- CHECK still short-circuits all of this — it means every kind is
-- filled, so the marker table is only the pre-validation gate.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound
-- the DDL wait behind a long reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS chunk_embeddings_kind_ready (
    kind      text PRIMARY KEY,
    marked_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE chunk_embeddings_kind_ready IS
    'Kinds the 0226 backfill verified fill-complete (no NULL ref_id/kind '
    'row resolving to them through chunks->refs). Written by '
    'backfill-embedding-kinds; read by the ANN router '
    '(store/_chunks_ops.py::_ann_kind_state) as the per-kind gate for the '
    'partial HNSW indexes until chunk_embeddings_ref_kind_filled validates.';

COMMIT;

-- End of 0242_chunk_embeddings_kind_ready.sql
