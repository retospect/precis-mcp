-- 0309_survey_tof_fold.sql
--
-- Fold the duplicate TOF measurand into `turnover frequency` (gr638050).
--
-- The minting path checked: 0305's survey seed minted its `tof` row
-- (name 'TOF', alias 'turnover frequency') because its insert-only-missing
-- guard dedupes on SLUG ONLY (`_measurand_under`): the hand-put
-- `turnover-frequency` node (which carries 'TOF' as an alias, tn520139)
-- has a different slug, so the seed never saw it and split one quantity
-- into two nodes. Every draft edit naming TOF then warned "ambiguous
-- label 'tof'" (the draft lint names every live claimant of a surface).
-- The missing lookup is the name/norm/alias surface match
-- (store.taxon_find_by_term) the reader vocabulary and the draft lint
-- actually use — the seed checked slugs. 0305 is sealed, so the repair
-- is this fold; the next measurand seed must dedupe on surfaces, not
-- slugs, or the split regrows.
--
-- The fold (gr638050: one node, the loser folded into the winner with
-- its surface kept):
--
-- * winner = the live `turnover-frequency` node under `measurand`
--   (tn520139, the 11 quantifies links); loser = the live `tof` node
--   the seed minted (tn635191: no quantifies links, no trust stamp);
--   the fold fires only when the two share a name/alias surface — the
--   same-quantity evidence (each names the other) and the licence to
--   merge at all;
-- * the winner keeps the folded surface: alias 'TOF' added when neither
--   the name nor the aliases already claim it, and the ord -1 card is
--   re-emitted (DELETE + INSERT, the upsert_card_combined pattern) so
--   the fresh row re-enters the embed queue;
-- * every edge on the loser is re-pointed onto the winner with the
--   garden merge's migrate_links semantics: ref ends substitute,
--   chunk ends on the substituted side re-ground onto the winner's
--   identical-text chunk or de-ground to NULL (with prior_* stamped in
--   meta), chunk ends on the unchanged side stay verbatim, would-be
--   self-loops are skipped, and the NULLS NOT DISTINCT unique index
--   dedups a copy against a pre-existing twin. The original rows are
--   then deleted. Trust acts bound to the old link_ids stay history on
--   the deleted rows, the one thing this SQL fold does not carry over
--   (the garden door's carry conversion is out of scope for a data
--   repair; the loser has no edges worth stamping in practice);
-- * measures on the loser (none expected on a day-old proposed node)
--   re-point through the 0234 door when canonical_unit /
--   dimension_kind / si_vector all match the winner IS NOT DISTINCT
--   FROM; a contract mismatch, a measure that references the loser any
--   other way (subject, source, primary link), or a current content-sha
--   review pinning one of those rows SKIPS the whole fold with a NOTICE
--   rather than mislabel a number or stale a pin;
-- * a chunk-level edge FROM another ref INTO the loser's chunks likewise
--   skips the fold (nothing real is expected to carry one; loud beats
--   wrong);
-- * the loser retires — 0229's trigger writes the ref_events trail — and
--   a `supersedes` edge records the fold so the graph keeps the history.
--
-- Every step is keyed on (slug, alias presence), never ref_id (0212's
-- portable-identity rule), and every guard re-reads live state, so a
-- replay is a no-op: after the fold the loser is retired, and on a fresh
-- replay there is no hand-put `turnover-frequency` under `measurand`,
-- so the fold has no winner and 0305's lone `tof` node stands. (The
-- fold leans on the migration ledger for that: a manual 0305 replay
-- after the fold WOULD re-mint `tof` — seeds must not be replayed by
-- hand.)
--
-- Forward-only (ADR 0005). Squawk: DML in one DO block, no DDL.

BEGIN;

-- Live taxa under `measurand` (any depth): the scope both roles resolve
-- against, same CTE as 0305/0306. Only ref_id is kept; slugs and aliases
-- are read live off `refs`.
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

