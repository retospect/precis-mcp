---
status: ready
title: Currency units for measures — USD at a stated price year, stored as reported, converted on read
pillar: quests
---

# Currency units for measures

Origin: gr510555. Rulings (Reto 2026-10-25, relayed; they supersede the
2026-10-24 "EUR canonical", Frankfurter and HICP decisions):

- FX comes from the ECB euro reference rates.
- The deflator is World Bank CPI.
- The canonical unit is USD at a stated price year, and USD is the default
  display.
- Each measure keeps its original currency and price year as reported.
- Build in slices. Store and migration code gets an Opus review.

## Motivation / why

Costs are stored as an opaque `USD` string: no price year, no conversion, no
EUR. Damage and cost figures (climate-quest cost per tonne, disaster losses,
MCB cost per °C) mix EUR and USD across 2015–2026 price years, so they can
be neither compared nor summed. Consumer: the MCB TEA strand (td513645).

Prod today (read 2026-10-25): 14 live taxa with `dimension_kind='currency'`
and 44 measures. Canonical units are `USD`, `USD/kg`, `USD/t`, `USD/yr`,
`USD/kW` and `USD/nozzle`, plus one `EUR` taxon. Every row was inserted with
`reported_unit` equal to the canonical unit, because the same-unit shortcut
in `make_converter` is the only path that accepts a currency. No row has a
price year.

## Design — units, storage, conversion

**Currency token.** A new pure module, `src/precis/taxonomy/money.py`,
provides `split_currency(unit) -> CurrencyUnit(currency, price_year,
remainder)`.

- It reads an ISO code from `ECB_CURRENCIES | {'EUR'}`, or one of the
  symbols `$`→USD, `US$`→USD, `€`→EUR, `£`→GBP. `¥`, `A$` and `C$` are
  refused as ambiguous, with `next=` naming the ISO code.
- An optional 4-digit year may sit next to the code: `USD2015`,
  `USD 2015`, `USD(2015)`, `2015 USD`, `2015USD`. The year must be in
  1900..current UTC year.
- The remainder is everything else, e.g. `/t`, `/yr`, `/nozzle`, or
  empty.
- A currency taxon is one with `meta.dimension_kind == 'currency'`; failing
  that, one whose `canonical_unit` carries a currency token.

**Remainder through pint.** `_registry()` defines `money = [currency]`. The
converter swaps the currency token for `money` and lets pint scale the
remainder, so `EUR/kg` against `USD/t` gives ×1000 with the currency kept
as `EUR`. A remainder pint cannot parse (`/nozzle`) must match the
canonical remainder textually. A currency unit against a non-currency
canonical, or the reverse, keeps today's dimension refusal.

**Storage.** Store SI except money. `value_num`, `value_low`, `value_high`
and `value_err` hold the amount in the *reported* currency, with the
remainder scaled to the canonical remainder. New nullable columns:

- `measures.currency` (`^[A-Z]{3}$`)
- `measures.price_year` (smallint, 1900–2100)

A legacy row has `currency` NULL. Its effective currency is the token in
its `reported_unit`, else the one in its taxon's canonical unit. That is
correct for all 44 rows, so no write is needed to read them.

The frozen trigger (`precis_measures_frozen`, migration 0234) already
freezes every column it is not told about. Setting a price year on a
legacy row is a supersession, never an UPDATE.

`precis_measure_sha` gets one trailing `concat_ws` argument. The argument
is NULL when both new columns are NULL, so every existing sha, and every
review over it, stays unchanged.

**Conversion: FX first, then deflate.** For an amount A in currency C at
price year Y, the target is USD at year T:

    A × fx(C→USD, Y) × CPI_US(T) / CPI_US(Y)

- `fx(C→USD, Y)` is the ratio of the ECB annual-average quotes:
  `per_eur(USD, Y) / per_eur(C, Y)`, where `per_eur(EUR) = 1`.
