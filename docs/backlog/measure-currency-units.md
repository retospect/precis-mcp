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
- **Convert on read.** The canonical unit is EUR at a stated price year
  (default: latest deflator year). Display converts via a dated FX rate
  (currency → EUR at the price year) and a deflator (price year → target
  year). Display currency can be EUR or USD per user preference.
- **FX and deflator source (open).** Proposal: ECB euro reference rates for
  FX (annual average for annual price years) and Eurostat HICP (euro area)
  as the deflator. USD figures: FX at the price year, then HICP to the
  target year. Both tables are small and dated; store them as a seeded
  reference table, not a runtime fetch.

## In scope

- Currency as a unit dimension: parse `EUR`, `USD`, `€`, `$` (plus price
  year suffix, e.g. `USD2015`, or a separate `price_year` field).
- `src/precis/taxonomy/measure_units.py`: today USD is a non-pint unit with
  no conversion (see the "not a pint unit" notes in `to_canonical`
  and the same-unit shortcut in the converter). Add a currency branch that
  converts through the FX/deflator table instead of pint.
- `src/precis/store/_measures_ops.py`: carry currency + price year on the
  stored measure; `_measures_ops` unit-compat minting currently skips
  USD with a log line — currency units mint without a compat row and are
  converted on read.
- A forward-only migration for the FX/deflator table and any new measure
  columns (never edit a sealed migration).
- Backfill: existing `USD` rows get currency `USD` and price year `NULL`
  (unknown) rather than a guessed year.

## Explicitly NOT in scope

- Live FX or intraday rates; PPP adjustment.
- Guessing a price year for legacy rows (they stay flagged unknown and are
  excluded from cross-measure sums until set).
- Re-extracting the TEA figures; that is a follow-up on td513645.

## Acceptance criteria

- `insert_measure` accepts a cost with currency and price year and stores
  them as reported.
- A read in EUR of a USD-2015 and a EUR-2024 figure returns comparable
  numbers at one stated price year, with the FX and deflator year shown.
- A row with unknown price year is not silently converted; the read says so.
- Pure-function tests for conversion with a fixed FX/deflator table.

## Target + blast radius

`src/precis/taxonomy/measure_units.py`, `src/precis/store/_measures_ops.py`,
one migration, the measure display path (`display_numbers`, `format_value`),
and the `measure` kind skill text. Existing non-currency units unchanged.

## Open questions / decisions log

1. FX/deflator source: ECB reference rates + Eurostat HICP proposed; OECD or
   World Bank as alternative. Reto to confirm.
2. Price year as a field vs part of the unit token (`USD2015`).
3. Annual-average vs year-end FX for a price year.
4. Default display: EUR always, or per-user preference from day one?
5. Pre-1999 years (no euro): use ECU/legacy rates, or refuse?
