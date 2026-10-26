-- 0253_fx_rates_price_index.sql
--
-- Currency conversion caches — slice 2 of
-- docs/backlog/measure-currency-units.md (gr510555). Both tables are
-- *caches*, not append-only evidence: the money_refresh job upserts
-- them on a weekly todo and reads recompute, so a CPI revision or a
-- restated ECB day applies everywhere.
--
--   * fx_rates        — the ECB euro reference rates, one row per
--                       (currency, rate_date). `per_eur` is units of
--                       the currency per 1 EUR, exactly as published.
--   * fx_rates_annual — the view readers consume: per (currency, year)
--                       the average quote, the quote-day count (a thin
--                       year refuses conversion) and the span covered.
--   * price_index     — annual price-index points keyed by series
--                       ('wb:FP.CPI.TOTL:USA', the World Bank US CPI —
--                       FX-first conversion needs no other series).
--
-- Forward-only (ADR 0005). Idempotent. One transaction.

BEGIN;

-- The runner sets lock_timeout = 0 for the apply (store/migrate.py); bound
-- the DDL wait behind a long reader instead (convention from 0224/0226).
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS fx_rates (
    currency    text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    rate_date   date NOT NULL,
    per_eur     numeric NOT NULL CHECK (per_eur > 0),
    source      text NOT NULL,           -- e.g. 'ecb:eurofxref-hist'
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (currency, rate_date)
);
COMMENT ON TABLE fx_rates IS
    'ECB euro reference rates cache (gr510555 S2): per_eur is units of '
    'the currency per 1 EUR as published. Upserted by the money_refresh '
    'job — a cache, not append-only; the upsert skips unchanged rows, '
    'so fetched_at marks the last CHANGE, not the last poll (poll '
    'freshness lives in money_refresh_log).';
COMMENT ON COLUMN fx_rates.per_eur IS
    'Units of `currency` per 1 EUR. fx(C->USD, Y) reads the annual view: '
    'per_eur_avg(USD,Y) / per_eur_avg(C,Y); EUR itself is the numeraire '
    '(per_eur = 1) and has no rows.';
COMMENT ON COLUMN fx_rates.source IS
    'Fill provenance for the truncation guard (a parse must yield >=90% '
    'of the cached rows for the same source).';

CREATE OR REPLACE VIEW fx_rates_annual AS
    SELECT currency,
           EXTRACT(YEAR FROM rate_date)::integer AS year,
           avg(per_eur) AS per_eur_avg,
           count(*) AS n_days,
           min(rate_date) AS first_date,
           max(rate_date) AS last_date
      FROM fx_rates
     GROUP BY currency, EXTRACT(YEAR FROM rate_date);
COMMENT ON VIEW fx_rates_annual IS
    'Annual average of the ECB daily quotes per currency (gr510555 S2). '
    'n_days is the thin-year evidence: under 50 quote days the read path '
    'refuses rather than converts on a fragment.';

CREATE TABLE IF NOT EXISTS price_index (
    series      text NOT NULL,
    year        bigint NOT NULL CHECK (year BETWEEN 1900 AND 2200),
    value       numeric NOT NULL CHECK (value > 0),
    source      text NOT NULL,           -- e.g. 'worldbank:v2'
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (series, year)
);
COMMENT ON TABLE price_index IS
    'Annual price-index cache (gr510555 S2): today only '
    'wb:FP.CPI.TOTL:USA (World Bank US CPI, 2010=100). Upserted by the '
    'money_refresh job — a CPI revision lands as an upsert and applies '
    'to every conversion.';
COMMENT ON COLUMN price_index.series IS
    'Series token, e.g. ''wb:FP.CPI.TOTL:USA''. FX-first conversion '
    'needs only the US CPI; a second series is a new constant, not a '
    'schema change.';

-- Freshness of the caches, one row per source. The upserts skip
-- unchanged rows (IS DISTINCT FROM), so a row's fetched_at marks the
-- last value CHANGE; this log bumps on every successful refresh — the
-- "is the cache fresh" answer a weekly cadence needs.
CREATE TABLE IF NOT EXISTS money_refresh_log (
    source       text PRIMARY KEY,     -- 'ecb' | 'worldbank'
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    rows_written bigint NOT NULL CHECK (rows_written >= 0)
);
COMMENT ON TABLE money_refresh_log IS
    'money_refresh freshness (gr510555 S2): one row per source, bumped '
    'on every successful poll. Cache-row fetched_at marks the last '
    'CHANGE; refreshed_at here marks the last successful fetch.';

COMMIT;

-- End of 0253_fx_rates_price_index.sql