- A price year is a year, so the annual average is used, never a point
  date.
- Display in EUR multiplies the USD value at T by `fx(USD→EUR, T)`.
- FX first needs only one CPI series (US) and prices the conversion when
  the cost was incurred. Deflating in the native currency first would need
  a CPI series per currency.

**Missing data never extrapolates.** The read path returns a
`MoneyConversion` (value plus the rate, rate year, day count, CPI years and
factor) or a `MoneyRefusal(reason, next)`:

- No price year: not converted. "price year unknown — shown as reported".
- C ≠ USD and Y < 1999: no ECB rate. A USD amount before 1999 still
  deflates.
- An ECB year with fewer than 50 quote days for C (ISK 2009–2017; late
  joiners): refused, naming the days found.
- Y or T outside the cached CPI years: refused with the covered range.
- The current partial year converts with a "year-to-date, N days" note.

**Canonical year.** The constant `CANONICAL_PRICE_YEAR` in `money.py` is set
at build time to the latest year with US CPI in the cache, likely 2025. It
moves only by commit, so stored-then-shown numbers never drift silently.
Labels read `USD(2025)/t`.

## Design — fetch, tables, reads

**Tables (one forward-only migration, slice 2).**

- `fx_rates(currency char(3), rate_date date, per_eur numeric > 0, source
  text, fetched_at timestamptz, PK (currency, rate_date))`. `per_eur` is the
  ECB quote: units of the currency per 1 EUR.
- View `fx_rates_annual(currency, year, per_eur_avg, n_days, first_date,
  last_date)`.
- `price_index(series text, year smallint, value numeric > 0, source text,
  fetched_at timestamptz, PK (series, year))`. The series is
  `wb:FP.CPI.TOTL:USA`.

These are caches, not append-only. An upsert updates a changed value (a CPI
revision) and bumps `fetched_at`. Reads recompute, so a revision applies
everywhere.

**Fetch.** Job type `money_refresh`, in `src/precis/workers/money_refresh.py`
plus `src/precis/workers/job_types/money_refresh.py`, is registered like
`news_poll`. It is deterministic and in-process (`claude_inproc` dispatch),
with params `{sources: ['ecb', 'worldbank']}`.

- ECB: `eurofxref-hist.zip`, the full daily history since 1999-01-04 in
  one GET. Cells hold `N/A` for gaps.
- World Bank: the v2 API, `country/USA/indicator/FP.CPI.TOTL?format=json`.
  It is keyless and annual from 1960.
- Both use `http_client(timeout=30)` + `polite_safe_get`. These are public
  source hosts in module constants (like `workers/weather.py`), not cluster
  addresses. Host rows auto-seed as `scraped-host`, so no outbound-policy
  migration is needed.
- Size caps: response ≤ 20 MB, unzipped ≤ 60 MB.
- One transaction per source. A fetch or parse fault records the job
  failure and leaves the tables untouched.
- A parse that yields fewer than 90 % of the rows already cached for that
  source is refused as a truncated download.
- The fetch is injectable (a `Fetch` callable, like weather), and tests use
  fixtures only.

**Cadence.** A recurring todo with `meta.schedule={'cron': '0 6 * * 1'}`
(Monday 06:00Z), `meta.executor='claude_inproc'` and
`meta.job_type='money_refresh'`. That todo is created in prod by the
coordinator at deploy, not by a build slice.

**Reads.** `store.money_tables()` loads `fx_rates_annual` + `price_index`
(about 1k rows), with a 1 h per-process TTL. Reads never touch the network.

**Display.**

- The unit, in order of precedence: explicit `unit=`, then the taxon's
  `display_unit` currency (the `EUR` taxon shows EUR), then
  `USD(CANONICAL_PRICE_YEAR)`.
- `price_year=` on `get`/`search(kind='measure')` overrides T.
- Every converted value shows the reported figure beside it:
  `1198 USD(2025)/t ← 1000 EUR(2015)/t · fx 1.110 (2015 avg, 256 d) · CPI
  2015→2025 ×1.33`.
