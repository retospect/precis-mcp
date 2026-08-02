---
status: built
title: Smartdraft per-paragraph review status — incremental re-check, checker matrix UI, document rollup
---

# Smartdraft per-paragraph review status — incremental re-check, checker matrix UI, document rollup

## Motivation / why

The rung-3 review substrate is largely built: the `chunk_review` ledger
(mig 0086) stores one `(chunk, checker)` approval watermarked at
`approved_sha`; "dirty" is *derived* (`approved_sha IS DISTINCT FROM
chunks.content_sha`), so any paragraph edit automatically invalidates every
checker at once. Four machine lenses exist (`flow` = paragraph meshes /
transitions, `cites` = citation faithfulness, `structure` = section arc,
`adversarial` = unsupported claims), run as review-todos through the
plan_tick reviewer engine, and a clean pass auto-records `approved` into
the ledger (`claude_inproc.py::_maybe_record_review_pass`). The human
checker writes through `edit(review='human')` and the smartdraft focus-block
✓ (emerald / amber-stale).

But the surface is fragmented, so in practice (e.g. draft `dr173020`) the
ledger holds only `human` rows:

- The whole-draft fanout (`mint_review_fanout`) is CLI-only and blunt — it
  mints for every chunk × lens, with no "only what's dirty" mode, so there
  is no cheap re-check loop after edits.
- The smartdraft UI shows review state only for the **focus** block and
  only the **human** checker; the four machine lenses are invisible.
- The UI "review ▾" menu speaks the *old* vocabulary
  (`structural`/`deep_review`) — a run from it would write back checker
  names that don't match the lens vocabulary (`structure` vs `structural`),
  splitting the ledger namespace.
- There is no per-paragraph "run checks on this / this subtree" trigger, no
  human check-off on non-focus blocks, no un-review, and no document-level
  rollup in the UI (only the text `view='review'`).

## In scope

**Semantics (decision, encoded in rendering + rollup):** a chunk is
**done** when `human` is approved at the current sha — human sign-off
supersedes machine lenses ("if human says it's OK it's good anyways").
Machine lenses are advisory pre-passes: their state renders as an
intermediate "machine-green, awaiting human" stage.

1. **Incremental fanout** — `mint_review_fanout(..., only_dirty=True)` +
   store query: mint only `(chunk × lens)` pairs with no `approved` row at
   the chunk's current sha. Add a `scope` (whole draft / subtree under a
   heading / single chunk) so the same primitive backs every UI trigger.
   **Skip unsettled chunks**: a chunk with an open anchored change-request
   is excluded (mirror `_maybe_record_review_pass`'s
   `_chunk_anchor_forms` open-request guard at mint time) — the writeback
   would refuse to approve it anyway, so a check there is a wasted LLM run
   on text that's about to change. Checks run on settled text; the natural
   order "apply fixes → then re-check" falls out by construction.
2. **Lens × chunk-kind granularity** (fixes the current fanout's blunt
   chunk × all-4 minting — ~528 runs, half opus, on a 132-chunk draft):
   `flow`/`cites` mint on **prose chunks only** (paragraph/listing/item —
   never equations, tables, headings, terms); `structure`/`adversarial`
   mint on **heading chunks only** (the anchored reviewer already renders
   the whole section via fisheye — per-paragraph minting re-reviews the
   same section N times for nothing). Ledger rows for section lenses land
   on the heading chunk; a paragraph's machine state = its own
   `flow`+`cites` plus its enclosing heading's `structure`/`adversarial`
   (rollup derives the enclosing heading via the existing ancestor walk;
   tooltip labels the section lenses "via section"). A section-lens
   approval goes stale when the *heading's* sha moves; a paragraph edit
   does not invalidate the section lenses (accepted imprecision — the
   round cap + human pass bound it; revisit if section reviews prove
   stale-prone).
3. **Vocabulary unification** — the smartdraft review menu mints the four
   ledger lenses (per-lens or "all"), replacing the
   `structural`/`deep_review` entries (the standalone todo-tree reviewers
   are untouched; only this draft-scoped menu changes). One checker
   namespace: `flow`, `cites`, `structure`, `adversarial`, `human`.
