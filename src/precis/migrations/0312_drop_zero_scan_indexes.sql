-- 0312_drop_zero_scan_indexes.sql
--
-- gr575086: drop ten secondary indexes at idx_scan=0 since the
-- 2026-10-22 17:02 UTC stats reset (~15 days) — ~660 MB maintained on
-- every write with no reads. Per docs/conventions/invariants.md each
-- drop was re-checked against every query on its columns, including
-- the foreign keys whose RI probes could use the index and the
-- periodic jobs (GC sweeps, maintenance purges) a stats window hides:
--
-- * chunk_embeddings_vec_hnsw_{draft,quest,tex}_idx (441 + 98 + 49
--   MB): three of the 0226 per-kind partial HNSW indexes, built by
--   `precis backfill-embedding-kinds`, not by a migration. The only
--   reader is the router's "partial" route
--   (store/_chunks_ops.py::_ann_route); a missing or unready partial
--   already falls back to the global index + post-filter — the
--   designed degrade — so no query changes shape. The kinds stay in
--   _ANN_PARTIAL_KINDS; the new _ANN_PARTIAL_INDEX_KINDS
--   (_ANN_PARTIAL_KINDS minus these three) is what the backfill builds
--   and `precis post-migration-check` expects, so the recorded
--   post-step does not rebuild them.
-- * parts_params_idx (61 MB): a whole-column btree on parts.params
--   that exists only on prod (never in a migration — an early
--   auto-named CREATE INDEX; IF EXISTS makes this a no-op where it is
--   absent). Every params predicate is a ->>/-> expression
--   (params->>'_category' ILIKE, params->'_lifecycle'->>'no_buy'),
--   which a whole-column index cannot serve; containment reads belong
--   to parts_params_gin (kept).
-- * paper_bib_entries_openalex_id_idx (25 MB): built by 0241 for the
--   slice-3 stub trickle's "unheld OpenAlex targets by cite count"
--   census — that GROUP BY never materialized; every access is
--   ref_id-keyed (_paper_entries, _write_oa_outcome) or a plain column
--   read.
-- * paper_authors_fullname_trgm_idx (17 MB): built by 0168 for
--   find_papers_by_author's similarity/ILIKE legs — but that predicate
--   ORs three name forms (full_name, concat_ws(family, given),
--   name_raw), two of which carry no index, so the planner can never
--   drive the whole OR from the GIN and never picks it. The query
--   keeps its current plans.
-- * paper_authors_openalex_idx / paper_authors_orcid_idx (9.6 + 9 MB):
--   no query filters or joins on openalex_author_id or orcid — both
--   are write-only provenance columns read via ref_id.
-- * struct_bonds_ref_j_idx (7.8 MB): (ref_id, j) WHERE retired_version
--   IS NULL, from 0042. No SQL predicates on j (bond endpoints resolve
--   in Python); ref_id lookups use struct_bonds_ref_id_fk_idx (kept).
--   It cannot serve the j-fkey RI probe anyway — partial, and j is not
--   leading.
-- * struct_bonds_j_idx (6.7 MB): the 0136 FK-covering index for
--   struct_bonds_j_fkey (j -> struct_atoms.id). Its RI probe is
--   dormant: nothing DELETEs struct_atoms or UPDATEs its pkey id —
--   atoms retire via retired_version. The only atom deletes are
--   CASCADEs off the rare `DELETE FROM refs` paths (cli/maintenance.py
--   tombstone purge — _NO_MEASURES-gated — agentlog trace deletes,
--   _cache_ops cached-ref replacement), and none fired the probe in
--   the window. If a structure ref is ever purged the probe seq-scans
--   a ~10 MB child: bounded, not 0200's GB-scale stall.
--   tests/test_schema_design.py grandfathers the now-uncovered FK.
--
-- KEPT (the gripe's own exclusions): struct_atoms_ref_label_key and
-- chunk_citations_pkey — both constraint-backing. Also kept:
-- struct_bonds_i_idx (scans >0), parts_params_gin, and the surviving
-- per-kind HNSW partials.
--
-- Forward-only (ADR 0005). Idempotent (IF EXISTS). Drops run in the
-- migration transaction — CONCURRENTLY cannot (same shape as 0078,
-- 0118, 0200).

DROP INDEX IF EXISTS public.chunk_embeddings_vec_hnsw_draft_idx;
DROP INDEX IF EXISTS public.chunk_embeddings_vec_hnsw_quest_idx;
DROP INDEX IF EXISTS public.chunk_embeddings_vec_hnsw_tex_idx;
DROP INDEX IF EXISTS public.parts_params_idx;
DROP INDEX IF EXISTS public.paper_bib_entries_openalex_id_idx;
DROP INDEX IF EXISTS public.paper_authors_fullname_trgm_idx;
DROP INDEX IF EXISTS public.paper_authors_openalex_idx;
DROP INDEX IF EXISTS public.paper_authors_orcid_idx;
DROP INDEX IF EXISTS public.struct_bonds_ref_j_idx;
DROP INDEX IF EXISTS public.struct_bonds_j_idx;

-- End of 0312_drop_zero_scan_indexes.sql
