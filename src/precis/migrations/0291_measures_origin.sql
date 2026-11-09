-- 0291_measures_origin.sql
--
-- The `origin` axis on a measure (gr606464; Reto ruling 2026-11-09, the
-- ALCHEM Lab gap analysis). It answers "where was this number produced" —
-- the ALCHEM schema's PropertyValue.origin — on its own axis, not folded
-- into `tier` (how the row ranks and what evidence it needs) or `method`
-- (the legacy free-text tag). This supersedes the 2026-09-30
-- measures-substrate decision that mapped origin onto tier with no new
-- values: under that fold an ML prediction read as 'computed', ranking
-- alongside experimental numbers.
--
--   * `measures.origin` — experiment | computation | ml_prediction |
--     literature; NULL on every pre-existing row (not recorded). Frozen
--     like every other measures column (precis_measures_frozen iterates
--     the row's jsonb keys); a wrong origin is corrected by supersession.
--
--   * Rankings exclude it by default: `best_measure` drops
--     `origin='ml_prediction'` unless `include_ml_predictions=True` —
--     Python-side, like the rest of its eligibility list, so no index.
--     Search and detail reads keep every live row: the axis marks the
--     number's provenance, never hides it.
--
-- `precis_measure_sha` gains one trailing concat_ws argument:
-- NULLIF(precis_sha_part(m.origin), 'NULL') is NULL on every existing row
-- (origin NULL), so concat_ws skips it and every sha — and every review
-- pinned on it — is unchanged. A row carrying an origin hashes it quoted,
-- the same shape as the other text fields.
--
-- Forward-only (ADR 0005). One transaction.

BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound
-- the DDL wait behind a long reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

ALTER TABLE measures
    ADD COLUMN origin text
        CHECK (origin IN ('experiment', 'computation', 'ml_prediction',
               'literature'));

COMMENT ON COLUMN measures.origin IS
    'Where the number itself was produced (ALCHEM PropertyValue.origin): experiment | computation | ml_prediction | literature. NULL = not recorded. A different axis from tier (evidence/rank) and method (legacy tag). best_measure excludes ml_prediction unless asked.';

-- One trailing concat_ws argument: NULL on origin NULL rows, so every sha
-- computed before this migration is identical after it.
CREATE OR REPLACE FUNCTION precis_measure_sha(p_id bigint)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT left(md5(concat_ws('|',
        precis_sha_part(m.subject_ref_id::text),
        precis_sha_part(m.measurand_ref_id::text),
        precis_sha_part(m.literal),
        precis_sha_part(m.reported_unit),
        precis_sha_float(m.value_num),
        precis_sha_float(m.value_low),
        precis_sha_float(m.value_high),
        precis_sha_part(m.value_text),
        precis_sha_part((CASE m.value_bool WHEN true THEN 't' WHEN false THEN 'f' END)::text),
        precis_sha_float(m.value_err),
        precis_sha_part(m.value_form),
        precis_sha_part(m.reference),
        precis_sha_part(m.tier),
        precis_sha_part(m.normalization),
        precis_sha_part(m.normalization_status),
        precis_sha_part(m.source_attribution),
        precis_sha_part(m.measurand_status),
        precis_sha_part(m.direction),
        precis_sha_part(m.role),
        precis_sha_part(m.run_key),
        precis_sha_part(m.subject),
        precis_sha_part(m.subject_group),
        precis_sha_part(m.experiment_ref_id::text),
        precis_sha_part(m.derived_from::text),
        precis_sha_part(m.primary_link_id::text),
        precis_sha_part(m.anchor_scheme),
        precis_sha_part(m.span::text),
        precis_sha_part(m.supersedes::text),
        precis_sha_part(m.source_ref_id::text),
        precis_sha_part(m.actor),
        precis_sha_part(m.model),
        precis_sha_part(m.input_unit),
        precis_sha_part(m.conditions::text),
        precis_sha_part(m.maturity),
        precis_sha_part(m.method),
        precis_sha_part(m.source_chunk),
        precis_sha_part(m.source_url),
        precis_sha_part(to_char(m.as_of, 'YYYY-MM-DD')),
        precis_sha_part(m.notes),
        NULLIF(concat_ws('@', m.currency, m.price_year), ''),
        NULLIF(precis_sha_part(m.origin), 'NULL'))), 16)
      FROM measures m WHERE m.id = p_id
$$;

COMMIT;

-- End of 0291_measures_origin.sql
