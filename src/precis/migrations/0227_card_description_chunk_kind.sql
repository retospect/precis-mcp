-- 0227_card_description_chunk_kind.sql
--
-- Register `chunk_kind='card_description'`: an LLM-generated narrative
-- description of a non-text ref (structure first; cad/pcb/figure follow
-- in gr497688 slices S2/S3), written by the card_describe worker and
-- embedded by the embed worker so hybrid search lands on function and
-- intent, not only the template card_combined facts. One chunk per ref
-- at ord=-2 (chunks (ref_id, ord) is UNIQUE; card_combined owns -1,
-- card_glossary -1000 — see ChunkStore._CARD_KIND_ORD), replaced by
-- DELETE+INSERT on edit — never UPDATEd.
-- is_card = TRUE keeps the kind at ord < 0 per the chunks_check
-- constraint.
--
-- The row's created_at doubles as the card_describe claim's watermark:
-- the worker only describes cards minted after this row lands, so the
-- fleet pass never sweeps the pre-existing structure corpus at Sonnet
-- cost (backfill is a separate Devin-agent path — gr497688 ruling 1).
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('card_description', TRUE,
     'LLM-generated narrative description of a non-text ref (one per ref)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0227_card_description_chunk_kind.sql
