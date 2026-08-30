-- 0144_claim_embeddings_hub_ref_id.sql
--
-- Vocabulary-compaction Stage C (docs/backlog/vocab-compaction-stages.md):
-- `claim_embeddings.claim_ref_id` -> `hub_ref_id`. Migration 0101's own
-- table COMMENT already says the rows are an embedding index "over taproot
-- claim hubs" — the column just hadn't caught up to the name the concept
-- goes by everywhere else (`hub_ref_id` is what `nanopub/mint.py` and
-- `handlers/_finding_nanopub.py` already convert `PublishRow.claim_ref_id`
-- INTO when they hand it onward). Scoped to THIS table only:
-- `nanopub_publish.claim_ref_id` / `nanopub_artifacts.claim_ref_id` are a
-- separate column on a separate table and are untouched here.
--
-- Ships with the code that reads/writes it (`workers/chase_trigger.py`)
-- behind a fleet quiesce — no old binary reads `claim_ref_id` on this table
-- after this lands, so a plain rename is safe.
--
-- Forward-only (ADR 0005). The DO-block guards make a repeat run (or a
-- fresh baseline already carrying `hub_ref_id`) a no-op — bare RENAMEs are
-- not naturally idempotent.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'claim_embeddings'
          AND column_name = 'claim_ref_id'
    ) THEN
        ALTER TABLE public.claim_embeddings RENAME COLUMN claim_ref_id TO hub_ref_id;
    END IF;
END $$;

-- Column rename leaves the FK constraint's own name stale (Postgres doesn't
-- rename constraints for you); tidy it so \d and error messages read true.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'claim_embeddings_claim_ref_id_fkey'
          AND conrelid = 'public.claim_embeddings'::regclass
    ) THEN
        ALTER TABLE public.claim_embeddings
            RENAME CONSTRAINT claim_embeddings_claim_ref_id_fkey TO claim_embeddings_hub_ref_id_fkey;
    END IF;
END $$;

COMMENT ON TABLE public.claim_embeddings IS
    'Embedding index over taproot claim hubs (findings tagged TAPROOT:claim). '
    'Probed per new chunk by the chase_trigger pass to mark affected claims '
    'due (TAPROOT_DUE tag). One vector per (hub, embedder); claim_sha gates '
    're-embed on claim edit. See migration 0101/0144 / workers/chase_trigger.py.';

COMMIT;

-- End of 0144_claim_embeddings_hub_ref_id.sql
