-- 0335_repair_computational_studytypes.sql
-- gr720473: correct the four reported paper tags immediately. The versioned
-- axis pass revisits other classifier-owned tags through its normal sweep.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    computational_tag_id bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM refs WHERE kind = 'paper' AND retired_at IS NULL
            AND ref_id IN (3984, 2857, 3838, 2973)
    ) THEN
        RETURN;
    END IF;

    INSERT INTO tags (namespace, value) VALUES ('STUDYTYPE', 'computational')
    ON CONFLICT (namespace, value) DO NOTHING;
    SELECT tag_id INTO computational_tag_id FROM tags
    WHERE namespace = 'STUDYTYPE' AND value = 'computational';

    WITH corrected AS (
        DELETE FROM ref_tags rt
        USING refs r, tags t, (VALUES
            (3984, 'experimental-ensemble'),
            (2857, 'experimental-ensemble'),
            (3838, 'experimental-ensemble'),
            (2973, 'synthesis')
        ) AS report(ref_id, old_value)
        WHERE rt.ref_id = r.ref_id AND rt.tag_id = t.tag_id
          AND r.ref_id = report.ref_id AND r.kind = 'paper' AND r.retired_at IS NULL
          AND t.namespace = 'STUDYTYPE' AND t.value = report.old_value
        RETURNING rt.ref_id
    )
    INSERT INTO ref_tags (ref_id, tag_id, set_by)
    SELECT ref_id, computational_tag_id, 'system' FROM corrected
    ON CONFLICT (ref_id, tag_id) DO NOTHING;
END $$;
COMMIT;
