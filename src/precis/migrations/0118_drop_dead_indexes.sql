-- 0118_drop_dead_indexes.sql
--
-- Drop 3 prod indexes with zero lifetime scans (~73 MB) — verified dead
-- by reading every query touching each column (docs/backlog/
-- prod-dead-index-drop.md):
--
-- * chunks_section_path_idx (28 MB): no query anywhere filters on
--   section_path; it is only ever SELECTed as a column.
-- * chunks_numerics_idx (14 MB): the `WHERE numerics @>` query side
--   promised in precis/utils/numerics.py never materialized — writers
--   fill the column, nothing reads it as a predicate.
-- * vault_events_name_at_idx (31 MB): the only reader is
--   vault.gc_events(), which deletes by `at <` alone — a (name, at)
--   index with name leading is planner-unusable for that predicate;
--   writers are insert-only audit rows. Ad-hoc forensics can seqscan
--   (or rebuild the index on demand).
--
-- KEPT deliberately: llm_call_log_request_hash_idx — its consumer is
-- route_log.gc()'s orphan-blob anti-join (added for that purpose in
-- 0077) plus the llm_call_log_request_hash_fkey RI check on llm_blob
-- deletes. idx_scan=0 only because the log's oldest row (2026-07-14)
-- is still inside the 90-day retention window, so the GC delete branch
-- has never fired. Also kept (per the backlog item's triage):
-- chunks_keywords_gin (backs verbatim mode), tag_embeddings_vector_hnsw
-- (below ANN planner threshold).

DROP INDEX IF EXISTS public.chunks_section_path_idx;
DROP INDEX IF EXISTS public.chunks_numerics_idx;
DROP INDEX IF EXISTS vault.vault_events_name_at_idx;