- An unconvertible row prints as reported with the refusal reason. An
  explicit `unit=` on a single `get` that cannot convert raises `BadInput`
  with `next=`.

**Search across currencies.**

- `min=`/`max=` are amounts in `unit=` (currency, optional year token, and
  remainder) at `price_year=` (default T). They convert to canonical USD at
  T; a bound that cannot convert raises `BadInput`.
- For a currency measurand the SQL range clause is dropped. Rows are
  converted in Python and the range is applied there, within `scan_cap`.
- Unconvertible rows are left out of a ranged result and counted in a
  footer, with `next=` naming the reason.
- `best_measure` ranks a currency group on converted values; unconvertible
  rows are counted as `n_unconverted`.

## Slices

Each slice lands alone; none writes prod data. Take the next free
migration number at build time (0248 is free as of a821e9bfa). Slices 1–2
need the Opus review before land.

**S1 — currency as reported (store + show, no conversion).**

- `money.py` with `split_currency`, the `money` pint unit, and a currency
  branch in `make_converter`.
- `MeasureSpec.price_year`, plus `price_year` in `_measure_write.RunParser`.
- Migration: the two columns and the sha function.
- `_insert_one` sets `currency` and `price_year`:
  - A year given both in the token and as a field, with different values,
    is refused.
  - A currency taxon without a currency in `reported_unit` is refused,
    with `next="reported_unit='EUR2015/t'"`.
  - A missing price year is accepted, and the ack adds the line "no price
    year: shown as reported, not converted; pass price_year= when the
    source states it".
- Until S3, comparisons stay within one currency:
  - `search_measures` ranges match only rows in the bound's currency, and
    a footer counts the rest.
  - `best_measure` partitions by effective currency.
  - The outlier median in `_sanity_escalations` is computed per currency.
- `render.value_text` shows `1000 EUR(2015)/t`.
- Skill text goes in `precis-measure-help.md` and `precis-measures-help.md`.
- Tests: `tests/test_money_units.py` (pure: token forms, symbols, refusals,
  remainder scaling, `/nozzle`) and `tests/test_measures_currency.py` (dev
  DB):
  - insert stores the columns; conflict, no-currency and ack-hint cases;
  - same-currency range and footer; `best_measure` partition;
  - a legacy NULL row reads as its canonical currency;
  - the sha is unchanged for a NULL row; the frozen trigger refuses an
    UPDATE of `price_year`.

**S2 — rate tables + `money_refresh` job.**

- Migration for `fx_rates`, `fx_rates_annual` and `price_index`.
- Pure parsers: `parse_ecb_hist(bytes)` and `parse_worldbank_cpi(json)`.
- The upsert, the truncation guard and the job-type registration.
- Fixtures go in `tests/fixtures/money/`:
  - `eurofxref-hist-sample.zip`: covers 2012-06-29, a weekend gap, `N/A`
    cells and the ISK gap.
  - `worldbank-cpi-usa.json`.
- Tests: `tests/test_money_refresh.py` covers the parse; idempotent and
  revision upserts; truncation refusal; a fetch fault leaving the tables
  unchanged; size caps; and the job's dispatch with an injected fetch. No
  network.

**S3 — conversion + display.**

- `MoneyTables`, `convert_money` and `CANONICAL_PRICE_YEAR`.
- `store.money_tables()`.
- Wire the conversion into the measure `get`/`search` rendering and the
  quest `view='measures'`; add the `unit=`/`price_year=` args.
- Tests: `tests/test_money_convert.py` (pure, fixed tables):
  - EUR 2015 → USD 2025 and → EUR 2025;
  - USD 1990 deflates; EUR 1995 is refused;
  - CPI out of range is refused with the range; the ISK-gap refusal; the
    partial-year note; no price year.
  - Plus `test_measure_kind.py` cases for converted-plus-reported output
    and the explicit-unit refusal.

