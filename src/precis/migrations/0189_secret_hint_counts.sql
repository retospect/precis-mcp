-- Saved-entry counts share the existing masked-hint write transaction.
-- No decrypt/backfill: legacy hints remain unchanged until replacement.
-- Unicode code points; CRLF is two characters but one line delimiter.
-- Empty helper input has zero lines; nonempty blank/trailing segments count.
CREATE OR REPLACE FUNCTION vault._hint(v text) RETURNS text
    LANGUAGE sql IMMUTABLE AS $$
    SELECT (CASE
        WHEN v IS NULL OR length(v) = 0 THEN '(empty)'
        WHEN length(v) < 12 THEN repeat(chr(8226), 6) || ' (' || length(v) || ')'
        ELSE left(v, least(3, length(v) / 5)) || chr(8230)
             || right(v, least(2, length(v) / 5))
    END) || ' · ' || coalesce(length(v), 0) || ' chars · '
         || CASE WHEN v IS NULL OR v = '' THEN 0
                 ELSE cardinality(regexp_split_to_array(v, E'\\r\\n|\\r|\\n'))
            END || ' lines';
$$;
