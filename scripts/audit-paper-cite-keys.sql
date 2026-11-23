-- gr762557. Run with scripts/prod-psql --ro -f scripts/audit-paper-cite-keys.sql
-- The summary counts papers, including zero findings. A paper can count in
-- both reason columns but appears only once in the total and detail rows.
-- Review the results before repair: extra keys may be intentional, and the
-- legacy surname + four-digit year + title word pattern is a heuristic.
-- Compare whole words case-insensitively. Accented or otherwise transformed
-- titles can produce false positives. Exclude retained cite_key_alias rows.
WITH live_keys AS (
    SELECT r.ref_id, r.title, ri.id_value, ri.source,
           substring(lower(ri.id_value) FROM '^[a-z]+[0-9]{4}([a-z]+)$') AS title_word
    FROM refs r
    JOIN ref_identifiers ri USING (ref_id)
    WHERE r.kind = 'paper' AND r.retired_at IS NULL AND ri.id_kind = 'cite_key'
), flagged_keys AS (
    SELECT *,
           lower(title) !~ ('\m' || title_word || '\M') AS title_word_missing
    FROM live_keys
), papers AS (
    SELECT ref_id, title,
           count(*) AS live_key_count,
           jsonb_object_agg(id_value, source) AS keys_by_source,
           coalesce(array_agg(id_value ORDER BY id_value)
                    FILTER (WHERE title_word_missing), ARRAY[]::text[]) AS mismatched_keys
    FROM flagged_keys
    GROUP BY ref_id, title
), findings AS (
    SELECT * FROM papers WHERE live_key_count > 1 OR cardinality(mismatched_keys) > 0
)
SELECT 'summary' AS row_type, NULL::bigint AS ref_id, NULL::text AS title,
       count(*) AS flagged_papers,
       count(*) FILTER (WHERE live_key_count > 1) AS papers_with_multiple_keys,
       count(*) FILTER (WHERE cardinality(mismatched_keys) > 0) AS papers_with_missing_title_word,
       NULL::bigint AS live_key_count, NULL::jsonb AS keys_by_source, NULL::text[] AS mismatched_keys
FROM findings
UNION ALL
SELECT 'paper', ref_id, title, NULL, NULL, NULL, live_key_count, keys_by_source, mismatched_keys
FROM findings
ORDER BY ref_id NULLS FIRST;