**S4 — cross-currency search and ranking.** Converted range filtering,
bound conversion, the unconverted footer, `best_measure` on converted
values (this drops the S1 currency partition when tables are present).
Tests: S4 cases in `tests/test_measures_currency.py` (a mixed EUR/USD range,
an unconvertible count, the ranking order).

**S5 — backfill dry list (read-only).** `scripts/measure-price-year-dry`
lists each live currency row with: id, measurand, literal, `reported_unit`,
source paper year, and a candidate price year with its evidence span. The
candidate comes from regexes over the anchor chunk (`2015 USD`, `US$2015`,
`in 2015 dollars`, `2015 prices`). The script writes TSV to `scratch/` and
runs in a read-only transaction. Applying a year is a supersession done
after Reto's typed go, outside these slices. Tests:
`tests/test_price_year_candidates.py` (regex cases; the script performs no
writes).

## In scope

The five slices above: currency tokens and price years on measures, the FX
and CPI caches and their weekly fetch, convert-on-read display, cross-currency
search and ranking, and a read-only backfill candidate list.

## Explicitly NOT in scope

- Live or intraday FX, point-date conversion, PPP, a per-user display
  preference store (the taxon `display_unit` and `unit=` cover it for now).
- Pre-1999 FX (DEM, FRF, …) and currencies the ECB does not quote.
- Guessing a price year: legacy rows stay unknown until a reviewed
  supersession sets one.
- Any prod write in a build slice: the backfill apply, merging the EUR
  taxon 517414 into its USD sibling 517413, and creating the recurring
  todo.
- Re-extracting the TEA figures (follow-up on td513645).

## Acceptance criteria

- `insert_measure` stores a cost's currency and price year as reported;
  `EUR2015/kg` on a `USD/t` taxon stores the amount ×1000 with `currency=EUR`
  and `price_year=2015`.
- Existing shas are unchanged by the migrations, and the 44 legacy rows
  read as before.
- The weekly job fills `fx_rates`/`price_index` from fixtures in tests;
  reads never call the network; fetches use `polite_safe_get` with
  timeouts and size caps.
- A EUR 2015 figure reads as USD at the canonical year, showing the rate,
  the rate year and day count, the CPI years and the factor, beside the
  reported figure.
- No price year, pre-1999 non-USD, a thin ECB year and out-of-range CPI
  each refuse with a reason and `next=`, never extrapolating.
- `search(kind='measure', property=…, min=, max=, unit='EUR/t')` matches
  across currencies on converted values and counts the unconverted rows.
- `scripts/measure-price-year-dry` produces a list and writes nothing.

## Target + blast radius

`src/precis/taxonomy/money.py` (new), `taxonomy/measure_units.py`,
`store/_measures_ops.py`, `handlers/measure.py`, `_measure_write.py`,
`_measure_render.py`, two migrations, the new worker and job type, the
two measure skills, and `scripts/measure-price-year-dry`. Non-currency
units are unchanged.

## Open questions / decisions log

Decided:

- **Price year.** Both a field and a token are accepted; the field wins and
  a conflict is refused.
- **Annual average.** A price year converts at the ECB annual-average rate.
- **Default display.** USD at `CANONICAL_PRICE_YEAR`; a taxon's
  `display_unit` or `unit=` overrides it.
- **Pre-1999.** Refused for non-USD amounts.
- **CPI series.** US only (`wb:FP.CPI.TOTL:USA`), because FX-first needs
  no other series.
- **Order.** FX first, then deflate.
- **Source.** The ECB file directly, not Frankfurter.

Non-blocking (defaults apply unless Reto overrides):

- `CANONICAL_PRICE_YEAR`: the latest cached US CPI year, fixed by commit.
- Missing price year: accept and show as reported, rather than refuse at
  insert.
