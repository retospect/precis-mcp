---
status: draft
prio: high
title: DOI-completeness check for a draft's cited papers — flag cites with no DOI, offer a fetch (rides the retraction walk)
---

# DOI-completeness check for a draft's cited papers

## Motivation / why
A pre-submission pass on `dr173020` (nanobuds) shipped clean on house-style,
coherence, notation, and retraction — but a co-author caught by eye that many
cited papers carried no DOI. Journals want a DOI on every reference; a missing
one is a copy-edit defect that no current check surfaces, so it falls to a human
eyeballing the bibliography. That is exactly the mechanical, per-citation work
the retraction scan already automates, and DOI presence is *cheaper* to detect
than retraction state — it is a column on the source ref, knowable with zero
network — so there is no reason it should be a manual step.

The retraction module (`src/precis/export/retraction.py`) already owns the hard
primitive: `cited_paper_refs(store, ref)` renders the body once, resolves every
cited slug to its source `ref` row exactly once, and returns `(refs,
unresolved)`. A `ref` row carries a `doi` column right beside
`retraction_status`. DOI-completeness is a second signal read off that same
walk — one press, one report, two independent signals — so the two can never
disagree about what a draft cites (the same invariant that doc already states
for export-gate vs watch-button).

The key asymmetry vs retraction, which keeps this small: retraction detection
*needs* a TTL-gated network check and the whole read-vs-check split is built
around it. DOI **absence** needs none of that — it is a pure read. Only the
optional *fix* (resolve a missing DOI by title/author) touches the network, and
that machinery already exists in `ingest/paper_meta_enrich.py` +
`ingest/crossref.py` / `ingest/openalex_meta.py`.

Two signals off one walk (decided — see log): DOI **presence** is a pure read;
DOI **validity** is a network check that mirrors retraction's read-vs-check
split exactly, down to its own `doi_validated_at` stamp.

## In scope
- **Presence (pure read).** Over a draft's cited source refs (reusing
  `cited_paper_refs`), partition each cite by identifier state:
  - **has DOI** — clean.
  - **no DOI but has another id** (arxiv / s2 / pubmed) — fetchable; DOI is
    resolvable from the id it does have.
  - **no persistent identifier at all** — the worst bucket; typically a
    whole-paper `[pa]` stub imported from a title (the 5 foundational nanobuds
    cites: Kroto, Krätschmer, Iijima, peapods, Nasibulin were this shape).
- **Validity (network check, stamped).** A "validated" marker + a
  `doi_validated_at` timestamp on the ref, mirroring `retraction_status` /
  `retraction_checked_at`: the check resolves the DOI upstream (doi.org /
  Crossref) and records whether it resolves, and when it was last asked. Like
  retraction, **"never validated" is a first-class outcome** distinct from
  "validated and fine" — the pane surfaces it rather than rounding a blank stamp
  down to valid. Presence is available with zero network; only validity spends a
  round-trip, so the export path reads the stored stamp and never blocks on a
  never-validated DOI.
- **Shared trigger.** Both signals ride `cited_paper_refs`, so the existing
  retraction-watch button can validate DOIs in the same network pass over the
  same refs — one press, retraction + DOI validity both re-checked, same
  `select_for_check` per-press budget and TTL gate.
- Surface the count in the seams the retraction report already feeds:
  - fold a `missing_doi` signal onto the same walk so the draft's
    retraction-watch pane / export summary reads "N cited papers — 1 retracted,
    3 missing DOI, 1 never checked" from one pass;
  - a line in the `citations` lifecycle view
    (`src/precis/handlers/_citations_view.py`);
  - a line in the Hygiene footer (`view='hygiene'`), beside the
    undefined-abbreviation / whole-paper-cite counts.
- Soft, never blocking. Missing DOI annotates the export the way
  `SOFT_STATUSES` (corrigendum / expression-of-concern) do — a PDF still
  compiles. Only `retracted` blocks; a missing DOI is fix-it, not stop-ship.
