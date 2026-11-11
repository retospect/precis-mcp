-- 0306_survey_measurand_fixes.sql
--
-- Two corrections to the landed 0305 seed (review td633663 of td633662,
-- fixed on td633943):
--
-- * `xrd-peak-position` aliased "Bragg angle" — but Bragg's θ is half the
--   measured 2θ peak position, so the alias paired values a factor of two
--   apart with no unit conversion to save them. The alias is removed.
-- * `double-layer-capacitance` seeded area-normalized (F/m^2) under the
--   generic name, so a literature C_dl reported in farads had no
--   measurand it could convert into. The landed node IS the areal
--   quantity — renamed `areal-double-layer-capacitance`, its generic
--   aliases moved to a new `double-layer-capacitance` minted as the
--   TOTAL capacitance (canonical F). Renaming keeps the node's ref_id
--   and its `specialises` edge, and never touches canonical_unit, so the
--   0187 unit guard cannot fire even if measures landed.
--
-- Every statement is keyed on (slug, canonical_unit) or slug + alias
-- presence — never ref_id (0212's portable-identity rule) — and the card
-- re-emit fires only on stale text, so a replay is a no-op.
--
-- The card refresh is the upsert_card_combined pattern: DELETE + INSERT
-- of the ord -1 `card_combined` row (never UPDATE — the fresh row
-- re-enters the embed queue; `ord < 0` card variants are the declared
-- exception to body-chunk append-only).
--
-- Forward-only (ADR 0005). Squawk: INSERT/UPDATE/DELETE on live rows, no
-- DDL.

BEGIN;

-- Live taxa under `measurand` (any depth): the scope every statement
-- resolves slugs against. Only ref_id is kept — slugs are read live off
-- `refs` so the post-rename state is what the dedup sees.
CREATE TEMP TABLE _measurand_under ON COMMIT DROP AS
WITH RECURSIVE root AS (
    SELECT ref_id FROM refs
     WHERE kind = 'taxon' AND retired_at IS NULL
       AND meta->>'start' = 'true' AND meta->>'slug' = 'measurand'
),
under (ref_id) AS (
    SELECT ref_id FROM root
    UNION
    SELECT l.src_ref_id
      FROM links l
      JOIN under u ON l.dst_ref_id = u.ref_id
      JOIN refs c ON c.ref_id = l.src_ref_id
                 AND c.kind = 'taxon' AND c.retired_at IS NULL
     WHERE l.relation = 'specialises'
       AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
)
SELECT DISTINCT u.ref_id FROM under u;

-- 1. The landed F/m^2 node is the AREAL measurand: rename it (title +
--    meta name/norm_name/slug/definition/aliases; canonical_unit,
--    si_vector, status, links untouched). The canonical_unit='F/m^2'
--    guard keeps a replay off the new F node, and the NOT EXISTS keeps
--    the rename from double-slugging a hand-minted areal node.
UPDATE refs r
   SET title = 'Areal double-layer capacitance',
       meta = r.meta || jsonb_build_object(
           'name', 'Areal double-layer capacitance',
           'norm_name', 'areal double-layer capacitance',
           'slug', 'areal-double-layer-capacitance',
           'definition',
           'Areal double-layer capacitance is the capacitance of the electrochemical double layer at an electrode surface per unit area — the area-normalized form; the total (in farads) is `double-layer-capacitance`.',
           'aliases',
           '["areal capacitance", "double-layer capacitance per area", "areal specific capacitance"]'::jsonb)
 WHERE r.kind = 'taxon' AND r.retired_at IS NULL
   AND r.ref_id IN (SELECT ref_id FROM _measurand_under)
   AND r.meta->>'slug' = 'double-layer-capacitance'
   AND r.meta->>'canonical_unit' = 'F/m^2'
   AND NOT EXISTS (SELECT 1 FROM _measurand_under u
                    JOIN refs o ON o.ref_id = u.ref_id
                    WHERE o.meta->>'slug' = 'areal-double-layer-capacitance');

-- 2. xrd-peak-position: θ ≠ 2θ — drop the conflating alias. The `?`
--    guard makes a replay a no-op.
UPDATE refs r
   SET meta = jsonb_set(r.meta, '{aliases}',
                        (r.meta->'aliases') - 'Bragg angle')
 WHERE r.kind = 'taxon' AND r.retired_at IS NULL
   AND r.ref_id IN (SELECT ref_id FROM _measurand_under)
   AND r.meta->>'slug' = 'xrd-peak-position'
   AND r.meta->'aliases' ? 'Bragg angle';

-- 3. Re-emit both nodes' card_combined rows (text = name + definition +
--    aliases, the 0305 shape). DELETE only when the stored text is stale;
--    INSERT only when no card row is left — a replay writes nothing.
DELETE FROM chunks c
 USING refs r
 WHERE c.ref_id = r.ref_id AND c.ord = -1 AND c.chunk_kind = 'card_combined'
   AND r.kind = 'taxon' AND r.retired_at IS NULL
   AND r.ref_id IN (SELECT ref_id FROM _measurand_under)
   AND r.meta->>'slug' IN ('areal-double-layer-capacitance',
                           'xrd-peak-position')
   AND c.text IS DISTINCT FROM
       (r.meta->>'name') || ' — ' || (r.meta->>'definition') ||
       CASE WHEN jsonb_array_length(r.meta -> 'aliases') > 0
            THEN ' (aka ' || (SELECT string_agg(a, ', ')
                              FROM jsonb_array_elements_text(
                                   r.meta -> 'aliases') AS a)
                 || ')'
            ELSE '' END;

INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
SELECT r.ref_id, -1, 'card_combined',
       (r.meta->>'name') || ' — ' || (r.meta->>'definition') ||
       CASE WHEN jsonb_array_length(r.meta -> 'aliases') > 0
            THEN ' (aka ' || (SELECT string_agg(a, ', ')
                              FROM jsonb_array_elements_text(
                                   r.meta -> 'aliases') AS a)
                 || ')'
            ELSE '' END,
       '{}'::jsonb
  FROM refs r
 WHERE r.kind = 'taxon' AND r.retired_at IS NULL
   AND r.ref_id IN (SELECT ref_id FROM _measurand_under)
   AND r.meta->>'slug' IN ('areal-double-layer-capacitance',
                           'xrd-peak-position')
   AND NOT EXISTS (SELECT 1 FROM chunks c
                    WHERE c.ref_id = r.ref_id AND c.ord = -1
                      AND c.chunk_kind = 'card_combined');

-- 4. Mint the TOTAL double-layer capacitance (farads) — the generic term
--    the renamed areal node gave up. Same insert-only-if-missing shape
--    as 0305: skipped when a live `double-layer-capacitance` sits under
--    `measurand`, no-op without the start node.
WITH root AS (
    SELECT ref_id FROM refs
     WHERE kind = 'taxon' AND retired_at IS NULL
       AND meta->>'start' = 'true' AND meta->>'slug' = 'measurand'
     ORDER BY ref_id
     LIMIT 1
),
ins AS (
    INSERT INTO refs (kind, title, meta)
    SELECT 'taxon', 'Double-layer capacitance',
           jsonb_strip_nulls(jsonb_build_object(
               'name', 'Double-layer capacitance',
               'norm_name', 'double-layer capacitance',
               'slug', 'double-layer-capacitance',
               'definition',
               'Double-layer capacitance is the total capacitance of the electrochemical double layer at an electrode interface, in farads — the C_dl an impedance fit reports; per unit area it is `areal-double-layer-capacitance`.',
               'aliases',
               '["Cdl", "C_dl", "double layer capacitance"]'::jsonb,
               'status', 'proposed',
               'dimension_kind', 'si',
               'si_vector', '-2,-1,4,2,0,0,0',
               'canonical_unit', 'F',
               'display_unit', 'uF',
               'value_type', 'quantity'
           ))
      FROM root
     WHERE NOT EXISTS (SELECT 1 FROM _measurand_under u
                        JOIN refs o ON o.ref_id = u.ref_id
                        WHERE o.meta->>'slug' = 'double-layer-capacitance')
    RETURNING ref_id, meta
),
card AS (
    INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
    SELECT ins.ref_id, -1, 'card_combined',
           (ins.meta->>'name') || ' — ' || (ins.meta->>'definition') ||
           CASE WHEN jsonb_array_length(ins.meta -> 'aliases') > 0
                THEN ' (aka ' || (SELECT string_agg(a, ', ')
                                  FROM jsonb_array_elements_text(
                                       ins.meta -> 'aliases') AS a)
                     || ')'
                ELSE '' END,
           '{}'::jsonb
      FROM ins
    RETURNING ref_id
)
INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
SELECT ins.ref_id, root.ref_id, 'specialises', 'system'
  FROM ins
 CROSS JOIN root;

COMMIT;

-- End of 0306_survey_measurand_fixes.sql
