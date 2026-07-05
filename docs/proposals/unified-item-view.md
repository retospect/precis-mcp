---
status: draft
title: One unified item view — DRY cross-kind list/search over source + authored things, with per-kind presenters
---

# One unified item view — DRY cross-kind list/search over source + authored things

## Motivation / why

Two big domains of "things" live in precis: **source** — everything
you *consume* and can search — and **authored** artifacts (drafts, cad,
structure, pcb, todo, folder — the `role='artifact'` kinds). Source is
broader than ingested documents: it is **every searchable non-artifact
kind**, spanning two sub-families:

- *ingested documents* — papers, stubs awaiting fetch,
  slides/presentations, patents, datasheets, cfp (the
  `corpus_role != 'none'` / `role='corpus'` kinds); and
- *cached external answers / references* — `perplexity-reasoning`,
  `perplexity-research`, `websearch`, `wikipedia`, `web`, `youtube`,
  `semanticscholar`, and the computational answer kinds (`calc`/wolfram,
  `math`, `oracle`). These are query results, not primary documents, but
  they are embedded + searchable and legitimate LLM context ("what did
  perplexity say about X" alongside the papers), so they belong in the
  same retrieval surface.

Only genuine ops/machine kinds (`status`, `agentlog`, `alert`, `job`,
`cron`, `message`) are *not* items — they never get a presenter (see
below), which is exactly what keeps them out of the list.

Today each family is browsed through a pile of bespoke pages:
`/papers-needed` (the stub fetch queue), `/papers` + `/papers/triage`,
`/refs`, `/tags/refs`, `/drive` (the artifact/folder browser). They
overlap heavily — each one is *some* slice of "list refs, filter,
tag, open" — but they don't share a query model, a row renderer, or a
tag surface. And none of them lets you do the thing that started this:
**"search papers and patents together, semantically, within a date
range, order by recency — then hand exactly that set to the LLM and
say 'write a document mainly based on this stuff.'"**

The key realisation: the human's filtered screen and the LLM's
retrieval scope are the *same query*. Keyword search, semantic search,
tag filter, kind filter, date range — these are identical operations
whether the row is a paper, a slide deck, or a draft, and the MCP
already exposes them (`search`, `tag`, `get`). So the web view and the
LLM are not two things looking at two corpora — they are **two
front-ends over one query model.** What you filter to on screen is
literally the set the LLM would retrieve. The `/papers/<slug>`
TOC/keyword view is the existing proof: built LLM-first for token
efficiency, and it turns out to be the nicest way for a person to skim
a paper too. Presentation differs a little; retrieval doesn't differ
at all.

So: build **one retrieval primitive in the MCP/store**, render it as
**one unified list** (which subsumes `/drive`, `/papers-needed`,
`/papers`, triage, `/refs`, `/tags/refs`), and let a tailored filter
double as an LLM context set.

## In scope

Staged; each slice ships independently and additively (the old pages
stay until the unified view proves out, then retire).

**Slice 1 — flag tags on items (this slice; the concrete origin).**
Kind-agnostic flag buttons — `read-later`, `must-read`, `skim` (bare
`OPEN:` tags) — as one-click toggles, first landed on `/papers-needed`
rows. Because a stub and its eventually-ingested paper are the **same
`ref_id`**, the flag rides through fetch+ingest into the finished
paper: flag now, read when it lands. A shared route
(`POST /flags/{kind}/{ref_id}`) + a Jinja partial (`_flag_buttons`) so
the same widget drops onto any future item list.

**Slice 2 — cross-kind search primitive (the DRY core). SHIPPED.**
`Store.search_chunks_across_kinds` searches the chunks of a *set* of
kinds at once (semantic + lexical, RRF-fused via `search_blocks_multi`),
collapses to one best chunk per ref (breadth/triage), bounds by
`refs.created_at`, and orders by relevance (default) or recency. The
`search` verb grew `kinds` (via the existing `kind='a,b'` comma
syntax), `sort=`, `since=`, `until=`; a `sort`/`since`/`until` call
routes to `_dispatch_source_search` (one store query over `refs.kind =
ANY(...)`, not the per-handler fan-out). Kinds with no embedded chunks
contribute nothing, so an over-broad set is harmless. Breadth is the
default (one hit per ref); depth (ranked chunks) is deferred to a later
`per_paper`-style param.

**Slice 3 — the per-kind presenter contract + unified list page.** A
base `ItemPresenter` every kind implements:
- `name()` — row/popover heading
- `preview(query) -> text | image` — one method, union return: text
  kinds return the matching chunk, visual kinds return an image. The
  renderer switches on the variant. This is the check-time guarantee:
  one mandatory method, adding a new required method breaks every kind
  that hasn't filled it in.
- `hover_preview(query)` — the richer peek (more chunks/metadata, or a
  lazy live 3D viewer)
- `thumbnail()` — cached still (deferred for visual kinds)
- `open_url()` — the click-through target
- `state()` / badges — waiting-vs-ready, wip-vs-exported
- `actions()` — kind-specific actions beyond the universal flags
  (papers-needed "re-chase stub", cad "apply proposal"). Declared here
  so consolidation doesn't leak them back onto bespoke pages.

Three-tier rendering, all presentations of `search`/`get`:
- **row** = `search` hit (query-aware preview cell)
- **hover popover** = shallow `get` peek (+ lazy live thumbnail + row
  actions; also the moment we can afford the expensive live 3D viewer —
  only under the pointer)
- **click** = full `get` (the existing per-kind viewer: paper reader,
  `/cad`, `/structure`, draft editor — these stay, they are the
  `open_url` targets)

Unified list page: filters (search kw⇄semantic · kind-set ·
tag/flags · date range · recency sort) + folder grouping mode +
density presets (compact line ⇄ preview cards ⇄ dense table). The old
pages become saved filters: `/drive` → `role='artifact'`;
`/papers-needed` → `kind=paper, state=waiting`; triage →
`tag=needs-triage`.

**Which kinds appear = which kinds have a presenter.** This is the
clean gate for membership, replacing a fragile `role`/`corpus_role`
query. Source presenters cover both sub-families — ingested documents
*and* the cached external-answer/reference kinds (`perplexity-*`,
`websearch`, `wikipedia`, `web`, `youtube`, `semanticscholar`,
`calc`/wolfram, `math`, `oracle`). Artifact kinds get presenters too.
Ops/machine kinds (`status`, `agentlog`, `alert`, `job`, `cron`,
`message`) get none and so never appear. The author/source split is
then a facet derived from `KindSpec.role`, not a separate page.

**Slice 4 — "write a document from this view."** A tailored filter *is*
a serialized query; a "use as context → draft" action mints an
authoring job scoped to exactly those refs (the LLM re-runs the same
verb to pull context). Nothing materialised — the human screen and the
job's retrieval scope are the same query object.

**Coupled workstream — kind taxonomy audit (rides on Slice 3).**
Adopting the presenter forces a touch of every one of the ~40 kinds, so
audit each kind's spec in the same pass: reconcile the `role` /
`corpus_role` drift (datasheet `evidence`+`stream`; pres
`corpus`+`none`), collapse near-duplicate kinds (the five external-
answer kinds `perplexity-reasoning` / `perplexity-research` /
`websearch` / `web` / `wikipedia` → possibly one kind + subtype; the
computational `calc` / `math` / `oracle` similarly), rename for
legibility, retire dead ones. Then (later) rewrite each `precis-*-help`
skill to match. Fewer, sharper kinds = less surface for the LLM to
learn. Presenter-per-kind and audit-per-kind are one loop, not two.

## Explicitly NOT in scope

- Retiring the per-kind **detail viewers** (paper reader, `/cad`,
  `/structure`, draft editor). Those stay — they are `open_url()`
  targets. "Much goes away" = the browse/filter/queue *pages* collapse,
  not the readers/editors.
- A per-column-configurable list UI. Density/preset toggle only in v1;
  the query already decides preview content.
- Live 3D thumbnails on every row. Cached still + hover-only live
  viewer; per-row live rendering is out.
- New tag *vocabulary* machinery — flags are plain `OPEN:` tags through
  the existing `tag` verb.
- `edgar` as a kind (does not exist); `patent` stays registered-but-
  disabled until `PRECIS_PATENT_RAW_ROOT` is set — both slot into the
  source-kind set automatically once they have a presenter.

## Acceptance criteria

- **Slice 1:** each `/papers-needed` row shows three toggle buttons;
  clicking `read-later` adds `OPEN:read-later` to that ref (verified via
  `has_tag`) and the button renders active; clicking again removes it;
  the page returns to the same `?awaiting&page` view; a failed dispatch
  renders the handler error (not a silent redirect). One batched query
  fetches flag state for the whole page (no N+1). Gate green
  (ruff + mypy + pytest in the container).
- **Slice 2:** `search` accepts a multi-kind + date-range + recency
  query and returns fused cross-kind results; an LLM can retrieve
  "papers + patents about X, newest first" in one call.
- **Slice 3:** adding a new abstract method to `ItemPresenter` fails the
  gate for any kind that hasn't implemented it (totality assert /
  mypy). `/drive` and `/papers-needed` render through the unified list.