- **Phase 2 (optional, mirrors the retraction button):** a "fetch missing DOIs"
  action that resolves the fetchable bucket by title+author via Crossref /
  OpenAlex, TTL-gated exactly like `check_refs_retraction`, writing the `doi`
  back onto the ref. The report still covers every cite while the network walk
  narrows to the neediest (same `select_for_check` budget pattern).

## Explicitly NOT in scope
- Export-gating on a missing or unvalidated DOI. Advisory only; a hard gate can
  be a later, separate toggle once false-positive rate is known (some
  legitimately DOI-less sources exist: preprints, datasets, theses, standards).
- **Deep metadata correctness.** Validity here is resolves / does-not-resolve
  (the DOI dereferences upstream), NOT "the resolved title/authors match the
  ref." A resolve-vs-mismatch distinction is a natural follow-on but is kept out
  of the first cut so the cheap presence+resolve win ships clean.
- Minting DOIs. This finds and fills from upstream metadata; it never invents
  an identifier.
- Non-paper cites (`[fi]` claim-hub findings, `[dc]` internal cross-refs) — a
  finding has no DOI by nature. Scope is the paper/whole-paper cite set that
  `cited_paper_refs` already enumerates.

## Acceptance criteria
- On a draft citing a paper with a `doi`, a paper with only an `arxiv` id, and a
  `[pa]` stub with neither: the report lists one clean, one "no DOI (fetchable
  from arxiv)", and one "no persistent identifier", with each `dc`/slug handle.
- The counts appear in `view='hygiene'` and `view='citations'`; a draft whose
  cites all have validated DOIs shows an all-clear line.
- A never-validated DOI reports as its own state (not folded into "valid"); a
  ref validated at time T shows the `doi_validated_at` stamp.
- Pressing the retraction-watch button validates DOIs in the same pass: a
  previously never-validated resolving DOI comes back stamped valid; a DOI that
  does not dereference comes back flagged invalid.
- The signals come off the *same* `cited_paper_refs` walk the retraction report
  uses (no second body render, no divergent notion of "what the draft cites").
- Neither a missing nor an unvalidated DOI changes `blocks_export`.
- (Phase 2, if built) pressing "fetch missing DOIs" resolves the arxiv-only cite
  to a DOI and re-reads clean; the no-identifier cite stays flagged.

## Target + blast radius
`src/precis/export/retraction.py` — extend the walk/report in place (decided:
one walk, `CitedPaper` gains `doi` + validity + `doi_validated_at`, the report
gains `missing_doi` / `unvalidated` partitions; rename the report if
"retraction" reads too narrow for what it now carries).
`src/precis/handlers/draft.py` (Hygiene footer) +
`src/precis/handlers/_citations_view.py` (lifecycle line);
`src/precis_web/routes/drafts.py` (pane/summary text, watch-button pass, phase-2
button). Presence reads the existing `refs.doi` column. **Validity needs a
forward-only migration** adding `doi_validated_at timestamptz` (+ a validity
marker) on `refs`, beside `retraction_checked_at`. Validation + phase-2 fetch
reuse `ingest/paper_meta_enrich` + `crossref` / `openalex_meta` and the
`check_refs_retraction` TTL/`select_for_check` pattern.

## Open questions / decisions log
- **Home for the walk — DECIDED (author, 2026-08-12):** one walk. Extend
  `retraction.py`'s `CitedPaper` / `draft_retraction_report` with the DOI fields
  rather than a sibling module — the walk, the pane, and the export summary are
  already one code path; a parallel module would re-plumb all three consumers
  for no gain.
- **Validity marker shape:** boolean valid/invalid, or a small status text
  (`valid` / `not_found` / `unchecked`) like `retraction_status`? Leaning status
  text for parity and to leave room for a later `mismatch` value without another
  migration. Decide at implementation.
- Which id-kinds count as "fetchable" for phase 2 (arxiv + pubmed clearly;
  s2 / title-only?) — gate on what `paper_meta_enrich` can actually resolve.
- Should preprint / dataset / thesis entry-types be exempt from the flag (no
  DOI expected) or flagged-but-labelled? Default: flag with an entry-type note,
  don't silently exempt — the author decides.
