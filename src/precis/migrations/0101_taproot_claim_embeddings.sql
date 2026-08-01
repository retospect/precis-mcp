-- 0101_taproot_claim_embeddings.sql
--
-- The claim-embedding index for the taproot chase *trigger* (plan
-- transient-napping-parrot, Phase 1). Taproot claim hubs are few (~1.2k
-- findings tagged TAPROOT:claim / STATUS:canonical) vs paper chunks (>1M),
-- so we index the claims and probe THAT index per new chunk — the "reverse
-- ANN" inversion that lets a freshly-embedded corroborator mark its
-- affected claims due, instead of hub_refine blind-rescanning every hub on
-- a weekly cadence (workers/hub_refine.py::_claim_hubs_due_for_refine, whose
-- own docstring calls its interval "scheduling state, not a corpus-change
-- watermark").
--
-- Shape mirrors `tag_embeddings` (the other "embed a non-chunk entity"
-- table): one vector per (entity, embedder), the embedder recorded so a
-- corpus re-embed can tell stale vectors from current ones, plus the
-- source-content hash so the trigger pass can re-embed only claims whose
-- sentence actually changed (claim edit → new sha → re-embed → the new
-- wording drives future matches).
--
--   * vector(1024) — same dim as chunk_embeddings / tag_embeddings (bge-m3),
--     so a chunk vector and a claim vector are directly comparable under
--     pgvector's `<=>` cosine distance.
--   * claim_sha — hash of the claim sentence (finding.title) at embed time;
--     the trigger pass upserts when the live sha differs.
--   * NO ANN index (HNSW/IVFFlat): the table is ~1.2k rows, so the
--     per-chunk probe is a trivial flat scan; an ANN index would only add
--     write-amplification for no read win at this size. Revisit if claim
--     count reaches ~10^5.
--
-- The rest of the trigger substrate is tags, not schema: a due claim
-- carries a closed `TAPROOT_DUE` ref tag (popped by hub_refine when it
-- claims the hub); a trigger-swept chunk carries a closed `CHASETRIG:<ver>`
-- chunk tag (the classify/classify_topics marker idiom) so the sweep
-- converges and never re-probes a chunk. Both live in the existing
-- tags / ref_tags / chunk_tags tables — nothing to add here.
--
-- Forward-only (ADR 0005). Idempotent (`CREATE TABLE IF NOT EXISTS`).

BEGIN;

CREATE TABLE IF NOT EXISTS public.claim_embeddings (
    claim_ref_id bigint NOT NULL
        REFERENCES public.refs (ref_id) ON DELETE CASCADE,
    embedder     text   NOT NULL,
    claim_sha    text   NOT NULL,
    vector       public.vector(1024),
    embedded_at  timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (claim_ref_id, embedder)
);

COMMENT ON TABLE public.claim_embeddings IS
    'Embedding index over taproot claim hubs (findings tagged TAPROOT:claim). '
    'Probed per new chunk by the chase_trigger pass to mark affected claims '
    'due (TAPROOT_DUE tag). One vector per (claim, embedder); claim_sha gates '
    're-embed on claim edit. See migration 0101 / workers/chase_trigger.py.';

COMMIT;

-- End of 0101_taproot_claim_embeddings.sql
