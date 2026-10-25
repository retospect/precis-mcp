-- 0249_measures_currency.sql
--
-- Currency as reported (measure-currency-units slice 1, gr510555). A cost
-- measure keeps the currency and price year it was written in:
--
--   * `measures.currency` — the ISO 4217 code of the reported_unit
--     ('EUR' on 'EUR2015/kg'); NULL on every pre-existing row. A legacy row's
--     effective currency is the token in its reported_unit, else the one in
--     its taxon's canonical unit — right for all live rows, so reading them
--     needs no write.
--   * `measures.price_year` — the year the price is at ('EUR2015/t' or
--     meta.price_year=2015); NULL means the year is unknown and the row is
--     shown as reported, never converted.
--
-- Both columns are frozen like the rest of `measures` (precis_measures_frozen
-- iterates the row's jsonb keys, so it covers them without being told);
-- setting a price year on a legacy row is a supersession, never an UPDATE.
--
-- `precis_measure_sha` gains one trailing concat_ws argument: NULL when both
-- new columns are NULL, so every existing sha — and every review pinned on
-- it — is unchanged.
--
-- Rollback shape: a forward migration dropping the columns and restoring
-- the 0187 sha body. Forward-only (ADR 0005). One transaction.

BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound
-- the DDL wait behind a long reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

ALTER TABLE measures
    ADD COLUMN currency text CHECK (currency ~ '^[A-Z]{3}$'),
    ADD COLUMN price_year integer CHECK (price_year BETWEEN 1900 AND 2100);

COMMENT ON COLUMN measures.currency IS
    'ISO 4217 code of the reported currency, as reported (EUR on EUR2015/kg). NULL = legacy row; its effective currency is the token in reported_unit, else the taxon''s canonical unit.';
COMMENT ON COLUMN measures.price_year IS
    'The year the price is at (EUR2015/t or meta.price_year). NULL = unknown: shown as reported, never converted.';

-- One trailing concat_ws argument: NULL when both new columns are NULL, so
-- concat_ws skips it and every sha computed before this migration is
-- identical after it.
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
        NULLIF(concat_ws('@', m.currency, m.price_year), ''))), 16)
      FROM measures m WHERE m.id = p_id
$$;

COMMIT;

-- End of 0249_measures_currency.sql
