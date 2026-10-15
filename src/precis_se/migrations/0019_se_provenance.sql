-- precis_se/0019_se_provenance.sql
--
-- Per-number provenance for se (docs/backlog/design-state-core.md item 4):
-- the shared design core's sidecar convention
-- ({param: {source, fidelity?, solver_id?, assumptions, library_version?,
-- margin_origin?}}, precis.design.provenance), stored beside the numbers it
-- describes.
--
-- * se_blocks.provenance — a sidecar keyed by block facet ('pose',
--   'envelope', 'geometry'). Written by a solver that sets the facet
--   (formfind → 'pose', realize(strategy='simp') → 'geometry'), by an op
--   carrying an explicit provenance= payload, and replaced by the stated
--   or assumed entry when an author overwrites a facet a solver had
--   claimed, so a stale solver claim never outlives the number.
-- * se_measures.provenance — ONE entry for the measure's own number (the
--   measure is the param), where a margin rule records margin_origin.
--
-- Absent = no claim, never a default: a NULL/empty sidecar reads as
-- "nobody said", which the margin audit and the fidelity ladder skip.
-- Additive and nullable; no data migration.

ALTER TABLE se_blocks ADD COLUMN IF NOT EXISTS provenance jsonb;
ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS provenance jsonb;

COMMENT ON COLUMN se_blocks.provenance IS
    'Per-facet provenance sidecar {facet: entry} in precis.design.provenance '
    'shape (source, fidelity, solver_id, assumptions, library_version, '
    'margin_origin). Facets: pose, envelope, geometry. NULL = no claim.';

COMMENT ON COLUMN se_measures.provenance IS
    'Provenance entry for this measure''s number, precis.design.provenance '
    'shape; margin_origin set = the number carries a margin (the margin '
    'audit). NULL = no claim.';

-- The margin audit is a jsonb probe over these two columns; a partial
-- index keeps it cheap once designs accumulate.
CREATE INDEX IF NOT EXISTS se_measures_margin_origin_idx
    ON se_measures (ref_id)
    WHERE retired_at IS NULL AND provenance ? 'margin_origin';
