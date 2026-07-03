-- 0051_summarize_hot_tier.sql
--
-- Give `llm_summarize` a salience-priority tier so a paper a human just
-- opened in the web reader gets summarised *first*, reusing the same
-- `chunks.last_seen` heat signal the dreamer and watcher already ride
-- (migrations 0007 / 0024). The reader bumps `last_seen` on open
-- (`Store.bump_salience_for_ref`), and the summarize claim gains a "hot"
-- fresh tier — `ORDER BY last_seen DESC` over recently-seen un-summarised
-- chunks — claimed after draft/conv and before the ~1M-chunk cold backlog
-- (queue order: draft > conv > hot > rest).
--
-- The hot tier's claim SQL orders by `last_seen DESC` and filters to a
-- recency window; without an index that is a seq-scan + sort over the
-- whole chunks table every pass (the exact cost the NOT-EXISTS fresh
-- claim was written to avoid). A plain descending btree on `last_seen`
-- lets the planner walk the most-recently-seen chunks and stop at LIMIT.
-- `last_seen` is NOT NULL (0007 seeded it to created_at), so no partial
-- predicate is needed. Same write-amplification class as the existing
-- `(last_seen - last_dreamt)` / `(last_seen - last_watched)` selection
-- indexes — each `bump_salience` already updates those.
--
-- No new column: unlike dream/watch, summarize never revisits a chunk it
-- has already summarised (the fresh claim's `NOT EXISTS chunk_summaries`
-- is its permanent done-marker), so it needs no per-actor rotation
-- column — only the shared `last_seen` signal.

CREATE INDEX IF NOT EXISTS chunks_last_seen_desc_idx
    ON chunks (last_seen DESC);
