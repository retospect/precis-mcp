-- 0086_chunk_review.sql
--
-- Paper-writing pipeline rung 3 (docs/design/paper-writing-pipeline.md
-- §"Review — the memoized approval ledger"): a per-(chunk, checker)
-- approval watermark keyed on the chunk's `content_sha`.
--
-- "Requires review by C" is a derived query, not a loop:
--   current chunks.content_sha ≠ the approved_sha the checker last recorded
-- (never-reviewed = no row = also requires review). A weave that edits a
-- chunk bumps its content_sha, so it goes dirty for every checker at once;
-- reviewers only ever run on dirty chunks. The human is a checker too
-- (checker='human') — a later rung's export gate reads those rows.
--
-- Same watermark shape as the classifier's `chunk_claims` lease
-- (0045_chunk_claims.sql), but persistent rather than ephemeral — modelled
-- on `chunk_summaries` (0001_initial.sql): a real FK `ON DELETE CASCADE`
-- (this ledger is a durable record, not an in-flight lease, so it's fine
-- to take the lock a CASCADE implies) and a `content_sha`-bearing sidecar
-- row per (chunk, consumer). `verdict` is free text (no CHECK) — the
-- checker set and its vocabulary (`approved`/`pass`/`changes`/`fail`/…) are
-- forward-flexible, unlike the fixed `chunk_summaries.status` enum.
--
-- Forward-only (ADR 0005). Idempotent. Regenerate the baseline snapshot at
-- release time (ADR 0031): `scripts/bump` / `precis db dump-schema`.

BEGIN;

CREATE TABLE IF NOT EXISTS chunk_review (
    chunk_id     bigint      NOT NULL
        REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    checker      text        NOT NULL,
    approved_sha text        NOT NULL,
    verdict      text        NOT NULL,
    at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, checker)
);

COMMIT;

-- End of 0086_chunk_review.sql