4. **`cites` covers sufficiency + correctness + living cites** — extend
   the citation-faithfulness brief/skill so it checks "every non-obvious
   claim carries a cite" (sufficiency) and "each cite actually supports
   its claim" (correctness), and *prefers* the taproot living cite: where
   the hub hint (`taproot/lookup.py::hubs_grounded_by_paper`, the same
   data behind `_pc_cite_claim_hub_hint`) shows a cited paper already
   grounds a hub, a bare `[pa…]`/`[pc…]` cite draws a change-request to
   switch to `[fi<hub>]`. Hub *coverage* itself stays deterministic — no
   LLM is spent counting it.
5. **Taproot surface in the reader** — (a) the document rollup dropdown
   shows the existing "N of M cited passages have a hub" scoreboard
   (`_hygiene_lines` data, deterministic, free) plus the
   `view='citations'` lifecycle counts (to-fetch / to-re-ground /
   to-promote / done) linking to that view; (b) the per-block / subtree
   dropdown gains **"convert to living cites"**, a web endpoint over the
   built `taproot/backfill.py` (`apply_chunk` / draft walk, dry-run
   preview first); (c) **cite integrity is deterministic, never a ledger
   checker**: a paragraph whose cite tokens don't resolve, or whose cited
   paper isn't held, gets a red integrity flag in the tooltip — computed
   at read time from the same token scan `_citations_view.py` uses,
   because integrity is NOT a function of the paragraph's sha (a paper
   can vanish from the corpus without the text changing; a sha-pinned
   "integrity ✓" would rot silently). Export keeps its existing hard
   gate (`export/sources.py`, resolve `--strict`); the `cites` lens
   brief states that resolution is pre-checked so the LLM judges only
   *support*, not existence. Backfill is a *fix* — it rewrites the chunk
   through the edit door (sha bump, approvals correctly stale), so the UI
   runs it before checks, and the skip-unsettled rule keeps checks off a
   chunk whose backfill review-todo is still open. Export already resolves
   `[fi<id>]` → derived originator cite keys (`taproot/cite.py`,
   Phase 1+2 built) — no export work needed.
6. **Per-paragraph status UI (smartdraft)** — every rendered block gets the
   review indicator, not just the focus: grey/empty = checks outstanding,
   hollow/blue = machine-green (own `flow`+`cites` + section lenses via
   heading, per item 2) but human pending, green = human-approved at sha,
   amber = previously approved but edited since. Mouseover tooltip = the
   per-checker matrix (✓ current / ⚠ stale / – never, with verdict + age;
   section lenses labeled "via section"). The block-render and `/blocks`
   hydration payloads carry the per-chunk review dict (one
   `review_status_for_draft` query, already implemented).
7. **Indicator dropdown** — per block: mark human-reviewed (any block, not
   just focus — endpoint already takes an explicit `dc`), un-review
   (retract a human ✓; new store op + endpoint), run one lens / all lenses
   on this paragraph, run on the enclosing section subtree (heading),
   convert to living cites (item 5), view diff-since-approval
   (`view='review-diff'` already renders it).
8. **Document rollup (toolbar)** — a header badge `N/M done`
   (**denominator = prose chunks only** — decided; headings/equations/
   tables are excluded from the human pass) with a dropdown: per-checker
   counts, the hub-coverage scoreboard (item 5), the deterministic
   document-shape stats (item 10), "run outstanding checks" (= incremental
   fanout, whole draft), and the document reads **review-complete** when
   N = M. Per-heading rollup counts in the TOC pane if cheap to derive from
   the same query.
9. **Doc sync** — state-map entry for the review surface (none exists
   today).
