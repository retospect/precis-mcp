---
status: draft
title: retire fi as the claim surface — nanopublications all the way
---

# retire fi as the claim surface — nanopublications all the way

<!-- Direction decided with Reto 2026-08-16. Staged; each stage is its
own worktree/ship. This item is the umbrella + the migration criterion;
slice out per-stage items as they become concrete. -->

## Motivation / why
The finding hub and the nanopub publish row are two names for the same
claim, and only one can be the citable surface long-term. The nanopub
is the better one: signed, content-addressed, timestamped, publicly
verifiable. Retiring `[fi<id>]` in favor of `[np<id>]` also forces
every claim through freeze-at-review — the choke point where wording
gets made concise and crisp (today's hub titles are often bloated,
compound, or carry attribution in the sentence).

**What retires is fi as the *surface*** (citation syntax, browse/read
affordance for world-claims), not the engine: evidence edges, refine,
trust derivation, and dispute detection keep running on hubs
underneath. And the fi population is two species — world-claims
(migrate to nanopubs) and process findings (drift/inflation critiques,
citation-miss records — QA about our prose, never mintable; re-home
closer to gripes).

## In scope
- **`[np<id>]` citation syntax** on the *publish-row id* — stable
  across re-signs (trusty URI changes, row persists), exists pre-
  publication (embargoed claims citable). Export resolves it through
  the same primary-source crawl `[fi]` uses, plus the artifact for the
  appendix (first slice — SHIPPED: `precis.export._nanopub_appendix`,
  "Published claim artifacts" end-matter in both exporters).
- **Crispness as a mint gate**, not exhortation — extend Layer-A
  schema lint (`src/precis/nanopub/gates.py`): length cap (~200
  chars), no attribution in the assertion ("by X et al.", "The
  authors", "The passage"), no compound multi-clause sentences, the
  scope's quantity present in the sentence. The migration sweep then
  IS the editorial rewrite pass.
- **Migration sweep**: per draft-cited hub — crisp rewrite → approve
  (freeze) → rewrite `[fi<hub>]` → `[np<pubrow>]` across drafts.
  Mechanical, incremental, resumable.
- **The migration criterion — two counters, both to zero:**
  1. hub coverage: claim hubs cited by live drafts with no publish
     row in state ≥ `reviewed` (the `nanopub.overview` query is the
     dashboard — no new build);
  2. prose coverage: `[fi<id>]` handles remaining in live drafts
     (draft-lint counter, `handlers/_draft_lint.py`).
  Migration bar is `reviewed` (frozen string), NOT `signed` — signing
  is human-paced (attesting key by design) and proceeds
  opportunistically; requiring it would hostage the sweep's tail to a
  signing marathon.
- **Endgame** (only at zero-zero): retire `[fi]` from the citation
  grammar for world-claims; re-home process findings.

## Explicitly NOT in scope
- Removing the finding kind, hub tables, or taproot machinery —
  evidence/refine/trust stay the engine under publish rows.
- Auto-publishing: registry POST stays triple-gated and human.
- Bulk LLM rewrite-and-approve without review — approve is the
  editorial checkpoint; batch tooling may draft rewrites, a human (or
  the review tier the mint flow already trusts) approves.
- Migrating process findings into nanopubs — never mintable.

## Acceptance criteria
- Prose can cite `[np<id>]` and it exports identically to the
  equivalent `[fi<id>]` (same bibliography entries, same trust marks)
  plus appendix entry when minted.
- Mint gate rejects: >~200-char sentences, attribution-in-assertion,
  compound claims; existing crisp claims (e.g. fi191001) pass.
- The two counters are visible somewhere cheap (overview page or CLI)
  and a migration pass demonstrably moves both toward zero on the
  nanobuds draft (dr173020) as the pilot.
- `[fi<id>]` cites keep working throughout — retirement is the last
  step at zero-zero, not a flag-day break.

## Target + blast radius
`src/precis/nanopub/gates.py` (style gate), export cite resolution
(`src/precis/export/latex.py` + `_trust_marks.py` sibling helper),
draft lint (`src/precis/handlers/_draft_lint.py`), `nanopub.overview`,
the migration sweep as a CLI verb or job type. Wide but staged; no
schema change expected (publish rows exist, 0129/0130 shipped).

## Open questions / decisions log
- Decided: cite anchor = publish-row id, not trusty URI or hub id.
- Decided: migration bar = `reviewed`; signed/published is
  opportunistic.
- Open: where process findings land (gripe? internal-only finding
  status?) — decide at endgame, nothing blocks on it.
- Open: does `[np<id>]` render visibly differently from `[fi<id>]` in
  export body text (e.g. inline "np" mark), or identically with the
  appendix as the only tell?
