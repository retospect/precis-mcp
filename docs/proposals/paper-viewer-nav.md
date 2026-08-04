---
status: implemented (slices 1-3 shipped 2026-08-04; slice 4 deferred — rides docs/design/paper-reader-bbox-backfill.md)
title: Paper viewer — trustworthy chunk anchoring, Sources/Cited tabs with fetchable stubs, reviewed toggle
model: sonnet
---

# Paper viewer — anchoring, references, review sign-off

From Reto's 2026-08-04 review of `/papers/nasibulin2007investigations`
(`pa1483`). Two defects + two features, four slices. Anchors verified
against the tree at authoring time.

## Root cause shared by both defects

Chunks carry **no real position data**. `page_first` is an ingest-time
guess: `marker.py::_assign_pages` substring-matches TOC headings into the
markdown and carries the last matched page forward (untested since
introduction; the fitz fallback separately assigns 0-indexed pages to a
1-indexed viewer). Prod evidence for `pa1483`: 48 chunks share only 4
distinct `page_first` values (0–5); chunk 34 claims page 3, its text sits
later. The viewer (`static/paper-viewer.js::findInPdf`) then **trusts the
guess first** — `app.page = page_first` — and dispatches the PDF.js
text-find from there, so the find selects the match nearest a wrong
starting point. The file's own header says "a page jump is the
always-correct fallback"; it is not.

## Slice 1 — anchoring fixes (web layer, no ingest change)

1. **Invert the trust: phrase-first, page-fallback.** In `findInPdf`,
   dispatch the phrase find *without* first jumping to the guessed page;
   listen on `eventBus` `updatefindmatchescount` — if `total >= 1`, let
   the find position the viewport. Only when the phrase is missing or
   matches zero (marker text diverged from the PDF text layer) fall back
   to the page jump, and mark it visibly approximate (e.g. "~p.4").
   Multi-match case: with no reliable page anchor, first match wins —
   strictly better than today's wrong-page-nearest match.
2. **TOC fallback query.** `gotoSeg`'s fallback dispatches a single bare
   KeyBERT keyword (`s.keywords[0]`) — stemmed, often not verbatim in the
   PDF → the red `notFound` find-bar Reto saw. Use the segment's heading
   text / first-chunk phrase instead, same phrase-extraction path.
