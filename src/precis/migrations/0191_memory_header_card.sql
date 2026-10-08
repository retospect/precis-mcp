-- 0191_memory_header_card.sql
--
-- Make a memory's title and hook searchable. Search only ever matched the
-- `memory_body` chunk (ord = 0); `refs.title` and `refs.meta->>'hook'` were
-- invisible to both the lexical and the embedding leg. Every memory now
-- carries a `card_combined` chunk at ord = -1 holding `title || E'\n' ||
-- hook` (the Python writer is `ChunksOps.sync_header_card`; both use the
-- same format: parts trimmed, empty parts skipped). The card is NOT the
-- body, so nothing is embedded twice (cf. 0050, which dropped the old
-- body-text card for exactly that reason). `MemoryHandler.search_card_kinds`
-- opts the card into search.
--
-- Insert-only and idempotent: refs that already have an ord = -1 chunk, are
-- retired, or have both title and hook empty are skipped. Embeddings stay
-- absent; the embed worker fills them like any card.

INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
SELECT r.ref_id, -1, 'card_combined',
       concat_ws(E'\n',
                 NULLIF(btrim(COALESCE(r.title, '')), ''),
                 NULLIF(btrim(COALESCE(r.meta ->> 'hook', '')), '')),
       '{}'::jsonb
  FROM refs r
 WHERE r.kind = 'memory'
   AND r.retired_at IS NULL
   AND concat_ws(E'\n',
                 NULLIF(btrim(COALESCE(r.title, '')), ''),
                 NULLIF(btrim(COALESCE(r.meta ->> 'hook', '')), '')) <> ''
   AND NOT EXISTS (SELECT 1 FROM chunks c
                    WHERE c.ref_id = r.ref_id AND c.ord = -1);
