-- 0018: rename three kinds to honest, self-documenting names.
--
--   fc        → flashcard            (drop the abbreviation; sense='flashcard'
--                                     already, README has to call out the
--                                     collision, TOOL_HINTS calls it 'flashcard'
--                                     and would error against the old name)
--   think     → perplexity-reasoning (provider-rooted; the answer IS Perplexity
--                                     sonar-reasoning-pro, cost+latency+depth
--                                     are coupled to the provider, naming
--                                     should admit it)
--   research  → perplexity-research  (same — sonar-deep-research, expensive
--                                     and pinned-forever)
--
-- chunk_kind 'fc_claim' / 'fc_evidence' rename in sync so the metadata stays
-- consistent.
--
-- Hard cutover: no alias period. Codebase is small and the discovery surface
-- is the skill index (loaded at boot from data/skills/precis-*-help.md), not
-- a stable API contract.

BEGIN;

-- ── kinds table rows ─────────────────────────────────────────────────

UPDATE kinds SET slug = 'flashcard' WHERE slug = 'fc';
UPDATE kinds SET slug = 'perplexity-reasoning' WHERE slug = 'think';
UPDATE kinds SET slug = 'perplexity-research' WHERE slug = 'research';

-- ── chunk_kinds vocabulary ───────────────────────────────────────────

UPDATE chunk_kinds SET slug = 'flashcard_claim' WHERE slug = 'fc_claim';
UPDATE chunk_kinds SET slug = 'flashcard_evidence' WHERE slug = 'fc_evidence';

-- ── refs.kind values ─────────────────────────────────────────────────

UPDATE refs SET kind = 'flashcard' WHERE kind = 'fc';
UPDATE refs SET kind = 'perplexity-reasoning' WHERE kind = 'think';
UPDATE refs SET kind = 'perplexity-research' WHERE kind = 'research';

-- ── chunks.meta->>'chunk_kind' values ────────────────────────────────
-- Chunk kind is stored in the JSONB meta envelope rather than as a typed
-- column, so the rename has to walk the JSONB. Cheap on the realistic
-- corpus size; one pass per renamed kind.

UPDATE chunks
   SET meta = jsonb_set(meta, '{chunk_kind}', '"flashcard_claim"')
 WHERE meta->>'chunk_kind' = 'fc_claim';

UPDATE chunks
   SET meta = jsonb_set(meta, '{chunk_kind}', '"flashcard_evidence"')
 WHERE meta->>'chunk_kind' = 'fc_evidence';

COMMIT;
