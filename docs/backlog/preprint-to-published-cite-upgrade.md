---
status: idea
title: Upgrade a cited preprint to its published version in the taproot, soft-retiring the preprint
prio: normal
---

# Upgrade a cited preprint to its published version in the taproot, soft-retiring the preprint

## Motivation / why
A cited **preprint** is a permanent hole in provenance. It carries no Crossref
DOI (only an arXiv DataCite mint `10.48550/arXiv.*`, or none at all), so the
retraction watch — which is Crossref-keyed — can *never* form an opinion on
it: it stays "never checked" forever. Concretely, this is what makes the
retraction-watch button on a preprint-heavy draft look stuck (see draft
173020, 2026-08-12: most DOI-less cites are legitimate preprints with no DOI
to check; the check re-walks them because there is nothing to stamp). A DOI
backfill can't fix it either — ~58% of DOI-less papers are preprints with **no
published DOI in existence yet** ([[abstract-fallback-crossref-s2]] touches the
same S2/Crossref lookup surface).

When the peer-reviewed **published** version *does* exist, we want it instead:
it carries the authoritative DOI, the final metadata, and — critically — the
retraction/correction record. The taproot should cite the version of record,
not the preprint, once the version of record is available.

## In scope
- **Detect** that a cited source is a preprint: has an `arxiv` identifier, or
  its only DOI is an `10.48550/arXiv.*` DataCite mint, or it has no DOI.
- **Discover** whether a published version with a real (Crossref) DOI exists —
  S2 frequently links preprint↔published in `externalIds` /
  `publicationVenue`; otherwise a similarity-gated title match (reuse
  `ingest/metadata_resolve` gates: >=0.85 auto, year-compatible, DOI not owned
  by another ref).
- **Ingest/resolve** the published paper as a first-class `paper` ref (dedup
  against any existing ref for that DOI).
- **Rewire the cite**: the taproot claim / draft cites the published paper
  instead of the preprint (via the shared resolver in
  `precis/taproot/cite.py` — `finding_cite_keys` / hub cite resolution — so
  the upgrade is honored wherever the cite is rendered).
- **Soft-retire the preprint ref**: mark it superseded and link
  `superseded-by` → published ref (keep it for provenance / historical
  chains; do not hard-delete).

## Explicitly NOT in scope
- Hard-deleting preprint refs, or touching preprints that have **no** published
  version (legitimately preprint-only work — leave them, and let the retraction
  button skip un-checkable cites gracefully; that is a separate fix).
- Changing the retraction check itself.
- Auto-rewriting an author's literal draft prose — the cite *target* moves; the
  visible citation text follows the normal render.

## Acceptance criteria
- Given a taproot claim / draft that cites a preprint whose published version
  is discoverable, after the pass the cite resolves to the **published** paper
  (real Crossref DOI), the preprint ref is soft-retired with a `superseded-by`
  link to the published ref, and the retraction watch can now check that cite
  (it stamps instead of staying "never checked").
- A preprint with no discoverable published version is left untouched (no
  false upgrade, no churn).
- The upgrade is idempotent — re-running does not create duplicate published
  refs or re-fire the supersede.

## Target + blast radius
- `precis/taproot/cite.py` (cite resolution / hub keys) — where the rewired
  cite must be honored.
- `precis/ingest/metadata_resolve.py` (similarity-gated title→DOI) and/or a new
  registered worker **PASS** (`workers/registry.py`) for the discovery+upgrade
  sweep; S2 lookup via `ingest/semantic_scholar.py` (`get_paper_by_id` /
  `externalIds`).
- `store` identifiers + a `superseded-by` / `supersedes` relation and a
  soft-retire state on `refs`.
- Read beneficiaries: `export/retraction.py`, the smartdraft retraction pane.

## Open questions / decisions log
- **Trigger surface:** a background worker PASS over cited preprints, vs. an
  on-demand action from the draft/taproot UI, vs. at cite-resolve time. A sweep
  is least intrusive; on-demand gives the author control.
- **Preprint↔published linkage confidence:** prefer S2's explicit
  externalIds/venue linkage; fall back to title match only at the auto
  threshold. Never upgrade on a weak match (a wrong upgrade silently rewrites a
  citation — worse than leaving the preprint).
- **What "soft retire" means precisely:** a `STATUS:` tag + supersede link and
  keep the ref, vs. `deleted_at` with the link. Leaning tag + link so existing
  chains/findings that referenced the preprint stay resolvable.
- **Author consent:** silent upgrade vs. surface "N cited preprints have a
  published version — upgrade?" in the pane. The version of record is almost
  always what the author wants, but a surprise cite change warrants at least a
  visible note.