10. **Document-altitude review (`toc` lens + deterministic shape stats)** —
    the third altitude (paragraph → section → document). Deterministic
    half, shown in the rollup dropdown, zero LLM: scaffold-completeness
    (current headings diffed against the draft's scaffold's expected
    sections) and per-section balance (the existing `wordcount` view
    data). Judged half: a fifth lens **`toc`** (opus), one review-todo per
    document — outline ordering, narrative arc, whether imbalance is
    actually a problem. Its approval is pinned NOT to a chunk sha but to a
    **TOC digest** (hash over the ordered `(heading handle, content_sha)`
    list): add/remove/rename/reorder a section → dirty; paragraph-internal
    edits don't churn it (balance drift is covered by the deterministic
    stats; word counts stay out of the digest deliberately). Stored as the
    root chunk's `chunk_review` row with the digest in `approved_sha`;
    the status query special-cases `checker='toc'` to compare against the
    recomputed digest. Minted by "run outstanding checks" and its own
    rollup-dropdown entry; the writeback pins the digest captured at tick
    start (same no-self-approval rule).

## Explicitly NOT in scope

- **Export gating** on review-complete (design-of-record in
  paper-writing-pipeline; separate slice — this proposal only surfaces
  status, never blocks an action).
- **Authoring behavior** for `meta.author=True` reviewers (rung 3e residual
  — flag exists, behavior is its own piece).
- **New lens kinds** beyond the four (math/units checking,
  figure-reference integrity, cross-document duplication) — candidates,
  listed in Open questions, not built here.
- `review_attempts` thrash soft-escalation (design-of-record, unbuilt).
- Any change to the standalone `structural.py`/`deep_review.py` todo-tree
  reviewers or the nursery tiers.
- Bulk "approve all" human sign-off — deliberate: human review is
  per-paragraph attention; a one-click blanket approve defeats it.

## Acceptance criteria

- `mint_review_fanout(only_dirty=True)` on a draft where every chunk×lens
  is approved at current sha mints **0** todos; editing one paragraph then
  re-running mints exactly `len(lenses)` todos for that chunk (unit test).
- Subtree scope mints only for chunks under the given heading (unit test).
- A chunk carrying an open anchored change-request is skipped by the
  incremental fanout; once the request is closed and the chunk edited, the
  next run mints its lenses again (unit test).
- Lens × chunk-kind mapping: fanout over a fixture draft mints
  `flow`/`cites` only on prose chunks and `structure`/`adversarial` only
  on headings — an equation/table/term chunk gets nothing (unit test).
- The "convert to living cites" endpoint dry-runs then applies
  `taproot/backfill.apply_chunk` on one `dc<id>`; the chunk's approvals go
  stale after apply (route test with injected cascade fns, mirroring
  `tests/test_taproot_backfill.py`).
- `toc` staleness follows the digest, not text: renaming or reordering a
  heading dirties the `toc` approval; editing a paragraph's body does not
  (unit test on the digest + status query).
- The rollup badge counts prose chunks only: a fixture draft with 3 prose
  + 2 heading chunks reads `0/3`, and human-approving the 3 prose chunks
  reads review-complete (route test).
- A review-todo minted from the smartdraft menu carries `meta.review` ∈
  the four lens names; a clean reviewer pass on it lands a `chunk_review`
  row under that same name (integration test through
  `_maybe_record_review_pass`).
- Smartdraft block render + `/blocks` hydration include the per-chunk
  review payload; template shows the four-state indicator; tooltip lists
  all checkers with staleness (template/route test).
- Human ✓ works on a non-focus block; un-review deletes/retracts the
  `human` row and the indicator reverts (route + store test).
- Toolbar badge shows `N/M`; after human-approving the last dirty chunk it
  reads review-complete (route test on a small fixture draft).
- Dogfood: run the incremental fanout on `dr173020` (prod, via the
  deployed reader — not the session MCP), watch lens rows land in
  `view='review'`.

## Target + blast radius

- `src/precis/quest/review_fanout.py` (+ `weave_review.mint_review_todo`
  callers) — only_dirty + scope.
- `src/precis/store/_draft_ops.py` — dirty-pairs query, un-review op,
  subtree chunk listing (reuse existing family walk).
- `src/precis/handlers/draft.py` — `edit(review=..., verdict='retract')`
  or equivalent un-review surface.