3. **Accept the handles we display.** The TOC shows ADR-0032 compound
   handles (`pa1483~13..15`) yet no UI affordance accepts them: the Jump
   box is `type="number"` and `GET /papers/{ref_id}/chunk/{ord}` is typed
   `ord: int` (422 before `_cited_chunk`'s own `N..M` regex is reached).
   Widen the route param to `str`, parse `pa<id>~lo..hi` / `lo..hi` /
   `lo` (guard: `pa<id>` must match the route's paper), make the Jump box
   a text input that normalizes either form. One resolver covers
   `?chunk=`, the Jump box, and TOC clicks (all back onto
   `_cited_chunk`, `routes/papers.py:386`).

Tests: route-contract tests for the widened `chunk/{sel}` param;
`build_toc_segments` round-trip (every emitted `lo` resolves);
`_assign_pages` unit coverage (zero today) pinning the degenerate
all-page-0 case as *documented* coarse behavior. PDF.js behavior itself
is manually verified (no browser in the gate).

## Slice 2 — "reviewed" toggle on the Meta tab

`refs.human_verified_at/by/note` exist on every ref, fully plumbed
(`store.set_human_verified` / `clear_human_verified`,
`_refs_ops.py:1382`), and their docstring reserves them for papers — but
the only writer today is `precis verify` on findings; **0 papers** carry
the stamp fleet-wide.

- UI: a checkbox/button row in `papers/_meta_panel.html.j2` — unchecked:
  "Mark reviewed"; checked: "Reviewed ✓ <date> by <who>" + un-tick.
- Endpoint: `POST /papers/{ref_id}/reviewed` (toggle), thin preset over
  the store ops in the `retriage`/`untriage` pattern
  (`routes/papers.py:1230`), `by` = the web write identity
  (`web:owner`).
- Policy (recommended): a successful metadata edit
  (`POST /papers/{id}/edit`) **clears** the stamp — a review can't cover
  metadata that has since changed (mirrors the finding-side
  retraction-propagation clear). Marking reviewed also clears any
  lingering `needs-triage`.
- Later (out of scope): a `/drive` facet for unreviewed papers.

## Slice 3 — Sources / Cited tabs + fetchable reference stubs

Today the outgoing bibliography is fetched from Semantic Scholar and
**discarded** (`backfill/citation_lens.py` keeps only held↔held `cites`
edges; `workers/chase.py` uses references transiently). `pa1483`: 2
outgoing edges held vs ~29 bibliography entries; 47 incoming. Reto's ask:
store them, make them trivially followable.

1. **Persist S2 neighbors.** New side table (forward migration)
   `s2_neighbors(ref_id, direction cites|cited_by, s2_id, doi, arxiv,
   title, year, held_ref_id nullable, fetched_at)`, written by the same
   fetch `citation_lens` already performs (it has the rows in hand and
   drops them) and refreshed on the existing `citation_edges` 30-day TTL
   event. `held_ref_id` resolved via `ref_identifiers` at write; a later
   ingest re-resolves on next refresh. This is deliberately **not** "mint
   a stub per reference" — no ref explosion; a stub is created only on
   explicit fetch (below).
2. **Tabs.** Extend the reader tab strip
   (`templates/_reader/reader.html.j2:48`) to
   `Navigate / Jump / Sources / Cited / Meta`. Sources = this paper's
   outgoing references in bibliography order; Cited = incoming (held
   incoming is already a free read —
   `store.links_for(id, direction='in', relation='cites')` — union the
   `s2_neighbors` cited_by rows for non-held). Each row: held →
   `/papers/<slug>` link (reusing the named `precis-paper` window
   convention from the smartdraft work); non-held → title/year +
   external links via the existing `paper_links.py` tiers (S2, DOI,
   arXiv, Scholar) + a **Fetch** button.
3. **Per-paper fetch endpoint.** `POST /papers/{ref_id}/fetch-ref`
   (body: doi/arxiv/s2_id from the row): wraps the existing `acquire` /
   `upsert_stub_paper` door (idempotent) + `requeue_stubs_for_fetch` so
   `fetch_oa` picks it up next; htmx fragment swap per the
   `flags.py:131` pattern, row flips to "queued". Today's only fetch
   affordance is the batch `/drive/requeue-stubs` — this is its
   single-paper sibling.
4. **First-view backfill.** Opening Sources/Cited when `s2_neighbors` is
   empty triggers the fetch inline (same S2 call `ingest/citations.py`
   makes today, cached), so the tab works immediately on old papers
   without a fleet backfill pass.

Note: `get(kind='paper', view='bibliography')` is mis-named — it renders
inbound verified-`citation` claims, not the bibliography. Leave it; the
agent-surface sibling of these tabs already exists as
`get(kind='semanticscholar', id='refs:<paper>')`.

## Slice 4 — real page anchors (deferred, separate decision)

The root fix for anchor accuracy is ingest-side: run Marker in
structured mode and persist true per-block pages (+bbox), i.e. the
already-scoped `docs/design/paper-reader-bbox-backfill.md` (todo 42379,
PRIO:low). That carries a corpus re-extract cost and is **not** part of
this build; slice 1 makes the viewer honest without it. Revisit priority
if phrase-match failures stay common after slice 1.

## Effort

Slice 1: 1–2 (coder) — JS + one route widening + tests.
Slice 2: 1 (coder) — endpoint + template row.
Slice 3: 3–4 (coder) — migration + lens write-through + two tabs +
fetch endpoint.
Slice 4: deferred; rides bbox-backfill design.
