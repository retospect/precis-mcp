---
status: draft
title: Currency units for measures — EUR canonical, stored as reported, converted on read
pillar: quests
---

# Currency units for measures

Origin: gr510555. Decision (Reto 2026-10-24): **EUR is canonical.**

## Motivation / why

Costs are stored as the opaque string `USD`: no price year, no conversion, no
EUR. Damage and cost figures (climate-quest cost per tonne, disaster losses,
MCB cost per °C) mix EUR and USD across 2015–2026 price years, so they can
be neither compared nor summed. Consumer: the MCB TEA strand (td513645)
mixes USD and EUR today.

## Design

- **Store as reported.** A currency measure keeps its original amount, ISO
  currency code and price year. Nothing is rewritten at insert.
- **Convert on read.** The canonical unit is EUR (Reto 2026-10-24; the
  gripe body's "canonical USD" is superseded). Display converts the stored
  amount via a dated FX rate (currency → EUR). Display currency can be EUR
  or USD per user preference.
- **FX source (approved, Reto 2026-10-24).** Frankfurter
  (`api.frankfurter.app`): no key, ECB daily reference rates from 1999.
  - Rates are cached in a local table; reads never call the API.
  - The fetch goes through `safe_get` (`src/precis/utils/safe_fetch.py`)
    with an outbound-policy row for the Frankfurter host
    (`src/precis/utils/outbound_policy.py`).
  - A weekend or holiday date resolves to the previous business day and
    the read reports that date. Example: USD→EUR on 2012-07-01 returns
    0.79428 dated 2012-06-29.
  - Yearly averages come from range queries. Example: 2012 = 0.7788 over
    257 days.
  - Pre-1999 values have no source yet (open question 4).
- **Price-year deflation (approved, Reto 2026-10-24: "cpi table is good.
  if we can get it.").** A cached, dated deflator table sits beside the FX
  cache table so cost figures normalise to one target price year.
  - Sources are free and keyless: Eurostat HICP (keyless API) for EUR;
    World Bank CPI for USD and other currencies. FRED needs a key, so it
    is not used.
  - Each measure stores its price year as reported.
  - Read order: FX at the value's date first, then deflate to the target
    price year. After conversion the value is EUR, so the index that
    applies is HICP.
  - The other order (deflate in the native currency with its own CPI, then
    convert at the target-year FX rate) is possible and was not chosen:
    it needs a CPI series per source currency, and it prices the
    conversion at the target year rather than when the cost was incurred.
    FX-first needs one HICP series for every currency.
  - Fetches go through `safe_get` with outbound-policy rows in
    `src/precis/utils/outbound_policy.py`; reads never call the network.

## In scope

- Currency as a unit dimension: parse `EUR`, `USD`, `€`, `$` (plus price
  year suffix, e.g. `USD2015`, or a separate `price_year` field).
- `src/precis/taxonomy/measure_units.py`: today USD is a non-pint unit with
  no conversion (see the "not a pint unit" notes in `to_canonical`
  and the same-unit shortcut in the converter). Add a currency branch that
  converts through the cached FX table instead of pint.
- `src/precis/store/_measures_ops.py`: carry currency + price year on the
  stored measure; `_measures_ops` unit-compat minting currently skips
  USD with a log line — currency units mint without a compat row and are
  converted on read.
- A forward-only migration for the FX cache and deflator tables and any new measure
  columns (never edit a sealed migration).
- Backfill: existing `USD` rows get currency `USD` and price year `NULL`
  (unknown) rather than a guessed year.

## Explicitly NOT in scope

- Live per-read FX or intraday rates; PPP adjustment.
- Guessing a price year for legacy rows (they stay flagged unknown and are
  excluded from cross-measure sums until set).
- Re-extracting the TEA figures; that is a follow-up on td513645.

## Acceptance criteria

- `insert_measure` accepts a cost with currency and price year and stores
  them as reported.
- A read in EUR of a USD figure returns the EUR amount with the rate and
  the rate's actual date shown (previous business day on a weekend).
- Reads never hit the network; the fetch path uses `safe_get`.
- A pre-1999 value is not silently converted; the read says so.
- A read with a target price year returns the EUR amount converted at the
  value's date, then deflated by HICP, showing the rate date, the index
  years used and the target year.
- A measure with no stored price year is not deflated; the read says so.
- A year outside the cached CPI/HICP coverage is refused with the covered
  range, not extrapolated.
- Pure-function tests for conversion and deflation with fixed FX and
  deflator tables.

## Target + blast radius

`src/precis/taxonomy/measure_units.py`, `src/precis/store/_measures_ops.py`,
one migration, the measure display path (`display_numbers`, `format_value`),
and the `measure` kind skill text. Existing non-currency units unchanged.

## Open questions / decisions log

1. Price year as a field vs part of the unit token (`USD2015`).
2. Annual-average (range query) vs point-date FX for a price-year-only
   value.
3. Default display: EUR always, or per-user preference from day one?
4. Pre-1999 values: no source yet (Frankfurter starts 1999); legacy
   rates from another source, or refuse?
5. CPI coverage: series start dates differ (HICP euro area, World Bank
   per country); the earliest price year supported and the treatment of
   gaps are unset.
6. Which World Bank CPI series (country vs currency mapping), and when it
   is used given FX-first reads use HICP (display in USD, alternative
   order).

Decided: FX = Frankfurter/ECB, cached locally; deflation = Eurostat HICP
plus World Bank CPI, cached locally, FX-first order; all fetches via
`safe_get` (Reto 2026-10-24).
