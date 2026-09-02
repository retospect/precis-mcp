---
status: draft
title: source-trust prior — rank papers by trustworthiness signals, without silencing small voices
---

# Source-trust prior

Reto, 2026-09-02: *"we should also rank the papers by trustyness, but not
reject hidden small voices entirely."*

## Motivation / why

Several passes need an ordering over sources — which candidate to spend
LLM verification on first (`docs/backlog/claim-conflict-search.md`), how
to weight conflicting evidence in display, how to order search hits.
Today they order by cosine distance only, which treats a retracted paper
and a heavily-replicated one identically. But the constraint is as
load-bearing as the ranking: genuine dissent disproportionately comes
from low-prestige venues, preprints, and thinly-cited work — the "hidden
small voices." A trust score used as a *filter* would systematically
suppress exactly the disagreement the conflict-search pass exists to
surface. So: **trust is a prior for ordering and weighting, never an
admissibility test.**

## In scope

1. **Score.** A per-paper trust score on `refs.meta.trust = {score,
   components, at}`, computed from signals we already hold or already
   fetch:
   - retraction/correction status (`refs.retraction_status`, ingested via
     Crossref — retracted ranks bottom and renders flagged, but stays
     visible: a conflict raised by a retracted paper is still worth a
     human glance, marked as such);
   - citation count / influential-citation count (semanticscholar kind);
   - publication state: published vs preprint (ties into
     `docs/backlog/preprint-to-published-cite-upgrade.md` — an upgraded
     cite should lift the score);
   - venue/publisher metadata where held.
   Components stored individually so consumers can re-weight without
   recompute.
2. **Compute path.** An enrich-style worker pass (cheap, no LLM) over
   held papers; recompute when a component changes (new citation fetch,
   retraction status flip) or on a slow cadence.
3. **Consumption rules — the contract, enforced wherever the score is
   read:**
   - ordering and weighting only; no consumer may drop a candidate solely
     on trust;
   - budgeted passes using trust ordering must reserve a floor fraction
     of spend for below-median-trust candidates (owned and tested by
     `claim-conflict-search`);
   - display may badge trust (and must badge retraction) but a low score
     renders as low-prominence, never as absent.
4. **Read surface.** Score visible on the paper page and available to
   search ranking as an optional re-rank signal (off by default until
   evaluated — search relevance changes are their own risk).

## Explicitly NOT in scope

- Author-level reputation or identity scoring — papers only.
- External trust services / altmetrics feeds — held signals only.
- Any change to admissibility or publication gates — trust never gates.
- LLM-judged "methodological quality" scoring — signals here are
  mechanical and auditable; a judged-quality axis would be its own item.

## Acceptance criteria

- Held papers carry `meta.trust` with per-component values; a retraction
  status flip changes the score on the next pass.
- A retracted paper ranks bottom in any trust-ordered list, renders with
  a retraction flag, and is still present in conflict-candidate lists.
- The consumption contract is written into the consuming code's owning
  docstrings and the relevant skills (`precis-nanopub-help`,
  search-related skills), stating the no-filter and floor rules.
- `claim-conflict-search`'s floor-rule test passes against a fixture
  where high-trust candidates would otherwise exhaust the verify budget.

## Target + blast radius

- New enrich worker pass in `src/precis/workers/` (no LLM), registered in
  `workers/registry.py`; `refs.meta.trust` writes on paper refs.
- Consumers: `claim-conflict-search` ordering; paper page render;
  optional search re-rank hook (dark until evaluated).
- Skills: consumption contract lines in the affected product skills.

## Open questions / decisions log

- Score shape: single scalar with components, or components-only with
  consumer-side weighting? (Leaning scalar + components — consumers that
  just want an ORDER BY get one, careful consumers can re-weight.)
- Citation-count normalization: raw counts favour old papers; age-adjust
  or leave raw with the component exposed?
- Does venue metadata coverage in the current corpus justify a venue
  component in slice 1, or is it citation+retraction+pub-state first?
