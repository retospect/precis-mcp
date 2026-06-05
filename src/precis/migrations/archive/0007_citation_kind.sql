-- ============================================================================
-- precis-mcp — citation kind (verifier-workflow scaffold)
-- ============================================================================
-- Adds a ``citation`` ref kind so a writing thread can persist
-- verified claim→source mappings. Each citation row carries:
--
--   refs.title   — short claim summary (truncated)
--   refs.meta    — JSONB: {claim, source_handle, source_quote,
--                  char_offset, verifier_confidence, verifier_caveats,
--                  verified_at}
--   links        — one ``cites`` link from the citation to the source
--                  paper (already in the ``relations`` vocab)
--
-- This is the scaffold the citation workflow (2026-05-31 design
-- discussion) writes through. The verifier subagent itself is
-- workflow-side, not server-side — this migration only opens the
-- storage door.
-- ============================================================================

INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('citation', TRUE, 'Citation',
     'Verified claim → source pointer. Written by the citation-fill workflow '
     'after the verifier confirms the source quote supports the claim.');
