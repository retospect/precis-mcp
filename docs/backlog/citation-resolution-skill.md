---
status: draft
title: Resolve an in-text citation ([32], Miller 1921) to the cited paper and passage, and record a chunk-level cites link
pillar: memory-graph
prio: normal
---

# Resolve an in-text citation to the cited paper and passage (gr513768)

## Motivation / why

A reader (agent or human) on a paper chunk that says "as shown before [32]"
cannot follow [32] to its source. Reference-list chunks are no longer
embedded (gr513749), so the bibliography is reachable only through
structured edges; the in-text marker is the missing first hop. Wanted: the
citing chunk carries a `cites` link to the cited paper, and to the cited
chunk when the supporting passage is found.

Machinery that exists (verified against the tree):

- `src/precis/workers/bib_parse.py` — `paper_bib_entries` (marker int,
  `raw_text`, `authors`, `year`, `doi`, `held_ref_id`). Numeric `[N]`
  bibliographies only; author-year is explicitly out of its scope. Coverage
  is low (gr513755 census 2026-10-24: 5,776 of 55,412 papers have entries).
- `src/precis/workers/bib_mark.py` — regex sweep of body chunks into
  `chunk_citations (chunk_id, marker, bib_entry_id)`; numeric only, whitelisted
  to the paper's parsed markers (`_extract_markers`).
- `src/precis/taproot/resolve.py::resolve_citation` — `(chunk_id, marker)` →
  `BibResolution` (doi, `held_ref_id`, stale-merge survivor handling).
- `src/precis/store/_links_ops.py::LinksMixin.add_link` — idempotent on
  `(src_ref, src_chunk, dst_ref, dst_chunk, relation)`; chunk endpoints via
  `src_pos`/`dst_pos`. `cites` / `cited-by` are one stored row, inverse
  rewritten at read time.
- `src/precis/workers/inbound_chase.py` — the prior art for the trigger
  (`mark_paper_active` on a paper `get`, closed tag, ship dark) and for
  paper-level then chunk-scoped `cites` upgrade.
- gr513755 (in build): OpenAlex `referenced_works` sourcing via the
  `enrich-openalex` path (`src/precis/ingest/openalex_meta.py`,
  `src/precis/workers/openalex_enrich.py`), a paper→paper `cites` writer from
  `paper_bib_entries.held_ref_id`, and a slow stub trickle for cited papers
  not held.

This item adds what none of those do: author-year markers, the chunk-scoped
link, the passage match, the agent-facing recipe, and the triggers.

## In scope

Pipeline, deterministic first:

