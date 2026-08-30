-- 0149_refs_retired_at_rename.sql
--
-- Vocabulary-compaction Stage E (docs/backlog/vocab-compaction-stages.md):
-- `refs.deleted_at` -> `retired_at`, closing the retire/soft-delete split.
-- The 8 other tables that already carry a `retired_at` column (chunks,
-- cad_designs, cad_nodes, pcb_boards, ...) conform already; this is the
-- one holdout, and the widest-used soft-delete predicate in the store
-- (`Store.retire_ref`, ex-`soft_delete_ref`, and every "is this ref alive"
-- query) — hence the largest migration in the plan by call-site count, even
-- though the SQL itself is a plain rename.
--
-- Postgres rewrites dependent *predicates* (the two partial indexes
-- `refs_alive_idx` / `uq_alert_open_source_fingerprint` — neither spells
-- "deleted" in its NAME, so no index rename needed) — but NOT a dependent
-- view's exposed column name: `v_refs` selects `deleted_at` unaliased, and
-- after the rename would keep exposing a column literally named
-- `deleted_at` (backed by `retired_at`). A view's output column can't be
-- renamed via CREATE OR REPLACE, so it's dropped and recreated below with
-- the new name (review finding, 2026-08-30; the view has no dependents and
-- no application reader — it exists for ad-hoc SQL).
--
-- Ships with the code that reads/writes `refs.deleted_at` fleet-wide
-- (handlers, store ops, web routes, workers, scripts — the whole
-- `Store.retire_ref` surface) behind a fleet quiesce, per the plan's
-- Deploy protocol — no old binary reads/writes `refs.deleted_at` after
-- this lands.
--
-- Forward-only (ADR 0005). The DO-block guard makes a repeat run (or a
-- fresh baseline already carrying `retired_at`) a no-op instead of an
-- error — a bare RENAME is not naturally idempotent.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'refs'
          AND column_name = 'deleted_at'
    ) THEN
        ALTER TABLE public.refs RENAME COLUMN deleted_at TO retired_at;
    END IF;
END $$;

DROP VIEW IF EXISTS public.v_refs;

CREATE VIEW public.v_refs AS
 SELECT ref_id,
    kind,
    set_by,
    title,
    authors,
    year,
    provider,
    human_verified_at,
    human_verified_by,
    human_verified_note,
    retraction_status,
    retracted_at,
    retraction_reason,
    retraction_url,
    retraction_checked_at,
    pdf_sha256,
    pdf_pages,
    pdf_role,
    meta,
    retired_at,
    created_at,
    updated_at,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'pub_id'::text))) AS pub_id,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'cite_key'::text))) AS cite_key,
    ( SELECT ref_identifiers.id_value
           FROM public.ref_identifiers
          WHERE ((ref_identifiers.ref_id = r.ref_id) AND (ref_identifiers.id_kind = 'paper_id'::text))) AS paper_id
   FROM public.refs r;

COMMIT;

-- End of 0149_refs_retired_at_rename.sql