- `src/precis_web/routes/drafts.py` — lens-run endpoint (replaces
  `/review`'s reviewer vocabulary), un-review endpoint, review payload in
  block JSON.
- `src/precis_web/routes/smartdraft.py` + `templates/smartdraft/view.html.j2`
  — per-block indicator, tooltip, dropdown, toolbar badge.
- `src/precis/data/skills/precis-review-citation-faithfulness*` — sufficiency
  + living-cite-preference wording.
- `src/precis/taproot/backfill.py` / `lookup.py` — consumed, not modified
  (new web endpoint wraps `apply_chunk`; rollup reads
  `hubs_grounded_by_paper` scoreboard data).
- Workers untouched except none — the reviewer engine + writeback already
  handle the minted todos.

## Open questions / decisions log

- **Decision — churn/termination model.** Reviews never invalidate
  reviews: lenses are read-only (find-and-file), staleness derives only
  from `content_sha`, so the four lenses run concurrently against one sha
  with no interaction and no loop. Only a *text edit* invalidates (all
  checkers at once, that paragraph only), and the incremental fanout makes
  the re-check O(edited paragraphs). The oscillation risk exists solely in
  the (out-of-scope, unbuilt) authoring path — two lens-editors rewriting
  each other's prose; when that ships it must serialize lenses per
  paragraph in a fixed order (structure → cites → flow) and cap rounds via
  the design doc's `review_attempts` counter. Human ✓ is the fixed point:
  it supersedes machine state and is one-way until the next edit.

- **Decision (2026-08-02):** the deterministic `sourced` signal stays as
  the provenance dots — the ledger holds judged state only (consistent
  with the read-time integrity rule).
- **Decision (2026-08-02):** `N/M done` denominator = prose chunks only;
  human sign-off is not collected on headings/equations/tables/terms.
- **Decision (2026-08-02):** document-altitude review added (item 10):
  deterministic scaffold-completeness + wordcount balance in the rollup,
  plus a `toc` opus lens pinned to a TOC digest rather than a chunk sha.
- **Implementation note (2026-08-02):** item 10's deterministic half
  shipped as wordcount balance only — scaffold-completeness did not.
  `Store.scaffold_sections` lays down a draft's headings once, at
  creation time, and persists nothing about what it laid down, so there
  is no stored "expected sections" list for the rollup to diff current
  headings against. Rather than invent a scaffold store (a schema
  decision outside this proposal's remit), the rollup dropdown surfaces
  the absence via a `scaffold_note` and shows only the word-count
  balance stats.
- **Implementation note (2026-08-02):** the old `structural`/
  `deep_review` menu names are kept as `POST /drafts/{id}/review`
  aliases mapping onto `structure`/`adversarial` — the fallback this
  doc's own open question anticipated, taken rather than a hard removal.
- Additional lens candidates (later): math/units consistency,
  figure/table-reference integrity, duplication across sections.
- **Deferred cost lever — combined same-tier pass.** One sonnet call per
  paragraph doing flow+cites together (shared fisheye context, per-lens
  verdicts) would halve per-paragraph runs. Requires lens-tagged findings
  + a multi-lens writeback in `_maybe_record_review_pass` (today any
  finding blocks the whole tick, which would coarsen the per-lens
  memoization). Not v1: steady-state cost is already "2 sonnet per edited
  paragraph"; build only if the one-time full pass proves painful. A
  single all-4 opus call per paragraph is rejected outright: costs more
  than the two sonnet calls it replaces, dilutes each rubric, and puts
  section lenses at the wrong altitude.
- Should heading chunks require human sign-off, or only prose blocks?
  (Machine lenses now split by chunk kind — item 2 — but the `N/M done`
  denominator still needs a call: prose-only keeps the human pass focused;
  including headings makes N = M mean literally everything was looked at.)
- Section-lens staleness (item 2's accepted imprecision): a paragraph edit
  doesn't invalidate its section's `structure`/`adversarial` approval,
  since that approval is pinned to the *heading's* sha. If this proves
  stale-prone, the sharper fix is pinning section-lens approvals to a
  **section digest** (hash over the ordered descendant shas) instead of
  the heading sha — more correct, but the dirty-derivation query must
  recompute the digest at read time; don't build it until the imprecision
  demonstrably bites.
- Where structural/deep_review menu users land: does anything depend on the
  old `/drafts/{id}/review` reviewer names? (Check agentlog/todo consumers
  before removal; fall back to keeping them as aliases that map onto
  `structure`/`adversarial`.)
