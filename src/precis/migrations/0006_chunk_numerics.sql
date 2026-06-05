-- ============================================================================
-- precis-mcp — chunk numeric-token lexical index
-- ============================================================================
-- Adds a denormalized ``TEXT[]`` column to ``chunks`` carrying every
-- ``<number><unit>`` token detected at ingest by
-- ``precis.utils.numerics.extract_numerics``. GIN-indexed for cheap
-- exact-value lookups (``WHERE numerics @> ARRAY['1.523 eV']``).
--
-- Path-2 in the tables-curveball design discussion (2026-05-31):
-- structured ``paper_facts`` extraction (path-3 / task #63) lands
-- later; this column gives the agent something workable in the
-- meantime without paying any LLM cost at ingest. No schema
-- commitment beyond a single column — when path-3 ships, the facts
-- extractor reads ``numerics`` as a hint and writes proper
-- ``(value, unit)`` rows into a new table.
-- ============================================================================

ALTER TABLE chunks
    ADD COLUMN numerics TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX chunks_numerics_idx ON chunks USING GIN (numerics);
