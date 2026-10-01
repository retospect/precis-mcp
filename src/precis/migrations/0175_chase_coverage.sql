-- 0175_chase_coverage.sql
--
-- The taproot chase **coverage ledger** (plan transient-napping-parrot,
-- Phase 1b). Closes the trimmed-A matcher-recall gap between the trigger
-- and the refiner: ``workers/chase_trigger.py`` marks a claim hub
-- ``TAPROOT_DUE`` when a freshly-embedded chunk lands within its cosine
-- floor (``PRECIS_TAPROOT_TRIGGER_MIN_SIM``, default 0.45), but
-- ``workers/hub_refine.py`` then ran its OWN forward-ANN discovery
-- (``PRECIS_TAPROOT_REFINE_TOPK``, default 8, no distance floor). If the
-- specific chunk that triggered the due-mark didn't rank in that top-8
-- (a densely-covered claim), the due tag was popped with the corroborator
-- never verified — a silent drop until a title edit or the 90-day backstop.
--
-- The fix records the EXACT triggering ``chunk_id`` here at trigger time;
-- ``hub_refine`` then verifies precisely those recorded chunks (never a
-- lossy re-ANN) and deletes the row once it has judged the chunk. A hub
-- with any live coverage row is itself due (see ``_is_hub_due``), so the
-- signal is durable: the row is the retry watermark, drained only when the
-- chunk has actually been looked at — not popped ahead of discovery like
-- the ``TAPROOT_DUE`` tag it complements.
--
--   * (hub_ref_id, chunk_id) PRIMARY KEY — one row per (claim, triggering
--     chunk); a chunk that re-triggers the same hub is idempotent
--     (chase_trigger inserts ON CONFLICT DO NOTHING).
--   * embedder — recorded so hub_refine verifies under the same embedder
--     the match was computed against (mirrors claim_embeddings, migration
--     0101).
--   * ON DELETE CASCADE on both FKs — a retired/deleted hub or chunk drops
--     its ledger rows automatically; there is no separate GC pass.
--   * NO separate hub_ref_id index: the PK (hub_ref_id, chunk_id) already
--     serves every hub_ref_id lookup (hub_refine reads all rows per hub).
--   * NO ANN index: this is a small per-hub keyed lookup, not a vector
--     scan (the vectors live in chunk_embeddings / claim_embeddings).
--
-- Forward-only (ADR 0005). Idempotent (``CREATE TABLE IF NOT EXISTS``).

BEGIN;

CREATE TABLE IF NOT EXISTS public.chase_coverage (
    hub_ref_id   bigint NOT NULL
        REFERENCES public.refs (ref_id) ON DELETE CASCADE,
    chunk_id     bigint NOT NULL
        REFERENCES public.chunks (chunk_id) ON DELETE CASCADE,
    embedder     text   NOT NULL,
    triggered_at timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (hub_ref_id, chunk_id)
);

-- The chunk FK cascades, and body chunks are replaced by DELETE + INSERT, so
-- every chunk delete looks this table up by chunk_id; the PK leads with
-- hub_ref_id and cannot serve that.
CREATE INDEX IF NOT EXISTS chase_coverage_chunk_id_idx
    ON public.chase_coverage (chunk_id);

COMMENT ON TABLE public.chase_coverage IS
    'Coverage ledger for the taproot chase trigger (plan '
    'transient-napping-parrot, Phase 1b). One row per (claim hub, '
    'triggering chunk) recorded by chase_trigger at due-mark time; '
    'hub_refine verifies exactly these chunks then deletes the row. '
    'A hub with any live row is due. See migration 0175 / '
    'workers/chase_trigger.py / workers/hub_refine.py.';

COMMIT;

-- End of 0175_chase_coverage.sql