1. **Marker → bib entry (no LLM).**
   - Numeric `[32]`, `[3–5]`, `[7,9]`: already `chunk_citations`; read via
     `resolve_citation`. Gap: papers without `paper_bib_entries` (a paper read
     before `bib_parse` reached it) — `bib_parse` is bumped for it only on
     goal-driven on-request resolution (Slice 7), never by an access trigger
     or a backfill (Reto 2026-10-24).
   - Author-year `Miller 1921`, `(Smith et al., 2004)`, `Smith and Jones
     (2004a)`, `Miller (1921)`: new regex in the `bib_mark` family, parsed
     **in memory only** (Reto 2026-10-24: "we don't store, this is temporary
     for resolution"): no table, no `chunk_citations` extension; the only
     persisted result is the `cites` link. Matched to `paper_bib_entries` by
     first-author surname + year over `authors`/`year`. Ambiguity (two
     entries match) writes nothing.
   - **Marker order.** Every chunk-sourced `cites` link carries its marker
     order, the 0-based ordinal of the citation's first occurrence within the
     chunk text, as `links.meta.marker_order`, plus the label as
     `meta.marker` (`"32"`, `"Miller 1921"`). Links from a chunk render in
     occurrence order (sort on `marker_order`). A range `[3–5]` yields three
     links sharing one occurrence position, ordered by number.
   - Same whitelist rule as `bib_mark`: a candidate marker counts only if it
     matches an entry of that paper (kills "Table 3", "[H2O]", years in
     running text).
2. **Entry → paper (no LLM unless the entry lacks a DOI).** `doi` /
   `held_ref_id` from the entry; else the citing paper's cached OpenAlex
   `referenced_works` (gr513755) matched on title/year; else a live OpenAlex
   title lookup through the existing client. Held → `held_ref_id`. Not held →
   `upsert_stub_paper` through the gr513755 trickle's budget, never directly
   (one shared rate; see `forward-citation-watch.md` for the interaction).
3. **Chunk → paper `cites` link (no LLM).** Citing chunk → cited paper,
   ref-level on the cited side, written with `add_link(relation='cites',
   src_pos=<chunk ord>, set_by=…, meta={'marker': …, 'marker_order': …})`.
   Cheap and idempotent, so safe to run for any accessed paper.
4. **Passage match (LLM, castor local). BLOCKED: writes of `cites` by a
   model-identity writer are on hold pending Reto's ruling on the
   machine-edge gate (open question 1).** Deterministic layers proceed.
   Input: citing sentence plus the
   top-5 body chunks of the cited paper (semantic search scoped to that
   paper; body chunks are embedded, reference chunks are not). A small model
   returns the supporting chunk or `none` (+ optional "does not support"
   caveat). Output upgrades the ref-level link to chunk→chunk
   (`dst_pos=<cited chunk ord>`); `none` leaves the ref-level link and stamps
   a done-marker so the pair is not retried.
5. **Skill `precis-cite-resolve-help`** in `src/precis/data/skills/`. Recipe
   an agent follows mid-read; family `paper`, answers "what does [32] refer
   to?", "link this citation to its source". It drives only existing MCP
   calls (no new verb required for the recipe):
   - `get(kind='paper', id=<paper>~<chunk>)` — read the citing chunk; spot the
     marker.
   - `search(kind='paper', q=<marker or surname year>, scope=<citing paper>,
     mode='lexical')` — find the bib entry in the paper's tail chunks
     (lexical first: reference chunks are not embedded).
   - `search(kind='paper', q=<title or DOI>)` — is the cited paper held.
   - The OpenAlex hop is server-side (step 2); an agent only calls
     `put(kind='paper', …)` to request an unheld paper, as
     `precis-stubs-help` documents.
   - `search(kind='paper', q=<citing sentence>, scope=<cited paper>)` —
     semantic search for the supporting passage.
   - `put(kind='paper', id=<citing chunk handle>, link='paper:<cited
     id>~<chunk>', rel='cites', meta={'evidence': ['pc<citing chunk id>'],
     'why': …})` — record the link.
   The skill also tells the agent that the deterministic result may already
   be there (read the chunk's links first) and that the write is idempotent.
6. **Routing.** The passage match is an LLM operation registered in
   `src/precis/utils/llm/operations.py::LLM_OPERATIONS` as
   `cite_resolve:passage` (tier `BIG`, `model=None`, label/description/note in
   the `chase:verify` entry's shape). The operator steers it to castor's
   local gpt-oss lane by writing the `app_settings` row
   `llm.op.cite_resolve:passage` with the castor chain, exactly as the
   2026-10-22 ruling did for `chase:verify` (its entry's `note`). Call site
   sets `source='cite_resolve:passage'`, pins no `model=`, goes through
   `route()` so it logs to `llm_call_log`. Reto's explicit choice: castor
   local, not a cloud tier.
7. **Triggers.** Access-driven: first `get` of a paper marks it
   (`CITERES:pending`, closed tag, same shape as `mark_paper_active`); a
   low-priority pass drains marked papers, deterministic layer first, LLM
   layer only when its flag is on and the gate ruling has landed. An access
   never bumps `bib_parse` for a paper without entries: that bump happens
   only for goal-driven on-request resolution (the on-request job below),
   never for backfill. Per-chunk done-marker versioned like
   `BIBMARK:<version>` so a re-sweep is a version bump. On request: a job
   ("deep link these papers now") that claims the named papers at high
   priority and reports an ETA. No sweep over unmarked papers anywhere.

## Explicitly NOT in scope

- Bulk or blanket corpus runs; the claim query joins only on marked papers
  or on-request job targets.
- Backfilling paper→paper `cites` from `held_ref_id` and OpenAlex reference
  sourcing (gr513755) — dependency, not this item.
- Embedding reference chunks (gr513749 ruled them out).
- Fetching unheld cited papers outside the gr513755 stub trickle.
- Changing the paper fisheye; cites / cited-by stay explicit views.
- Verifying that the cited passage supports the claim as an evidence edge
  (that is `chase:verify` / taproot territory); here "no support" is only a
  caveat note on the link.

## Acceptance criteria

Per slice, below. Overall: for an accessed paper with parsed bib entries,
every numeric marker in its body chunks has a chunk-sourced `cites` link to
the entry's paper (or a stub) within one pass; author-year markers resolve
on a fixture set at precision ≥ 0.95 (unmatched is acceptable, wrong is
not); every chunk-sourced `cites` link carries `meta.marker_order` and a
chunk's links render in occurrence order; author-year parse leaves no stored
row anywhere; an access never triggers `bib_parse`. The LLM pass (blocked on
the machine-edge ruling) writes no link without a `meta.evidence` pin and
runs only on the castor chain (`llm_call_log.model` shows the local model).

## Slices (smallest first)

| # | Slice | Acceptance |
|---|---|---|
| 1 | Chunk→paper `cites` writer from `chunk_citations` where `held_ref_id` is set (extends the gr513755 writer to chunk scope on the citing side). Dark flag, version marker. | Fixture paper: each swept marker yields one link with `meta.marker` + `meta.marker_order`; a chunk's links list in occurrence order (a marker cited twice keeps its first position); re-run inserts nothing; `get` of the chunk shows the links; unheld entries write nothing. |
| 2 | Author-year marker extraction in memory (no table) + match to `paper_bib_entries` authors/year, with the `bib_mark` whitelist rule; writes `cites` links with marker order. | Fixture of ≥30 labelled author-year sentences: precision ≥ 0.95; ambiguous → no link; "et al.", `2004a/b`, parenthetical and narrative forms covered; no new table or `chunk_citations` row; mixed numeric + author-year chunk orders by text position. |
| 3 | Skill `precis-cite-resolve-help` + skill-lint pass; recipe only, over existing calls. | `get(kind='skill', id='precis-cite-resolve-help')` served; `search(kind='skill', q='what does [32] refer to')` ranks it first; every call in the recipe runs against the dev DB. |
| 4 | OpenAlex hop for entries without DOI (title/year lookup via the shared `openalex` limiter) and stub request through the gr513755 trickle budget. | Entry with no DOI resolves or records a stamped miss; no direct stub mint from this path; refusal by the limiter defers, never errors the pass. |
| 5 | Access trigger (`CITERES:pending`, drain pass, per-chunk version marker). Never bumps `bib_parse`. | Reading a paper marks it once; the pass processes only marked papers; ship dark; an unmarked paper is never touched (test on the claim query); a paper with no entries is skipped, no parse enqueued. |
| 6 | **BLOCKED on the machine-edge gate ruling.** Passage layer: `cite_resolve:passage` op, top-5 candidate pick, chunk→chunk upgrade, `none` memo. | Registered in `LLM_OPERATIONS` (registry test passes); with the castor row set, calls log to the local model; link carries `meta.evidence` + `why`; `none` is memoized and not retried. |
| 7 | On-request job with ETA ("deep link this set"); the only path that bumps `bib_parse` for a named paper lacking entries. | Job over N papers reports queued/ETA, outranks the access queue, bumps `bib_parse` only for the named papers, finishes with a per-paper count of resolved/ambiguous/unheld. |

## Target + blast radius

New: `src/precis/workers/cite_resolve.py` (trigger + drain), a marker
extension next to `bib_mark.py`, `src/precis/data/skills/precis-cite-resolve-help.md`,
one `LLM_OPERATIONS` entry. Touches `handlers/paper.py` (the mark call,
beside `mark_paper_active`), `workers/registry.py` (new pass, default off),
no migration (author-year is not stored). Link volume:
bounded by accessed papers × citations per paper; the LLM lane is bounded
by the castor slot the router takes.

## Open questions / decisions log

Decided (Reto 2026-10-24, relayed by COORDINATOR):

- Author-year citations are not persisted ("we don't store, this is
  temporary for resolution"): parsed in memory, only the `cites` link is
  kept. No sibling table, no `chunk_citations` extension.
- Links from a chunk carry their marker order (ordinal within the chunk) in
  `links.meta` and render in occurrence order.
- `bib_parse` is bumped on access only for goal-driven on-request
  resolution, never for backfill or plain access.

Open:

1. **Machine-edge gate — ON HOLD, Reto has not ruled.** The LLM passage layer
   (slice 6) may not write `cites` links until he does; deterministic layers
   proceed. `cites` is not in `MACHINE_EDGE_STANCE`
   (`src/precis/store/_links_ops.py`). A write with a model identity (the LLM
   layer) therefore needs `meta.evidence` + a writer annotation and is
   refused when the two papers sit inside the `+recall` card-distance floor
   (`machine-edge-co-retrievable`) — which a citing/cited pair often does.
   Add `cites` to the stance set (or exempt it), or write the LLM-layer link
   through another door? Deterministic writes carry no model identity and are
   unaffected. Reto's rule to change; not mine.
2. **Who is `set_by` for the deterministic layer?** `system` vs `agent`;
   decides whether the link shows as machine-authored and whether the gate
   in question 1 applies.
3. **Chunk-level `dst` granularity for numeric ranges.** `[3–5]` yields three
   links from one chunk; is that the wanted rendering or one link with a
   `markers` list in `meta`?
4. **Request surface.** Is "deep link this set now" a new job kind or an
   existing generic job with a `kind` payload? Needs a pointer to the job
   conventions before slice 7 is `ready`.
5. **Eval.** Passage-layer precision on castor gpt-oss vs a frontier judge
   (see `llm-judge-reliability.md`): is a sampled review gate wanted before
   the link is shown, as for `chase:verify`?