- **Slice 4:** a filtered list can be handed to an authoring job whose
  context is exactly that query's refs.

## Target + blast radius

- **Slice 1:** new `src/precis_web/routes/flags.py`; new
  `templates/_flag_buttons.html.j2`; edit
  `routes/papers_needed.py` + `templates/papers_needed/index.html.j2`;
  new batched `store` tag-state method (`_tags_ops.py`); register router
  in `app.py`. No migrations. No worker changes.
- **Slice 2:** `handlers/*` search surface + `store/_refs_ops.py`
  (multi-kind `list_refs` / lexical + a cross-kind semantic union).
- **Slice 3:** `protocol.py` (KindSpec + `ItemPresenter`), a presenter
  per kind, a new unified-list route + template; eventual retirement of
  `/drive`, `/papers-needed`, `/papers/triage`, `/refs`, `/tags/refs`.
- **Slice 4:** the job substrate (a `draft_from_query` job_type) + the
  authoring prompt's retrieval step.

## Open questions / decisions log

- **DECIDED — additive, not rip-and-replace.** Build alongside the old
  pages, retire once proven (matches the repo's ship-dark ethos).
- **DECIDED — two domains, one surface.** Author/source is a `kind`-set
  *facet* of one unified list, not two separate pages — because the
  retrieval layer is uniform and a tailored view doubles as an LLM
  context set. (Earlier lean toward two views reversed on this.)
- **DECIDED — presenter is a base class with a generic default first,
  promoted to `@abstractmethod` once every kind adopts.** You cannot
  have both incremental adoption and the check-time-totality guarantee
  on day one; the default-render road stays shippable, the end state is
  the hard contract.
- **DECIDED — no legacy-alias burden on the LLM surface.** A fresh LLM
  instance re-reads the current skills/overview every session (no memory
  of old kind names), so kind renames / merges / retirements need **zero**
  backward-compat aliases for the LLM. Only stored refs + non-LLM
  consumers (web routes, workers) need data migration. *The interface is
  free to change; the data isn't.* This is the license for an aggressive
  taxonomy cleanup (the coupled workstream above).
- **DECIDED — search is chunk-level; the matching chunk is the preview.**
  Semantic + keyword both run over chunks (`chunk_embeddings` + the
  keyword index), fused per ref (best chunk per ref for the breadth /
  triage row). `preview(query)` for a text kind returns that winning
  chunk (highlighted); a visual kind with no chunk hit returns a
  thumbnail. So the retrieval unit is the chunk, the row unit is the ref,
  and the preview cell is the chunk that made the ref match.
- **DECIDED — source spans cached external answers, not just documents.**
  The source kind-set is *every searchable non-artifact kind* — ingested
  documents **and** cached answers/references (`perplexity-*`,
  `websearch`, `wikipedia`, `web`, `youtube`, `semanticscholar`,
  `calc`/wolfram, `math`, `oracle`). Membership is gated by "has a
  presenter," not a `role` query, so ops/machine kinds stay out and new
  cached kinds opt in by implementing one.
- **OPEN — flag namespace.** `OPEN:read-later` (bare tag) as in Slice 1,
  vs a dedicated closed axis. Bare `OPEN:` chosen for v1 (no vocab
  machinery); revisit if the OPEN-namespace teardown lands.
- **OPEN — Slice 2 breadth vs depth.** One-best-hit-per-ref (triage) vs
  ranked chunks (depth); probably one param serving both.
- **OPEN — visual-kind thumbnail investment for Slice 3.** kind-icon
  (free) → cached still (a render+cache pass) → live-on-hover. Text
  kinds need nothing new either way.