DO $$
DECLARE
    v_loser   bigint;
    v_winner  bigint;
    v_n       bigint;
    v_measures bigint;
    v_pinned  bigint;
    v_contract_ok boolean;
BEGIN
    -- The winner: the hand-put `turnover-frequency` node (gr638050's
    -- tn520139, the 11 quantifies links).
    SELECT w.ref_id INTO v_winner
      FROM refs w
     WHERE w.kind = 'taxon' AND w.retired_at IS NULL
       AND w.ref_id IN (SELECT ref_id FROM _measurand_under)
       AND w.meta->>'slug' = 'turnover-frequency'
     ORDER BY w.ref_id
     LIMIT 1;

    -- The loser: 0305's mint — slug `tof`, name 'TOF', still live.
    SELECT r.ref_id INTO v_loser
      FROM refs r
     WHERE r.kind = 'taxon' AND r.retired_at IS NULL
       AND r.ref_id IN (SELECT ref_id FROM _measurand_under)
       AND r.meta->>'slug' = 'tof'
       AND lower(regexp_replace(
             btrim(coalesce(r.meta->>'name', r.title)), '\s+', ' ', 'g')) = 'tof'
       AND r.ref_id IS DISTINCT FROM v_winner
     ORDER BY r.ref_id
     LIMIT 1;

    IF v_winner IS NULL OR v_loser IS NULL THEN
        RAISE NOTICE 'survey TOF fold: no live tof/turnover-frequency pair — nothing to do';
        RETURN;
    END IF;

    -- The same-quantity evidence, and the fold's whole licence: the two
    -- nodes claim a shared surface. On prod the winner carries 'TOF' as
    -- an alias while the loser carries 'turnover frequency' — each names
    -- the other. Without a shared surface these are two quantities that
    -- happen to share a slug family, and the fold must not touch them.
    IF NOT EXISTS (
        WITH surf AS (
            SELECT r.ref_id,
                   array_agg(DISTINCT s) AS surfaces
              FROM refs r
              CROSS JOIN LATERAL (
                  SELECT lower(regexp_replace(
                             btrim(coalesce(r.meta->>'name', r.title)),
                             '\s+', ' ', 'g'))
                  UNION
                  SELECT r.meta->>'slug'
                  UNION
                  SELECT r.meta->>'norm_name'
                  UNION
                  SELECT lower(regexp_replace(btrim(a.alias), '\s+', ' ', 'g'))
                    FROM jsonb_array_elements_text(
                             CASE WHEN jsonb_typeof(r.meta->'aliases') = 'array'
                                  THEN r.meta->'aliases' ELSE '[]'::jsonb END
                         ) AS a(alias)
              ) AS u(s)
             WHERE r.kind = 'taxon' AND r.retired_at IS NULL
               AND r.ref_id IN (v_loser, v_winner)
             GROUP BY r.ref_id
        )
        SELECT 1 FROM surf l JOIN surf w
          ON l.ref_id = v_loser AND w.ref_id = v_winner
         WHERE l.surfaces && w.surfaces
    ) THEN
        RAISE NOTICE 'survey TOF fold: tof % and turnover-frequency % share no surface — not the same quantity, skipped', v_loser, v_winner;
        RETURN;
    END IF;

    -- A measure that references the loser any way other than as its
    -- measurand would strand it on the retirement; the measurand_ref_id
    -- rows are re-pointed below when they are safe to move.
    SELECT count(*) INTO v_n
      FROM measures
     WHERE subject_ref_id = v_loser
        OR source_ref_id = v_loser
        OR primary_link_id IN (SELECT link_id FROM links
                                WHERE src_ref_id = v_loser
                                   OR dst_ref_id = v_loser);
    IF v_n > 0 THEN
        RAISE NOTICE 'survey TOF fold: loser % carries % measure reference(s) as subject/source/primary-link — skipped; supersede them onto the winner first', v_loser, v_n;
        RETURN;
    END IF;

    SELECT count(*) INTO v_measures
      FROM measures
     WHERE measurand_ref_id = v_loser;
    IF v_measures > 0 THEN
        -- 0234's door: re-point only onto the same unit contract, and
        -- never under a current content-sha pin (the re-point stales it
        -- by changing precis_measure_sha).
        SELECT (l.meta->'canonical_unit' IS NOT DISTINCT FROM w.meta->'canonical_unit'
            AND l.meta->'dimension_kind' IS NOT DISTINCT FROM w.meta->'dimension_kind'
            AND l.meta->'si_vector' IS NOT DISTINCT FROM w.meta->'si_vector')
          INTO v_contract_ok
          FROM refs l, refs w
         WHERE l.ref_id = v_loser AND w.ref_id = v_winner;
        SELECT count(*) INTO v_pinned
          FROM reviews rv
          JOIN measures mm ON rv.target_kind = 'measure' AND rv.target_id = mm.id
         WHERE mm.measurand_ref_id = v_loser
           AND rv.sha_basis = 'content'
           AND rv.content_sha = precis_measure_sha(mm.id);
        IF NOT v_contract_ok OR v_pinned > 0 THEN
            RAISE NOTICE 'survey TOF fold: loser % carries % measure row(s) (% pinned) and the winner is not a safe re-point target — skipped', v_loser, v_measures, v_pinned;
            RETURN;
        END IF;
        PERFORM set_config('precis.allow_measurand_repoint', 'on', true);
        UPDATE measures SET measurand_ref_id = v_winner WHERE measurand_ref_id = v_loser;
    END IF;

    -- A chunk-level edge from another ref into the loser's chunks is the
    -- one shape this fold does not re-home; refuse rather than guess.
    SELECT count(*) INTO v_n
      FROM links
     WHERE (src_chunk_id IN (SELECT chunk_id FROM chunks WHERE ref_id = v_loser)
            AND src_ref_id <> v_loser)
        OR (dst_chunk_id IN (SELECT chunk_id FROM chunks WHERE ref_id = v_loser)
            AND dst_ref_id <> v_loser);
    IF v_n > 0 THEN
        RAISE NOTICE 'survey TOF fold: loser % carries % foreign chunk-level edge(s) — skipped; fold it through the garden merge instead', v_loser, v_n;
        RETURN;
    END IF;

    -- Edge migration (the garden merge's migrate_links semantics, in
    -- SQL): copy every edge touching the loser onto the winner, then
    -- delete the originals. A chunk end on the substituted side
    -- re-grounds onto the winner's identical-text chunk or de-grounds to
    -- NULL (prior_* recorded); the unchanged side stays verbatim.
    INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
                       relation, set_by, meta)
    SELECT
        CASE WHEN l.src_ref_id = v_loser THEN v_winner ELSE l.src_ref_id END,
        CASE WHEN l.src_ref_id = v_loser AND l.src_chunk_id IS NOT NULL THEN
            (SELECT wc.chunk_id FROM chunks wc
              WHERE wc.ref_id = v_winner AND wc.ord = sc.ord
                AND wc.chunk_kind = sc.chunk_kind AND wc.text = sc.text
              ORDER BY wc.chunk_id LIMIT 1)
        ELSE l.src_chunk_id END,
        CASE WHEN l.dst_ref_id = v_loser THEN v_winner ELSE l.dst_ref_id END,
        CASE WHEN l.dst_ref_id = v_loser AND l.dst_chunk_id IS NOT NULL THEN
            (SELECT wc.chunk_id FROM chunks wc
              WHERE wc.ref_id = v_winner AND wc.ord = dc.ord
                AND wc.chunk_kind = dc.chunk_kind AND wc.text = dc.text
              ORDER BY wc.chunk_id LIMIT 1)
        ELSE l.dst_chunk_id END,
        l.relation, l.set_by,
        coalesce(l.meta, '{}'::jsonb)
          || jsonb_build_object('prior_ref_id', v_loser)
          || CASE WHEN l.src_ref_id = v_loser AND l.src_chunk_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM chunks wc
                                    WHERE wc.ref_id = v_winner AND wc.ord = sc.ord
                                      AND wc.chunk_kind = sc.chunk_kind
                                      AND wc.text = sc.text)
                  THEN jsonb_build_object('prior_src_chunk_id', l.src_chunk_id)
                  ELSE '{}'::jsonb END
          || CASE WHEN l.dst_ref_id = v_loser AND l.dst_chunk_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM chunks wc
                                    WHERE wc.ref_id = v_winner AND wc.ord = dc.ord
                                      AND wc.chunk_kind = dc.chunk_kind
                                      AND wc.text = dc.text)
                  THEN jsonb_build_object('prior_dst_chunk_id', l.dst_chunk_id)
                  ELSE '{}'::jsonb END
      FROM links l
      LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id
      LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id
     WHERE (l.src_ref_id = v_loser OR l.dst_ref_id = v_loser)
       AND (CASE WHEN l.src_ref_id = v_loser THEN v_winner ELSE l.src_ref_id END)
        <> (CASE WHEN l.dst_ref_id = v_loser THEN v_winner ELSE l.dst_ref_id END)
    ON CONFLICT DO NOTHING;

    DELETE FROM links WHERE src_ref_id = v_loser OR dst_ref_id = v_loser;

    -- The winner keeps the folded surface: alias 'TOF' when neither the
    -- name nor the aliases already claim it (case/whitespace folded).
    UPDATE refs w
       SET meta = jsonb_set(w.meta, '{aliases}',
                            coalesce(w.meta->'aliases', '[]'::jsonb)
                                || '["TOF"]'::jsonb)
     WHERE w.ref_id = v_winner
       AND lower(regexp_replace(
             btrim(coalesce(w.meta->>'name', w.title)), '\s+', ' ', 'g')) <> 'tof'
       AND NOT EXISTS (
               SELECT 1
                 FROM jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof(w.meta->'aliases') = 'array'
                             THEN w.meta->'aliases' ELSE '[]'::jsonb END
                      ) AS a(alias)
                WHERE lower(regexp_replace(btrim(alias), '\s+', ' ', 'g')) = 'tof');
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n > 0 THEN
        -- The card re-enters the embed queue: DELETE + INSERT, never
        -- UPDATE (the upsert_card_combined pattern; ord < 0 card
        -- variants are the declared exception to body-chunk
        -- append-only).
        DELETE FROM chunks WHERE ref_id = v_winner AND ord = -1
                             AND chunk_kind = 'card_combined';
        INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta)
        SELECT v_winner, -1, 'card_combined',
               coalesce(w.meta->>'name', w.title) || ' — '
                   || coalesce(w.meta->>'definition', '') ||
               CASE WHEN jsonb_array_length(coalesce(w.meta->'aliases',
                                                     '[]'::jsonb)) > 0
                    THEN ' (aka ' || (SELECT string_agg(a, ', ')
                                       FROM jsonb_array_elements_text(
                                            w.meta->'aliases') AS a)
                         || ')'
                    ELSE '' END,
               '{}'::jsonb
          FROM refs w
         WHERE w.ref_id = v_winner;
    END IF;

    -- Retire the loser (0229 writes the ref_events trail) and record the
    -- fold on the graph.
    UPDATE refs SET retired_at = now() WHERE ref_id = v_loser;
    INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)
    VALUES (v_winner, v_loser, 'supersedes', 'system')
    ON CONFLICT DO NOTHING;

    RAISE NOTICE 'survey TOF fold: retired % into %', v_loser, v_winner;
END $$;

COMMIT;

-- End of 0309_survey_tof_fold.sql
