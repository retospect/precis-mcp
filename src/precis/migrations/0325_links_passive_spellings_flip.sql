-- 0325_links_passive_spellings_flip.sql
--
-- `Store.add_link` stores the passive spelling of eight asymmetric relations
-- as the active edge with the ends swapped (`_CANONICAL_FLIP`): served-by ->
-- serves, cited-by -> cites, has-draft -> draft-of, has-plan -> plan-of,
-- prerequisite-of -> has-prerequisite, represented-by -> represents,
-- has-dossier -> dossier-of, has-paper -> paper-of. Rows written before
-- that flip existed carry the passive slug literally, so
-- a reader that walks only the active spelling never sees them. This
-- rewrites them: for each pair, a passive row whose flipped twin already
-- exists is dropped as a duplicate; every other one has its relation and
-- ends flipped in place.
--
-- In place (UPDATE), not INSERT + DELETE: the row keeps its link_id,
-- created_at and trust columns (the trust guard refuses raw trust_* values
-- on INSERT), and the revisions trigger logs the flip as an edit of the
-- same link instead of a delete plus an unrelated create. The trust guard's
-- internal door (`precis.trust_internal`, as the body guard uses) keeps the
-- stamp unchanged: a flip changes the spelling, not the claim.
--
-- Unique key: `links_endpoints_relation_idx` on (src_ref_id, src_chunk_id,
-- dst_ref_id, dst_chunk_id, relation) NULLS NOT DISTINCT, so the twin test
-- uses IS NOT DISTINCT FROM on the chunk ends.
--
-- The `relations` vocabulary is untouched: the passive slugs stay
-- registered, callers may still say them. Idempotent: a replay finds no
-- passive rows. Forward-only (ADR 0005).

BEGIN;

DO $$
DECLARE
    v_pair    text[];
    v_dropped bigint;
    v_flipped bigint;
BEGIN
    PERFORM set_config('precis.trust_internal', 'on', true);
    FOREACH v_pair SLICE 1 IN ARRAY ARRAY[
        ['served-by', 'serves'],
        ['cited-by', 'cites'],
        ['has-draft', 'draft-of'],
        ['has-plan', 'plan-of'],
        ['prerequisite-of', 'has-prerequisite'],
        ['represented-by', 'represents'],
        ['has-dossier', 'dossier-of'],
        ['has-paper', 'paper-of']
    ] LOOP
        DELETE FROM links p
         WHERE p.relation = v_pair[1]
           AND EXISTS (
               SELECT 1 FROM links a
                WHERE a.relation = v_pair[2]
                  AND a.src_ref_id = p.dst_ref_id
                  AND a.dst_ref_id = p.src_ref_id
                  AND a.src_chunk_id IS NOT DISTINCT FROM p.dst_chunk_id
                  AND a.dst_chunk_id IS NOT DISTINCT FROM p.src_chunk_id);
        GET DIAGNOSTICS v_dropped = ROW_COUNT;

        UPDATE links
           SET relation = v_pair[2],
               src_ref_id = dst_ref_id, dst_ref_id = src_ref_id,
               src_chunk_id = dst_chunk_id, dst_chunk_id = src_chunk_id
         WHERE relation = v_pair[1];
        GET DIAGNOSTICS v_flipped = ROW_COUNT;

        RAISE NOTICE '0325 %: % flipped to %, % dropped as duplicates',
            v_pair[1], v_flipped, v_pair[2], v_dropped;
    END LOOP;
    PERFORM set_config('precis.trust_internal', '', true);
END $$;

COMMIT;

-- End of 0325_links_passive_spellings_flip.sql
