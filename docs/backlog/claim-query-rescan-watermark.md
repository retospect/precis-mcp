# Chunk-claim candidate query — stop rescanning the done set

The `chunk_claims`-ledger claim query (classify/axis/llm_summarize shape:
candidate scan over `chunks` with `NOT EXISTS chunk_claims` + tag EXISTS,
`ORDER BY c.chunk_id LIMIT n FOR UPDATE SKIP LOCKED`) is prod's #1
pg_stat_statements entry: mean ~17.5 s/call, ~7.4 h DB time per 6 days.
Cause: every tick re-filters the ~2.4M already-claimed chunks from
`chunk_id` 0 before reaching fresh candidates — cost grows linearly with
the done ledger forever. Fix: persist a per-artifact high-water mark
(`app_state`: max claimed `chunk_id`; new/re-chunked rows get higher ids,
so `c.chunk_id > watermark` is safe with a periodic slow full sweep as
backstop), or a covering partial-index rethink if the watermark's
append-only assumption doesn't hold. Owner `src/precis/workers/classify.py`
(`_claim`), `axis_pass.py`, `llm_summarize.py`. Perf; needs a short design
note before code.
