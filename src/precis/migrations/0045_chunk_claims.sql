-- 0045_chunk_claims.sql
--
-- Shared lease table for the per-chunk backfill workers (LLM summary,
-- rake-lemma, KeyBERT keywords, embeddings).
--
-- The workers used to claim a batch with `SELECT ... FOR UPDATE OF c SKIP
-- LOCKED` and hold that row lock — and the whole transaction, hence the xmin
-- horizon — open across the slow per-chunk work (LLM call / transformer
-- forward pass). That long transaction is what starved autovacuum on the hot
-- tables (they grew 150x while their stats/dead-tuples went unmaintained).
--
-- The lease decouples claiming from processing. A claim INSERTs a
-- `chunk_claims` row in a short transaction and COMMITs immediately (releasing
-- the lock); the slow work runs with no open transaction; on completion the
-- result is written to its artifact table and the claim row is DELETEd, all in
-- a second short transaction. A crashed/stalled worker leaves its claim row;
-- once `claimed_at` ages past the worker's cooldown the chunk is re-claimed
-- (oldest-first) — the cooldown is the reaper, no separate process.
--
-- One small table, every kind:
--   * It is sparse BY CONSTRUCTION — it holds only rows that are in-flight or
--     still retrying. Terminal work (ok, or failed past the attempt cap) has
--     NO claim row. So the lease write/delete churn lands here, not on the
--     1.5M-row chunks/chunk_summaries/chunk_embeddings tables, and the reaper
--     scan is trivially small.
--   * `artifact` is the type discriminator (the worker's model name, e.g.
--     'llm-v1', 'rake-lemma', 'keybert', 'bge-m3'), so summary, keyword and
--     embedding leases coexist here. Storage stays per-kind (text artifacts in
--     chunk_summaries, vectors in chunk_embeddings, KeyBERT keywords on
--     chunks.keywords); only the *claim* is universal.
--   * `attempts` lives with the lease: a still-retrying failure keeps its claim
--     row (attempts incremented, claimed_at refreshed = backoff); once it hits
--     the cap the worker writes a terminal 'failed' marker to the artifact
--     table and DELETEs the claim. So `attempts` never pollutes the artifact
--     tables and the claims table self-empties.
--
-- Deliberately no FK to chunks: the claims table is ephemeral and we don't want
-- claim INSERTs taking key-share locks on chunks rows. A claim for a deleted
-- chunk is harmless — its reclaim just finds no chunk row and is skipped.
--
-- "fresh" work = chunks with NO artifact row AND NO claim row (self-healing:
-- delete a result and it becomes claimable again). Done-ness still lives with
-- the result; this table only tracks work in progress.
--
-- Forward-only (ADR 0005). Regenerate the baseline snapshot at release
-- (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS chunk_claims (
    chunk_id   bigint      NOT NULL,
    artifact   text        NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    attempts   integer     NOT NULL DEFAULT 0,
    PRIMARY KEY (chunk_id, artifact)
);

-- Reaper: per-artifact, oldest-first. The table is tiny, but this keeps the
-- reclaim an index range scan rather than a sort.
CREATE INDEX IF NOT EXISTS chunk_claims_reap_idx
    ON chunk_claims (artifact, claimed_at);

-- Heal old-model failures. Before this pass leased, a *sub-cap* failure was
-- recorded as a chunk_summaries row (status='failed', attempts < cap). Under the
-- lease model that row is terminal — never re-claimed (the fresh claim excludes
-- any summarized chunk, and there is no lease to reclaim) — which would strand
-- failures that the old code would have retried. Rather than DELETE the (already
-- computed) summary rows, seed a lease for each so the cooldown reaper brings
-- them around again "in the usual way": claimed_at = now() makes them behave
-- exactly like a just-failed live retry (eligible after one cooldown), and the
-- carried-over attempts preserves the remaining retry budget. At-cap failures
-- (attempts >= cap) are left terminal. The 3 is MAX_SUMMARIZE_ATTEMPTS in
-- workers/llm_summarize.py; only 'llm-v1' used chunk_summaries failure rows so
-- far (the other artifacts adopt the lease table fresh).
INSERT INTO chunk_claims (chunk_id, artifact, attempts, claimed_at)
SELECT cs.chunk_id, cs.summarizer, cs.attempts, now()
  FROM chunk_summaries cs
 WHERE cs.summarizer = 'llm-v1'
   AND cs.status = 'failed'
   AND cs.attempts < 3
ON CONFLICT (chunk_id, artifact) DO NOTHING;

COMMIT;

-- End of 0045_chunk_claims.sql
