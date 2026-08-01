---
status: draft
title: Taproot draft-citation view — per-draft lifecycle surfacing (to-fetch / re-ground / promote / done)
model: opus
---

> **Built.** The core `view='citations'` data view + tests landed
> (`src/precis/handlers/_citations_view.py`, wired into `handlers/draft.py`;
> `tests/test_draft_citations_view.py`). The web surface (AC5) landed as a
> **Drive scope facet**, not a bespoke smartdraft panel: `/drive?cited_by=<draft>`
> narrows Drive's existing stub-acquisition queue (`state=stub` — flags +
> watch-dir drop-zone) to the draft's to-fetch set, and the smartdraft reader's
> Export tools carry a "papers to fetch ▸" link to it. The scope reuses the same
> `draft_fetch_ref_ids` derivation as this view (`_citations_view.py`), so the
> two never diverge (`routes/drive.py`, `store.recent_refs(ref_ids=…)`;
> `tests/precis_web/test_drive_sql.py`, `test_routes.py`). **Remaining:** the
> `precis-draft-help` skill entry. The view implementation parses chunk text for
> both the handle and `[pub_id]` grammars directly (the autolinker doesn't mine
> `[pub_id]`), a correctness refinement over this spec's "via the cites edge"
> wording.

# Taproot draft-citation view — per-draft lifecycle surfacing

## Motivation / why

A draft cites corpus papers with three token forms: `[pa<id>]` (whole-paper),
`[pc<id>]` (paper-chunk), and — the taproot end-state — `[fi<hub>]`/`[pub_id]`
(the **claim**, which resolves to current originators at export, ADR 0074).
Getting a draft from direct paper cites to claim cites is a lifecycle, and its
**engine** is already built (`taproot/backfill.py` migrates `[pc]`→`[fi]`;
`taproot/cite.py`+ADR 0074 render the terminal state). What is missing is the
**author's map of that lifecycle**: per draft, which cites are ready to promote,
which papers still need fetching, and which need re-grounding. Today that is
tracked by hand (this session tracked `dr173020`'s four un-fetched landmark
stubs in a scratchpad).

This proposal is the **read-only surfacing** — one derived view that partitions
a draft's cites so an author can work them down. It is the durable home of
**gripe 180155** ("papers to fetch for this draft" button) and its motivating
**gripe 180189** (the pa1056 metadata corruption). The write-path work to
*extend* the migration engine to the `[pa]` arm is a **separate, dependent
proposal** — [`taproot-draft-pa-arm.md`](./taproot-draft-pa-arm.md) — that
lights up this view's `[pa]` rows once it ships. This one changes **no write
path** and delivers value over the already-built `[pc]`/`[fi]`/fetch states.

## The lifecycle (the model this view surfaces)

One citation, four states; the view assigns each cite exactly one:

```
[pa] stub  ──fetch──▶  whole-paper cite,      ──promote──▶  [fi<hub>] claim cite
                        fetched (re-ground)     (backfill)    (terminal)
   │                        │                                     │
"to fetch"              "to re-ground"                     "done" — living cite
cited-by ∧ 0 blocks     [pa] ∧ fetched                    (ADR 0074)
```

- **to fetch** — paper cited but **0 blocks** (a stub). Derived; **self-clearing
  on ingest** (no link to delete — closes gripe 180155's "should we clear it?"
  with *no*). Not promotable.
- **to re-ground** — paper **fetched** but still cited **whole-paper `[pa]`**;
  wants a `[pa]`→`[pc]` chunk pin so the eventual hub edge is chunk-grounded
  (the follow-on proposal acts on these rows).
- **to promote** — a **`[pc]`** cite not yet routed through a hub → the existing
  `backfill.plan_chunk`/apply migrates it. (Engine built; the view just points
  at it.)
- **done** — a **`[fi<hub>]`/`[pub_id]`** cite. Terminal.

**The partition is purely derived — no LLM call, no new storage.** A cite's
state is a function of only: its **token kind** (`pa`/`pc`/`fi`, parsed by
`utils/mentions.py::BARE_BRACKET_REF_PATTERN`) and, for `pa`/`pc`, the cited
paper's **block-count** (`store.count_blocks`) reached via the write-time
autolinker's `cites`/`cited-by` edge. That is the whole classifier.

> **Selectivity is deliberately NOT in this view** (resolves readiness
> blocker #7). Whether a `[pc]` cite-group is a *promotable claim* vs. a
> *background/pointer* cite is decided by `taproot/canon.py::extract_claim` — an
> **LLM call** — which already runs inside `backfill`'s promote **dry-run**.
> Pulling it into a read view would mean an LLM call per cite-group on every
> view read. So the view classifies by token-kind + fetch-status only; every
> `[pc]` shows as **to-promote**, and the `NO-CLAIM`/skip determination surfaces
> at promote-time where the call already happens. The view stays cheap and
> derived.

## In scope

- A **per-draft read view** `get(kind='draft', id=<slug>, view='citations')`
  that walks the draft's chunks, parses each paper/claim token, and returns
  every cite partitioned into exactly one of {to-fetch, to-re-ground,
  to-promote, done}. Each row: `{dc<id>, token, paper/hub handle, title, DOI (if
  any), next-action label}`. Grouped by partition, stable order.
- **Derivation only**: token kind (`utils/mentions.py`) + paper block-count
  (`store.count_blocks`) over the autolinker `cites`/`cited-by` edges
  (`store/_draft_ops.py`). No new columns, no migration, no LLM call.
- The **to-fetch partition = `cited-by draft ∧ block-count 0`** — this *is* the
  gripe-180155 fetch worklist; it self-clears on ingest with no link edit.
- A thin **web surface** — delivered as a **Drive scope facet**, not a bespoke
  panel (Drive already *is* the papers-to-fetch surface: `state=stub` is the
  folded `/papers-needed` queue with acquisition flags + watch-dir drop-zone;
  it was only missing per-draft scope). A `cited_by=<draft>` param on
  `/drive` narrows that queue to the draft's **to-fetch** set (reusing
  `draft_fetch_ref_ids`), and the smartdraft reader links to it. The richer
  re-ground/promote/done partitions stay in `view='citations'` (CLI + `get`),
  which is the fetch view's superset. (CLI reads the same view.)

