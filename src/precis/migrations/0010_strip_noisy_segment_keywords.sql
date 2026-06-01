-- 0010_strip_noisy_segment_keywords.sql
--
-- Backfill cleanup: ref_segments.keywords + ref_segments.forms
-- contain ``"na"``, ``"na na"``, etc. for refs whose ``segment_toc``
-- ran while their table chunks were still labelled ``chunk_kind =
-- 'paragraph'`` (pre-migration 0009). The segmenter happily pulled
-- the empty-cell ``na`` tokens into the per-segment keyword list,
-- which then surfaced in ``precis tools search`` output.
--
-- Strategy: drop any keyword whose ``long`` form is a short ``na``
-- run, a single capital letter, or an empty-cell sentinel from the
-- deng10-style tables. Same heuristic applied to ``forms[]``. Refs
-- can be re-segmented later (precis worker --only segments) to
-- regenerate richer keywords from the now-corrected chunk_kind
-- labelling — but this strips the visible noise immediately.

BEGIN;

-- 1. Filter the JSONB ``keywords`` array.
UPDATE ref_segments
   SET keywords = (
       SELECT COALESCE(
           jsonb_agg(kw),
           '[]'::jsonb
       )
         FROM jsonb_array_elements(keywords) AS kw
        WHERE NOT (
              -- ``na`` repeated up to 6 times
              lower(kw->>'long') ~ '^(na )*na$'
              -- Single capital letter from table headers (A, B, …, I)
              OR length(kw->>'long') = 1
              -- ``na na na na`` interspersed combos
              OR lower(kw->>'long') ~ '^(na ?){2,}$'
       )
   )
 WHERE keywords IS NOT NULL
   AND keywords @> '[]'::jsonb
   AND EXISTS (
       SELECT 1 FROM jsonb_array_elements(keywords) kw
       WHERE lower(kw->>'long') ~ '^(na )*na$' OR length(kw->>'long') = 1
   );

-- 2. Strip the denormalised ``forms`` TEXT[] using the same rules.
UPDATE ref_segments
   SET forms = ARRAY(
       SELECT f
         FROM unnest(forms) AS f
        WHERE NOT (lower(f) ~ '^(na )*na$')
          AND NOT (length(f) = 1)
   )
 WHERE forms IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM unnest(forms) f
       WHERE lower(f) ~ '^(na )*na$' OR length(f) = 1
   );

COMMIT;
