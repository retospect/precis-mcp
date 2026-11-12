-- 0311_card_gen_abstract_chunk_kind.sql
--
-- gr635825: register `chunk_kind='card_gen_abstract'`, a LABELLED
-- generated abstract for a paper with no publisher abstract on file,
-- written by the gen_abstract worker from the paper's own body prose
-- (aims, method, key results with numbers; never from bylines). It is a
-- separate chunk from `card_abstract` on purpose: the published abstract
-- stays distinguishable, view='abstract' and the card name the generated
-- text as generated, and the chunk is deleted when a publisher abstract
-- is later filled (ingest.cards.ensure_abstract_card).
--
-- One chunk per ref at ord=-1002 (ChunkStore._CARD_KIND_ORD), replaced by
-- DELETE+INSERT, never UPDATEd. is_card = TRUE keeps it at ord < 0 per
-- the chunks_check constraint.
--
-- Forward-only (ADR 0005). Idempotent.

BEGIN;

INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('card_gen_abstract', TRUE,
     'LLM-generated abstract-style summary of a paper with no publisher abstract (one per ref, labelled generated)')
ON CONFLICT (slug) DO NOTHING;

COMMIT;

-- End of 0311_card_gen_abstract_chunk_kind.sql