## Explicitly NOT in scope

- **The `[pc]`→`[fi]` migration engine** — built (`taproot/backfill.py`); this
  view *points at* it (a to-promote row's action invokes existing
  `plan_chunk`/apply), and does not call or modify it.
- **The `[pa]` write-path arm** — extending `backfill.py`'s segmenter to
  `[pa<id>]` groups, the re-ground action, and the retirement invariant for it
  live in the dependent proposal `taproot-draft-pa-arm.md`. This view only
  *displays* `[pa]` rows in to-fetch / to-re-ground; it performs no `[pa]`
  promotion.
- **Selectivity / claim-vs-background classification** — a promote-time concern
  (`extract_claim`), not a view concern (see the blockquote above).
- **Render/export of `[fi]`/`[pub_id]`** — built (`cite.py`, ADR 0074);
  unchanged.
- **Any write path at all** — this proposal is read-only.

## Acceptance criteria

1. `get(kind='draft', id=<slug>, view='citations')` returns every paper/claim
   cite in the draft, each in exactly one partition, with `dc<id>`, token,
   handle, title, DOI, and a next-action label. For `dr173020` today: the four
   landmark stubs (pa42557/42556/42555/42559) appear under **to-fetch**; `[pc]`
   cites under **to-promote**; any `[fi]` cites under **done**.
2. The **to-fetch** partition equals exactly `cited-by draft ∧ paper
   block-count = 0`. After ingesting one of those stubs, re-reading the view
   shows that cite has **left to-fetch** (moved to to-promote for a `[pc]` cite,
   or to-re-ground for a `[pa]` cite) **with no link deleted** — verified by a
   test that flips a stub to non-zero blocks and re-derives.
3. The view performs **zero LLM calls and zero writes** — asserted by a test
   that runs it with the canon/LLM dispatch stubbed to raise; classification
   still succeeds from token-kind + block-count alone.
4. A draft with no citations returns an empty-but-well-formed view (all four
   partitions present, empty); a `[fi]`-only draft returns everything under
   **done**.
5. **[built as a Drive scope facet, not a bespoke panel]** `/drive?cited_by=<draft>`
   scopes Drive's `state=stub` acquisition queue to exactly the draft's to-fetch
   set (`recent_refs(ref_ids=draft_fetch_ref_ids(draft))`): each row is a stub
   the draft cites, carrying the existing acquisition flags + find: (DOI) links +
   watch-dir drop-zone. An unknown draft → empty queue (never the whole corpus).
   The smartdraft reader's Export tools link to it ("papers to fetch ▸"). Reuses
   the acquisition surface wholesale rather than duplicating it.

## Target + blast radius

- **`handlers/draft.py`** — new `view='citations'` branch; a pure read over
  `links` + `store.count_blocks` + the `utils/mentions.py` token parse. No
  storage, no migration.
- **`precis_web`** — a `cited_by=<draft>` scope facet on `routes/drive.py`
  (`store.recent_refs(ref_ids=…)`, reusing `draft_fetch_ref_ids`), a scope
  banner in `drive/index.html.j2`, and a "papers to fetch ▸" link in
  `smartdraft/view.html.j2`'s Export tools. No bespoke panel.
- **Reused unchanged**: `utils/mentions.py`, `store.count_blocks`,
  `store/_draft_ops.py` autolinker, `taproot/*` (only *referenced* by
  next-action labels, not called).
- **Skills**: `precis-draft-help` gains the `view='citations'` entry.

## Open questions / decisions log

1. **[RESOLVED — one view, `view='citations'`, client filters]** One view with
   four partitions (the fetch list is the `blocks=0` partition); the web panel
   filters partitions client-side. Not a separate `view='fetch-queue'`.
2. **[RESOLVED — token-kind + block-count only; selectivity is promote-time]**
   The classifier is purely derived; the claim-vs-background decision is not a
   view concern (readiness blocker #7). Recorded above.
3. **[RESOLVED — read-only, no atomicity surface]** This proposal touches no
   write path, so the `backfill` atomicity question (readiness blocker #6)
   belongs entirely to the dependent `[pa]`-arm proposal, not here.
4. **[open — minor] Title/DOI source for a stub row.** A stub carries
   `refs.title` + a `doi` identifier (both present for the four landmarks);
   confirm the view reads them from the ref, not a fetch. v1: read from the ref;
   a title-less stub shows its handle.

## Cross-references

- [`taproot-draft-pa-arm.md`](./taproot-draft-pa-arm.md) — the dependent
  write-path proposal that lights up this view's `[pa]` rows.
- `docs/proposals/taproot.md` — shared model; closes the surfacing half of the
  draft-side lifecycle; respects #15 (draft insulation) trivially (read-only).
- `docs/decisions/0074-...` — the terminal `[fi]`/`[pub_id]` render this view's
  "done" partition points at.
- gripe **180155** / **180189** — the to-fetch slice; this view is their home.
