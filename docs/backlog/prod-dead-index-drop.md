# Drop 4 dead prod indexes (~127 MB, zero lifetime scans)

`llm_call_log_request_hash_idx` (54 MB), `vault_events_name_at_idx` (31 MB),
`chunks_section_path_idx` (28 MB), `chunks_numerics_idx` (14 MB) — idx_scan=0
for the DB's lifetime (stats never reset). Verify before dropping: find every
query touching each column (a planner-unusable predicate explains
live-but-unused); request_hash smells like a dedup lookup whose read path
moved to `llm_blob` (migs 0077/0078 era). One forward migration for the
confirmed-dead. Decided keeps: `chunks_keywords_gin` (backs verbatim mode),
`tag_embeddings_vector_hnsw` (below ANN planner threshold); PK indexes
excluded. Owner: new forward migration under `src/precis/migrations/`.

test: migration applies; the queries each index served still EXPLAIN sanely.
