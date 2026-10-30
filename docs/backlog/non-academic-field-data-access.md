---
status: draft
title: Access to non-academic field-monitoring/geochemistry datasets for quest evidence — acquire/license decision
pillar: quests
---

# Non-academic field-monitoring data access (gr539647)

Source gripes: gr539647.

A `reto:ask`, not a build item: qu509651 ticks filed
`needs-tool: access-to-non-academic-field-monitoring-geochemistry-dataset`
and the recommend comment is "decide whether to acquire/license access
to EPA/USGS-style field-monitoring or geochemistry datasets for this
quest line".

## Motivation / why

qu509651's carbon-mineralization pathway needs chromium/nickel
mobilization data from chrysotile/ultramafic tailings during carbonation
to fill the measured side-effects rubric axis. The academic corpus is
exhausted: two independent literature-search strategies
(lab-carbonation-review and field-pile/leachate-monitoring framings)
plus ~15 in-graph search variants (pa510153, pa509678, pa58239) surfaced
zero quantified Cr/Ni leachate data. Field-monitoring data lives in
non-academic sources — EPA/USGS-style datasets the paper corpus does not
mirror — so the chromium-mobilization thread stays permanently frozen
unless the quest gets a lane to that data or a ruling that the axis
accepts a bound instead of a measurement.

## In scope

- The decision itself: acquire/license access to non-academic
  field-monitoring/geochemistry datasets for this quest line, or
  decline and mark the side-effects axis a permanent data gap.
- If acquire: which sources (open EPA/USGS products vs licensed
  datasets), and the lane a quest tick uses to reach them.

## Explicitly NOT in scope

- Building an ingest pipeline before the decision. No lane exists
  today; shape follows the ruling.
- Academic-literature search improvements — the corpus is confirmed
  exhausted for this datum; more search strategies are not the fix.
- Other quests' data needs beyond a note that the same decision likely
  generalises (cluster tag `quest-data-lookup`).

## Acceptance criteria

- A decision is recorded in this item's decisions log: acquire (naming
  source and lane) or decline (naming what the side-effects axis
  carries instead).
- If acquire: a qu509651 tick can cite a quantified Cr/Ni leachate
  figure from the named source, closing the gripe's `needs-tool` gap.
  (TBD — the cite shape depends on the lane the ruling picks.)
- If decline: the carbon-mineralization side-effects axis carries an
  explicit data-gap mark and the chromium-mobilization thread is closed
  or re-scoped on record.

## Target + blast radius

Decision only at filing. Depending on the ruling: quest data-lookup
surface (whatever serves non-corpus evidence to a tick) and possibly an
ingest lane for tabular/agency data — today the corpus is papers.
Prod-side spend only if the ruling picks a licensed source.

## Open questions / decisions log

1. **Acquire or decline?** Options: (a) open EPA/USGS field-monitoring
   and geochemistry products (no license cost; coverage of
   chrysotile-tailings carbonation leachate unverified); (b) a licensed
   geochemistry dataset (cost; needs a named product); (c) decline —
   the axis takes a qualitative bound and the thread closes as
   data-gap.
2. **Which lane serves non-academic data to a tick if acquired?**
   Options: ingest as corpus documents, a `measures`/finding write from
   a one-off extraction, or a per-quest data attachment. The corpus
   pipeline assumes papers; tabular agency data may not fit it.
