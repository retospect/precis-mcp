-- 0338_correct_simulated_studytype_pa2857.sql
-- gr720473 follow-up: 0335 corrected the four reported paper tags, but pa2857
-- (molecular dynamics) still carried experimental-single-entity on prod
-- (observed 2026-11-20). Forward-only correction for that one paper; 0335 is
-- sealed and stays as landed.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    computational_tag_id bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM refs WHERE kind = 'paper' AND retired_at IS NULL
            AND ref_id = 2857
    ) THEN
        RETURN;
    END IF;

    INSERT INTO tags (namespace, value) VALUES ('STUDYTYPE', 'computational')
    ON CONFLICT (namespace, value) DO NOTHING;
    SELECT tag_id INTO computational_tag_id FROM tags
    WHERE namespace = 'STUDYTYPE' AND value = 'computational';

    WITH corrected AS (
        DELETE FROM ref_tags rt
        USING refs r, tags t
        WHERE rt.ref_id = r.ref_id AND rt.tag_id = t.tag_id
          AND r.ref_id = 2857 AND r.kind = 'paper' AND r.retired_at IS NULL
          AND t.namespace = 'STUDYTYPE'
          AND t.value IN ('experimental-single-entity', 'experimental-ensemble')
        RETURNING rt.ref_id
    )
    INSERT INTO ref_tags (ref_id, tag_id, set_by)
    SELECT ref_id, computational_tag_id, 'system' FROM corrected
    ON CONFLICT (ref_id, tag_id) DO NOTHING;
END $$;
COMMIT;
