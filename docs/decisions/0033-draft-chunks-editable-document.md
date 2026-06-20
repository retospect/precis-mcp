# ADR 0033 — Draft-as-chunks: an editable document kind

- **Status**: accepted (2026-06-20)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0006 — Tri-identifier scheme](./0006-tri-identifier-scheme.md)
  - [ADR 0007 — Derived-queue, no block jobs](./0007-derived-queue-no-block-jobs.md)
  - [ADR 0010 — Postgres/pgvector system of record](./0010-postgres-pgvector-system-of-record.md)
  - [ADR 0017 — Derived-queue family](./0017-derived-queue-family.md)
  - [ADR 0027 — Reparent via parent link](./0027-reparent-via-parent-link.md)
- **Does NOT supersede**: the append-only invariant on ingested-paper
  body chunks stays exactly as-is. This adds a *new, explicitly
  mutable* kind alongside it.

## Context

Projects (strategic-root todos that own a `meta.workspace`) currently
author their document as `.tex` files on disk under `PRECIS_ROOT`
(`utils/workspace.py` → `ensure_initialized` lays down `main.tex`,
`tex/`, `pics/`, `data/`, `refs.bib`, a git repo). The document lives
outside the chunk store, so it gets none of the corpus infrastructure
(embeddings, keyword index, search, TOC, neighbor windows) and the
agent edits it as opaque text files.

We want to **move the authored document into the chunk store** — make
Postgres the system of record for the draft itself — for three
reasons:

1. **Reuse the infra.** Everything that already works on chunks
   (embed, `chunk_keywords`, summaries, search, `view='toc'`,
   neighbor windows) then works on the draft for free.
2. **Annotation-driven authoring.** The human does **not** edit prose
   directly. The human steers via (a) the design **brief**
   (`meta.workspace.brief`, which already cascades into the planner
   prompt) and (b) **change requests attached to a paragraph or
   section**. The agent (planner coroutine + jobs) consumes those and
   regenerates the affected chunks.
3. **Export, don't store, LaTeX.** The final artifact still renders to
   LaTeX → PDF (Word as a downgrade), but `.tex` becomes an *export
   target*, not the source of truth.

This requires four properties to hold **simultaneously and without
anything going stale**: hierarchy, reading sequence, insertability,
and permanent addressability. The investigation that grounds the
decisions below verified the relevant schema:

- `chunks.chunk_id` is a standalone `BIGSERIAL PRIMARY KEY` — a real
  surrogate, no semantic load (`0001_initial.sql:191`).
- Every derived table keys on `chunk_id`, never on `(ref_id, ord)`:
  `chunk_embeddings(chunk_id, embedder)`,
  `chunk_summaries(chunk_id, summarizer)`, and `keywords` /
  `keywords_meta` live on the chunk row. **So reordering or
  reparenting a chunk cannot desync its derived data.**
- Append-only on paper body chunks is **convention + `UNIQUE(ref_id,
  ord)`**, not a trigger — nothing to fight.
- `embed` / `summarize` claim work via `LEFT JOIN ... WHERE
  o.chunk_id IS NULL` ("no derived row exists"). An in-place text
  `UPDATE` therefore would **not** re-derive — the stale-embedding
  trap. (`chunk_keywords` is safer; it already compares
  `keywords_meta->>'version'`.)
- A durable per-chunk slug mechanism **already exists** as precedent:
  `Block.slug` ← `chunks.meta->>'slug'`, matched by `get_block` on
  `(ref_id, meta->>'slug')` (`_blocks_ops.py:1073`); the selector
  grammar (`paper.py:_parse_paper_id`) parses numeric ordinals only.
  (We do **not** reuse `meta.slug`; §1 mints a dedicated global `handle`
  column instead — but this confirms per-chunk durable addressing is a
  known pattern.)
- `chunks.section_path` is a `TEXT[]` of section *labels* (a heading
  breadcrumb), GIN-indexed (`0001_initial.sql:200`).
- `ord` is `INT NOT NULL`, constrained only by `UNIQUE(ref_id, ord)`
  — gaps already allowed; the neighbor query is a range filter +
  `ORDER BY ord`, already gap-tolerant.

## Decision

Introduce a `draft` ref kind whose body chunks are **mutable in
structure and (via a single helper) in text**, addressed by stable
non-numeric anchors. The four properties map to **four orthogonal
columns**, which is why they coexist without staleness:

| Property | Mechanism | Mutates | Touches text? |
|---|---|---|---|
| Permanent addressability | `chunk_id` PK (internal) + minted 6-char `handle` (the only exposed handle) | never | — |
| Hierarchy | `parent_chunk_id` adjacency list (heading chunk owns its content + subheadings) | reparent = 1 UPDATE | no |
| Sequence | `pos TEXT` fractional/lexicographic key (`ORDER BY pos`) | reorder = 1 UPDATE | no |
| Insertability | `key_between(lo, hi)` → mint a key strictly between neighbors | insert = 1 INSERT | no |
| Never stale | `content_sha` compare per derived consumer (edit bumps it) | edit | yes (only here) |

### 1. Identity & addressing — non-numeric only

For a `draft`, the **only** user/agent-facing chunk handle is a
**minted, fixed-length, opaque handle**: a random **6-character base-58**
string (no-ambiguous-chars alphabet, dropping `0/O/l/1`) in a stored
`chunks.handle TEXT` column with a **global `UNIQUE`** constraint.
Numeric `~N` ordinals are **not offered** for this kind — they are
positional and rot on insert, and reading as an ordinal invites
confusion. The handle reads as opaque, not ordinal.

Why minted-random-global rather than a meaningful per-ref slug, or an
encoding of `chunk_id`:

- **Fixed length, unguessable, no order-leak.** A random 6-char handle
  doesn't grow with the corpus (unlike encoding the ever-larger PK)
  and doesn't leak creation order / enumerate (unlike the PK
  encoding). `58⁶ ≈ 38 B` — against ~10⁵–10⁶ draft chunks,
  birthday-collision per insert is negligible.
- **Minting is cheap — the `UNIQUE` constraint catches collisions.**
  Mint random, `INSERT`; on the (vanishingly rare) unique violation,
  regenerate and retry. Same mechanism as the `ord` serial.
- **No `ref_id` in the handle.** One flat global address (~2 tokens),
  terser than a doc-qualified or meaningful slug. Enables a flat URL
  space (`/c/<handle>`).
- **Meaning lives in the heading text**, shown by the TOC/UI — the
  anchor needs no semantic content, so no slugify/uniqueness logic.
- *No-ambiguous-chars* (base-58) so the rare hand-typed/read-aloud
  handle isn't misread; humans mostly click badges, agents copy exactly.

**Cross-doc accident guard.** A global handle means a typo could
silently resolve to a valid chunk *in another document*. Mitigation:
validate at bind time. Whenever an anchor or `[¶…]` ref is bound we
always know the expected draft (the project owns it), so check
`resolved.ref_id == expected_ref` and reject on mismatch — `ref_id`
stays a **guard**, just not part of the handle. (A typo onto a valid
*same-doc* chunk is undetectable without a check digit; not worth it.)

The new machinery is the `handle` column + minting and **exposing the
handle form in the selector grammar** (`_parse_paper_id`), distinct from
numeric `~N`. Bare-inline sigil is `¶` (gating question 2).

Durable references (change-request anchors, links, citations) use:

- **subtree** — a single heading handle, meaning "this heading and all
  its descendants." The natural anchor for a chapter/section-level
  change request; resolves by `parent_chunk_id` walk and is invariant
  under reorder. Preferred for "rework this chapter."
- **span** — a pair of handle endpoints, resolved as the slice of the
  DFS linearisation between them (`row_number()` over the recursive
  order). Survives inserts; material added between the endpoints joins
  the span. For arbitrary cross-boundary selections.
- **frozen set** — an explicit list of handles, when "exactly these
  paragraphs, nothing new" is required.

**No *absolute* ordinal addressing — handle-relative windows instead.**
`~N..M` is not offered for drafts; the navigation primitive is a
**handle-relative window**: `¶<handle>-B+A` = the chunk plus B before and
A after in DFS reading order (each part optional: `¶c+3`, `¶c-5`,
`¶c-5+3`). Resolution: DFS-linearise, find the anchor's row, return
`[r-B, r+A]`, clipped at doc bounds, retired chunks excluded. It is
**handle-anchored** so the *center* is stable as order shifts around it
(unlike `~N..M`, whose center drifts on insert). It is a **transient
navigation view**, never a durable reference — durable anchors stay
handle / span / frozen-set. This is the ergonomic handle for the
"DFS prev/next N around a handle" editing read (§ reading interface).
Absolute `~N..M` survives only for **papers** (frozen corpus), where
nothing inserts so it cannot rot.

### 2. Sequence — sibling-scoped fractional key, never renumber

Add `pos TEXT`, holding a lexicographically-sorted
fractional-indexing key (Figma/Notion / Jira-LexoRank):
`key_between("m","t") → "p"`, `key_between(start,"m") → "g"` — you can
always mint a key strictly between two others, so insert/move never
renumbers. (A float would exhaust double precision after ~50 inserts
into one gap; the string key does not.)

`pos` is **sibling-scoped**: it orders a chunk only among chunks
sharing its `parent_chunk_id`. The global reading order is a
**recursive depth-first walk** — root's children by `pos`, recurse —
materialised at query time as a path-of-`pos` sort key
(`WITH RECURSIVE ... ORDER BY sort_path`). This is the key choice: it
makes a **subtree move O(1) writes**.

Worked example — reorder Section 1's children from A, B, C to B, A, C
(each chapter carrying its whole subtree):

```
children of Section 1:   A.pos="m"  B.pos="t"  C.pos="x"   → A, B, C
mint B.pos = key_between(start,"m") = "g"
UPDATE chunks SET pos='g' WHERE chunk_id = <B>            -- one row
                         A.pos="m"  B.pos="g"  C.pos="x"   → B, A, C
```

A and C are untouched; **none of B's descendants move** — their
sibling-relative `pos` is unchanged, and the DFS reaches B's subtree
earlier automatically. This is *not* a reparent: every
`parent_chunk_id` is unchanged. And because no `text` changed, nothing
re-embeds.

**Verb surface — intent, not arithmetic.** The agent never computes
`key_between` or writes `pos`; it sends a move *intent* with handles
and the handler does the arithmetic. There is no "move" verb, so a
move is an **`edit`** carrying a `move` arg:
`edit(kind='draft', id='¶<B>', move={before:'¶<A>'})`. The grammar
(handles, all parts optional): `{before|after: '¶x'}` (reorder),
`{parent:'¶<heading>', after:'¶x'}` (cross-section move),
`{into:'¶<heading>', first|last:true}` (boundary). `move.parent` is the
**ergonomic surface**: the agent never touches `parent_chunk_id`
directly — the move helper writes it (a chunk-level column, not a
ref-level link), honouring ADR 0027's principle that structure changes
go through a sanctioned op rather than a raw write. `edit` is the
unified mutation verb: a `text=` edit changes content (→ `content_sha`
→ reembed); a `move=` edit changes structure (`pos` + `parent_chunk_id`,
no text → no reembed). The handler resolves handles → `chunk_id`s,
computes `pos`, writes, logs a `moved` `chunk_event` —
`pos`/`key_between`/`chunk_id` never appear on the wire.

`pos` is short — typically 1–3 chars, growing ~1 char per repeated
insert into the *same* gap. Repeated `key_between` at one spot
lengthens keys; a periodic **per-sibling-group rebalance** (re-stamp
one parent's children with evenly-spaced keys) keeps them short —
local and cheap, never global. **No prefix/bucket is needed**
(unlike Jira LexoRank's `0|`/`1|`): because `pos` is sibling-scoped it
is only ever compared `WHERE parent_chunk_id = X ORDER BY pos`, so
keys never compete across groups and rebalance is local.

`ord` is retained only to satisfy `NOT NULL` + `UNIQUE(ref_id, ord)`:
for a draft it is a per-ref **monotonic insertion serial**
(`max(ord)+1`), never reordered and never displayed.

**Traversal cost (the trade-off).** Serial extraction — export,
"next 5 chunks", whole-doc render — is no longer a flat `ORDER BY
pos`; it is the recursive DFS above. For a single draft
(hundreds–low-thousands of chunks) that recursive sort is cheap. A
materialised `sort_path` column would restore flat `ORDER BY`, but
caching it reintroduces the O(subtree) rewrite on every move — so we
keep the sort query-time until read latency is shown to bite, then
cache as a pure optimisation behind the same edit/move helpers.

### 3. Hierarchy — adjacency list, numbers computed

Add `parent_chunk_id BIGINT REFERENCES chunks(chunk_id)`. A heading is
itself a chunk (`chunk_kind='heading'`) that owns its paragraphs and
sub-headings. TOC = walk the heading chunks. Reparent = update one
pointer (consistent with ADR 0027's reparent-via-link philosophy for
todos). The displayed section number (`1.2.3`) **and the heading
level** (chapter/section/subsection) are **computed at render time**
from tree depth/position — never stored, so an inserted chapter
renumbers nothing and a reparent re-levels automatically.
`section_path` (label breadcrumb) is kept as a **derived** cache for
GIN search/display compatibility, recomputed when the tree changes.

**The FK targets `chunk_id`, not `handle`** — `handle` is also unique and
would work, but every other internal reference in the chunk family
(`chunk_embeddings`/`chunk_summaries`/`chunk_events`) keys on
`chunk_id`, so hierarchy does too for one internal join key.
`parent_chunk_id` is internal plumbing (reparent via the `parent` link
/ UI, never hand-written), so it sits on the internal side; the UI
**displays the parent's `handle`** (resolved), giving external
handle-uniformity without an inconsistent FK.

### 4. Freshness — content-addressed dirty, per derived consumer

A chunk's text has **three independent derived consumers** (`embed`,
`summarize`, `chunk_keywords`). A single `dirty` boolean on the chunk
cannot represent "embedding stale, summary current" — whichever worker
runs first clears it and the others wrongly skip. The correct dirty
signal is therefore **content-addressed and per derived row**:

- Add `chunks.content_sha` (hash of `text`), set by the edit helper in
  the same `UPDATE` as the text.
- Each derived row records the `content_sha` it was built against
  (`chunk_embeddings.content_sha`, `chunk_summaries.content_sha`;
  keywords already carry `keywords_meta->>'version'` — extend the
  envelope with the sha).
- Each worker's claim predicate becomes `... OR o.content_sha IS
  DISTINCT FROM c.content_sha` alongside the existing `o.chunk_id IS
  NULL`. Workers stamp the sha on write (`ON CONFLICT DO UPDATE`).

This is the per-consumer-correct generalization of the version-compare
`chunk_keywords` already uses. It is self-scaling (a new consumer just
compares the same sha) and has **no visibility gap** — the stale
derived row stays live until overwritten, rather than the chunk going
un-searchable.

All draft text writes funnel through one helper that does the
`UPDATE chunks SET text=?, content_sha=?`; the workers re-derive on
the sha mismatch. Reorder (`pos`) and reparent (`parent_chunk_id`)
never touch `text`/`content_sha`, so they correctly never re-derive.

**WIP freshness — prioritize + refresh-on-access, split by cost.**
A freshly-edited draft chunk must not wait behind the paper
backlog, and the agent must not read stale derived data for what it is
looking at. Two moves over the `content_sha` staleness signal:

- *Prioritize WIP in the claim queues* — add `ORDER BY (ref.kind=
  'draft') DESC, recently-edited DESC, chunk_id` to the
  embed/summarize/keyword claims so draft chunks are claimed
  first. WIP volume is tiny vs. the corpus, so it wins the front and
  clears in a tick or two without starving papers (independent-queue
  / `PRIO` pattern).
- *Refresh on access, by cost* —
  - **Keywords (local, KeyBERT)**: recompute **inline in the edit
    helper** right after the `content_sha` bump — fast and local, never
    stale post-edit.
  - **Embedding (a service, ADR 0020)**: **best-effort inline**, but if
    the embedder is unavailable, **fall back to the prioritised async
    queue** (the `content_sha` mismatch re-claims). So editing never
    blocks on / fails with the embedder being down.
  - **LLM gist (`llm_summarize`)**: too costly to compute on every
    stale read, so prioritized-background (above) **plus** a bounded
    on-access top-up — when `get()` returns draft chunks, the
    handler synchronously refreshes only the *returned* stale ones if
    that set is small (it usually is — the stale set is just the
    chunks edited since the last pass), else serves current with a
    `refreshing` flag. The stale set being tiny for interactive work
    is what makes synchronous top-up affordable.

`content_sha` is **in-place** — the chunk row, its handle, and all
references persist across the edit, and there is no visibility gap.
(Rejected: *delete-on-edit*, which deletes only the derived
`chunk_embeddings`/`chunk_summaries` rows — the chunk and its handle
survive, so it does not actually break references, but it leaves a
brief window with no embedding. In-place `content_sha` avoids even
that, so it is the chosen route.)

Because drafts are not written furiously, the "always current" feel is
achievable: the cheap/local derivations are recomputed **inline at
write** (above), so they are current the instant the write returns; the
only deferred consumer is the LLM gist (a paid, seconds-long call that
agent bulk-rewrites would otherwise serialise), which `content_sha` +
WIP-priority + on-access top-up keep current-enough without coupling
the edit to the model.

### 5. Steering & execution — existing machinery

- **Brief**: edit `meta.workspace.brief` (already cascades into the
  planner prompt's variable layer).
- **Change request**: a `kind='todo'` with `parent_id` = the project,
  `meta.anchor` = a handle / span / frozen set, **and a `targets`
  link** to the anchored chunk. The link makes it a bidirectional edge
  (§ Reference graph): the chunk's backlinks show its outstanding
  notes, and the request flows into the todo tree → `dispatch` → jobs.
  The target is a **handle (immutable)**, so this link is **minted once
  and stable** — no ongoing sync (unlike inline-marker links, which
  re-derive on text edit). If the anchored chunk is edited/moved it
  still resolves; if retired, the dangling-token policy flags the todo.
- **Decomposition is the executor's decision, not the request's.** We
  do *not* pre-classify a note as leaf-vs-fan-out. The planner/executor
  decides whether to do it in one job or fan out a child per section
  (an `LLM:*` planner tick may fan out; a leaf job may handle it
  directly) — the request just carries the anchored intent. A leaf todo
  can thus *become* a parent at execution: the job mints children, lets
  dispatch work them, waits (`child_job_succeeded`), then returns and
  finishes.
- **List & link the undone ones.** Open requests are listed (web "open
  requests" panel) *and* discoverable per-chunk via the `targets`
  backlink, so every chunk surfaces its outstanding notes and none go
  lost.
- **Prioritise — the draft is actively worked.** Its open change
  requests get elevated `PRIO` (the int 1..10 column) / surface ahead
  of background todos in the doable rotation — the todo-execution
  analogue of the WIP derived-data prioritisation (§4).
- **Notes are todos, not a new kind — and they can wait.** A
  "note of what to do" anchored to a chunk is just the change-request
  todo above. Because it lives in the todo tree it carries
  `meta.auto_check` (`paper_ingested`, `time_past`, `tag_present`, …),
  so *"expand this once paper X is ingested"* **defers itself** until
  the dependency clears, then the planner/dispatch acts. No new note
  concept, no new scheduler.

### 6. Migration — one additive, nullable, no backfill

A single forward migration adds, all nullable / behavior-preserving:

- on `chunks`: `handle TEXT` (minted 6-char base-58, **global `UNIQUE`**
  index), `pos TEXT`, `parent_chunk_id BIGINT REFERENCES
  chunks(chunk_id)`, `content_sha TEXT`, `retired_at TIMESTAMPTZ`.
- on `chunk_embeddings` and `chunk_summaries`: `content_sha TEXT` (the
  sha the row was derived against), plus the `OR content_sha IS
  DISTINCT FROM` clause in their claim queries.
- new `chunk_events(event_id BIGSERIAL, chunk_id BIGINT REFERENCES
  chunks(chunk_id), ts TIMESTAMPTZ, event_kind TEXT, content_sha TEXT,
  prev_text TEXT, source JSONB)` — append-only lifecycle/undo/
  provenance log (gap 2).

For papers, `content_sha` stays NULL (no backfill); the derived row's
`content_sha` is also NULL, and `NULL IS DISTINCT FROM NULL` is false,
so the new claim clause never fires — existing behavior is preserved.
The chunk-level `handle`/`pos`/`parent_chunk_id`/`retired_at` stay NULL
for papers. Regenerate
the baseline snapshot per ADR 0031 (`scripts/bump` / `precis db
dump-schema`); do not hand-edit it. Forward-only per ADR 0005.

### 7. Export — target-parameterized render

The DFS render pass is parameterized by target; it resolves the same
token classes (section numbers, `[¶…]`, `[§…]`, glossary) into whichever
output, from the same handle/slug → entry maps. Postgres stays canonical;
exports are one-way (no round-trip back to chunks).

- **LaTeX → PDF** — the high-fidelity target. The `format='tex'`
  workspace becomes an export target, not the document store.
- **Word (.docx) — the downgrade, citations intact.** Driven by
  **pandoc + citeproc**, *not* LaTeX→docx (which mangles
  bibliographies). The render emits a pandoc intermediate
  (pandoc-Markdown with `[@paper-slug]` citations) plus CSL-JSON / a
  `.bib` built from the `rel='cites'` data; `pandoc --citeproc
  --csl=<numeric/endnote-style>` resolves citations into a **basic
  endnote format** (`[1]`, `[2]`… + numbered reference list) that
  survives intact. `\citequote` verbatim quotes can be carried into
  the endnote. (Native Word endnote *fields* via OOXML/python-docx is
  a heavier optional upgrade, only if Word's live endnote feature is
  wanted over a correct reference list.)

## Consequences

- The draft gets search, TOC, neighbor windows, and the keyword
  index for free, because it is genuinely chunks.
- Hierarchy, sequence, insertability, and permanent addressability
  hold simultaneously because they live on independent columns; the
  only re-derivation trigger is a text change, funneled through one
  helper.
- The append-only paper invariant is untouched; mutability is scoped
  to the new kind.
- Risk concentrated in the edit helper: a raw `UPDATE chunks SET
  text=...` that bypasses it reintroduces the stale-embedding bug.
  Mitigation: make it the only sanctioned write path for the kind;
  consider a guard.

### 8. Source markup vs. rendered output

A unifying rule that several features (section numbers, the glossary,
cross-refs, citations) all obey:

> **Anything in the output that depends on position or number is a
> stable-id-keyed token (handle or slug) stored in the chunk's source
> text and resolved at render time — never stored already-resolved.**

Chunk `text` is a **markdown-ish dialect** — markdown prose, `$…$` /
`$$…$$` math (KaTeX-subset LaTeX), and inline **references that are
markdown links**. The source is **not** LaTeX; the render pass maps it
per target (LaTeX commands for the PDF, HTML+KaTeX for web, pandoc for
Word). One syntax family, the renderer **dispatching on the target**:

- **Web link** `[text](https://…)` → `\href` / `<a>` (standard markdown).
- **Section numbers** — no token; computed from tree depth + sibling
  `pos` (§3).
- **Cross-reference** `[¶<handle>]` (bare) or `[text](¶<handle>)` → a
  chunk in *this* draft → computed §/number (LaTeX `\ref`/`\label`).
  rel `references`.
- **Citation** `[§<paper>~<n>]` (bare) or `[text](§<paper>~<n>)` → an
  external corpus chunk → `[n]` + `refs.bib` entry (LaTeX
  `\cite`/`\citequote`, carrying `source_quote`). rel `cites`. Reuses
  the frozen `~N` selector (safe — papers don't insert). **Prefer a
  deep ref** — `~<n>` to the *exact* paper chunk that holds the cited
  detail, not the whole `§<paper>` — and capture its `source_quote`;
  the citation skill prompt instructs the agent to locate and cite the
  precise passage (the deep handle is also a backlink into that paper).
- **Glossary term** `[surface words](¶<term-handle>)` → a
  `chunk_kind='term'` chunk (display = the surface words, **inline** so
  embed-strip is local; the linkify pass inserts it, humans never type
  it). Render: web → short form + mouseover; LaTeX → `\newacronym` +
  `\gls` (define-on-first-use + `\printglossary`). It **never** stores
  the expanded/abbreviated string — first-use is position-dependent.
- **Authoring reference** `[[<address>]]` → a **thought** (memory /
  think / finding / oracle / …) or any precis object. Thoughts are
  **referenceable, not citeable**: they get an authoring **link** (rel
  `derived-from`/`related`), surfaced in the links/backlinks panel, but
  **never** become a citation or `refs.bib` entry and **render to
  nothing** in output. (Only papers `[§…]` are citeable; only this
  draft's sections `[¶…]` are cross-referenceable.)

These bracket forms are **markdown extensions** the parser recognises
(`[¶…]`/`[§…]`/`[[…]]` are *our* refs, not markdown shortcut links); a
plain `[text](url)` stays a normal link. Author-only refs may carry
display text via `[text]([[address]])` or render nothing when bare.
Every reference also materialises a `link` edge (§ Reference graph), so all
references — serializing or author-only — are queryable backlinks.

**Indexing implication to record:** the embed/search input is the
marker-stripped source above — a pure function of the chunk's own
`text` — so `content_sha = hash(text)` (§4) fully determines it, with
**no cross-chunk re-embed** when a term/heading elsewhere changes.
**Markers are byte-stable** because they key on the immutable handle, not
resolved values: reorder / renumber / target-rename / term-redefine all
change the *rendered* output (computed at render) but never the stored
marker, so only a real edit of *this* chunk's source bumps the sha
(→ correct reembed). This is why the glossary token **wraps the surface
words inline** (`[the words](¶<term>)` → "the words") so the strip is
local (no registry lookup); `¶`/`§` cross-refs/citations carry no prose
surface and are dropped on strip.
Display/export resolution (long-form first-use, computed numbers) is
separate and not embedded. The exporter is a single DFS pass resolving
all token classes against current state.

**Editor authoring loop — term awareness without per-term calls.**
the linkify pass emits `[surface](¶<term-handle>)` glossary markers for
registered terms (the agent writes natural prose; the pass wraps). This is
achieved with *three* mechanisms, none of which is an O(terms) MCP
round-trip:

- *Read (awareness)* — the whole glossary (`handle | short | long |
  surface-forms`) is injected once into the editor prompt's
  **variable layer**, via a tracked `planner_prompt._render_glossary`
  (sibling of `_render_project_brief`; per-draft, so variable
  not cached layer). One server-side query of the draft's
  `chunk_kind='term'` chunks; the model sees the complete list and
  makes no resolution calls.
- *Guarantee (completeness)* — a deterministic **linkify pass**
  (Aho-Corasick / trie over the registry surface-forms) runs after
  the model returns and wraps every registered occurrence in
  `[surface](¶<term-handle>)`, skipping text already inside a marker, headings,
  defining contexts, and math/verbatim chunks. This — not the model's
  diligence — is the source of truth, so "markers used exclusively
  where a term exists" holds by construction. The model's own markers
  are honored as intent (sense-disambiguation).
- *Extend (new terms)* — the model registers a new abbreviation via
  `put(kind='term', meta={short, long, surface_forms})` (or proposes
  it for human approval). The only point terms touch the tool surface.

Resolution to first-use/glossary (LaTeX) or mouseover (web) is the
batch DFS render pass — never per-term MCP calls.

**Editor prompt composition — always-on identity + agent-selected
skill menu.** No human dropdown and no pre-classifier; two layers:

- *Always-on (identity)* — brief, **style guide**, glossary define
  *this draft* and are injected into every edit regardless of
  task (variable layer, alongside `_render_project_brief` /
  `_render_glossary`). Not a choice — you don't pick the house style.
- *Agent-selected task skills* — the ~10 task-type skills
  (structural-edit, citation, decomposition, …) are pre-listed as a
  **menu** (id + one-line description) in the **cached system layer**
  (static across drafts → ~free per tick). The executing agent
  picks the 1–2 relevant ones and `get(kind='skill', id=…)` loads
  their bodies into context (progressive disclosure). This is the
  existing skill-store idiom with the menu pre-listed instead of
  searched.

This is a **closed-set pick from an enumerated in-context list** —
technically a model choice, but bounded, inspectable, and reliable;
*not* the open-ended vector-retrieval nondeterminism that pre-listing
deliberately avoids. No classifier, no mode→module totality table to
maintain; adding a skill is adding a menu line. Log the loaded skill
ids per tick for audit; pin a skill for a specific case only if a hard
guarantee is ever needed.

Read fidelity needs no classifier either: it is agent-driven
progressive disclosure — start at the outline table, `get(handle)` for
verbatim where acting (see the reading interface below).

**Agent reading interface — progressive disclosure, not a full dump.**
What the agent sees per chunk is altitude-adaptive and agent-driven;
showing full verbatim for the whole document is the token trap. Being
chunks is what makes the cheap map possible:

- *Universal* — every shown line carries its **handle**, so the
  agent can target an edit/anchor with no lookup.
- *Navigation / structural read* — a compact **outline table**, one
  row per chunk: `handle | depth/heading-path | gist` (the
  `llm_summarize` gist is the "meaning" column). ~10 tokens/chunk, so
  a 500-chunk doc (~5k tokens) is affordable to show *whole* — the
  map for locating/reorganising without reading prose.
- *Editing read* — `get(handle)` / subtree returns the **verbatim
  source text** (markdown-ish, markers intact: `[¶…]`/`[§…]`/`[[…]]`)
  of the target plus its DFS prev/next N neighbours, tagged with handles.
  Full fidelity, scoped to the region being edited — never whole-doc.
- *Keywords / embeddings* are retrieval signal powering the `search`
  verb, **not** shown inline — the prose gist conveys "what this is
  about" better than a keyword array.

The agent navigates the cheap outline first and pulls verbatim only
for the region it acts on — no classifier gates this; it is ordinary
progressive disclosure via `get(handle)`.

### 9. Chunk types & math rendering

A draft's chunks are typed by `chunk_kind` (the existing
`TEXT NOT NULL REFERENCES chunk_kinds(slug)` column). The new draft
kinds are registered as seed rows in `chunk_kinds`; they are `ord ≥ 0`
+ non-`card_%`, satisfying the existing card CHECK. There is a **single
`heading` kind** (no per-level chapter/section/subsection kinds); a
heading's **level is computed *exclusively* from depth** — walk
`parent_chunk_id` to the root (O(depth), cheap), never stored — so it
re-levels automatically on reparent. The rest follow one principle:

> Every typed chunk has a prose **face** (`text`) and an optional
> verbatim **payload** (`meta`). Two orthogonal axes follow from it:
> the **edit register** (how the agent authors it) and **embedding**
> (which always targets the face).

**All *text/markup* kinds are agent-regeneratable** — the question is
the register, not whether. Prose-register chunks are rewritten as
prose; markup-register chunks (equation, code, table data) are authored
as verbatim markup by the math/code skill, so the prose-rewrite pass
never "smooths" the LaTeX or reflows code and breaks it.

**Images are the exception, and graphs a special case — deferred to a
next phase.** An image payload is an *asset* the agent attaches/swaps
but cannot author (it can only regenerate the caption *face*). A
**graph/plot** is an image payload backed by a raw-data **supplement**,
so the figure is regenerable *from the data* via a render step (data →
plot) — a generative pipeline, not text. This ADR covers their prose
face (caption) and attachment; **image/graph payload handling,
including data-supplement-driven plot regeneration, is next-phase.**

**Embedding targets the face**, and is skipped only when there is no
face — *not* because a kind is un-embeddable. A bare formula/code
block has no prose to embed (and raw LaTeX/code embeds poorly in a
prose embedder), so it is `no_index`; a *captioned* one embeds its
caption.

| kind | face (`text`) | payload (`meta`) | edit register | embed |
|---|---|---|---|---|
| `heading` | title | — | prose | title |
| `paragraph` | prose (face is all) | — | prose | yes |
| `equation` | optional label | LaTeX source | markup (math) | face if present, else `no_index` |
| `figure`/`image` | caption | image asset | prose caption; payload is an **attached asset**, not authored | caption |
| `graph`/`plot` *(next phase)* | caption | image asset + raw-data supplement | caption now; **plot-from-data regen deferred** | caption |
| `table` | legend | cells / LaTeX `tabular` | prose face + markup payload | legend |
| `aside`/`callout` | aside prose | box style | prose | yes |
| `listing`/`code` | optional caption | verbatim code | markup (code) | face if present, else `no_index` |
| `term` | definition | `{short, long}` | prose | yes |

The face/payload split is why captions/legends — not raw cell data —
are the searchable text. `aside`/`callout` (admonition box:
`tcolorbox`/`mdframed` on export) is a first-class type;
`listing`/`blockquote` added if docs need them. `citation` is **not**
a chunk — it is an inline token. The edit register is the same signal
the skill menu keys on (math skill vs prose style guide).

**Keep this DRY — no per-kind exceptions.** Embedding is one uniform
rule ("embed `text` if present"); a bare equation skips *because* it
has no face, not via a special case. Kind-specific data lives in
`meta`, **never a new per-kind column**, and there is **no per-kind
embedding branch**. The only kind-aware code is render dispatch
(unavoidable — every doc system renders a figure ≠ an equation) and
the one-line edit-register lookup. Emergent nicety: to make a
formula/table findable you give it a caption (its face) — the model
nudges you to describe it rather than special-casing searchability.

**`chunk_kind` does triple duty + fail-fast validation.** The same
discriminator drives render dispatch, edit register, *and* rejecting
inappropriate ops at the boundary: (a) DB — `chunk_kind` FK rejects
unknown kinds, card CHECK + `UNIQUE`/`NOT NULL` stand; (b) relational
guard (trigger or handler) — **only `heading` may be a parent** (a
paragraph can't own children, can't be reparented under a non-heading)
and the §1 same-ref guard; (c) register guard (handler) — a
prose-rewrite op targeting a markup-register chunk (equation/code) is
rejected, as is a missing required payload (`figure` needs `meta.src`).
One discriminator, one place to "barf quickly."

**Math** is a render-target capability, not new storage: the source is
authored in **LaTeX math syntax** (`\frac`, `\int`, …) with `$…$`
(inline) / `$$…$$` (display) delimiters. Display `equation` chunks
*and* inline `$…$` inside prose both render via **KaTeX** on the web
(client-side auto-render, no server step; fast/light, preferred over
MathJax for authoring) and pass the *same source* through verbatim to
LaTeX export. Caveat: KaTeX supports a large *subset* of LaTeX math —
stick to it and it is write-once (web == PDF); shared `\newcommand`
macros from the doc preamble are fed to KaTeX's `macros` option so the
web matches the LaTeX preamble.

## Gaps surfaced by the completeness pass

The original sketch covered insert / move / edit but missed several
things an authored, evolving document needs. Recommended resolutions:

1. **Deletion is soft (retire), not hard.** Delete sets
   `chunks.retired_at`; the row, its handle, and its history persist.
   Retired chunks are filtered out of reading order / DFS / TOC /
   export / search (`retired_at IS NULL`; embedding dropped). The win
   is anchors: a change-request todo's `meta.anchor` handle still
   **resolves — to a retired chunk** — so the dependent todo is flagged
   via the existing nursery orphan path instead of mis-resolving. The
   retired row *is* its own tombstone (no separate tombstone table).
   Un-retire = restore (free undo). Handles are never reused, so a
   retired chunk keeps its handle reserved. Hard delete is an admin-only
   purge. **A draft is never empty:** `delete` refuses to retire the
   **last live chunk** (combined with the auto-minted title heading at
   create, a draft always has ≥1 live chunk — the empty case is designed
   out).

   **Deleting a *heading* is ambiguous — never orphan.** A child under
   a retired heading has a retired ancestor and silently drops out of
   the DFS/TOC, so orphaning is a broken state, not an option. Offer
   two explicit operations instead:
   - *Retire subtree (cascade)* — retire heading + all descendants
     ("delete section and contents"). Reversible via un-retire-subtree.
   - *Remove heading / promote children (unwrap)* — retire only the
     heading and splice its **direct** children up to the heading's
     parent, into the heading's former `pos` slot (relative order
     preserved); deeper descendants follow automatically, so it is
     O(direct-children). Re-levels promoted children by one depth
     (subsection→section, since level = depth) — surface that in the
     UI. Reversible via the `chunk_events` `reparented` rows.

   Empty heading (or leaf) → unambiguous, just retire it, no
   parameter. Non-empty heading → the op is ambiguous *and*
   consequential, so **`mode=cascade|promote` is required — no
   default, not even `promote`** (`cascade` destroys a section;
   `promote` silently re-levels content — guessing either is wrong).
   Recoverability via un-retire is the safety *net*, not a licence to
   default: the required mode prevents the surprise; undo only repairs
   it. UI: two buttons ("delete section **and contents**" vs "remove
   heading, **keep contents**") *are* the disambiguation. Agent:
   states `mode` from the change-request intent; a request that
   doesn't disambiguate is a cue to ask the user, not to guess.
2. **Per-chunk lifecycle log = undo + provenance, one table.**
   Append-only `chunk_events(event_id, chunk_id, ts, event_kind,
   content_sha, prev_text, source)`, idiomatic alongside `ref_events`
   / `job_event` forensics:
   - `event_kind ∈ {created, edited, moved, reparented, retired,
     restored}`.
   - `edited` rows carry `content_sha` + `prev_text` → **revision
     history / undo / diff** (subsumes a separate `chunk_revisions`).
   - `moved` / `reparented` carry from/to in `source` (no text — cheap).
   - `source` (JSONB) records the **cause** — change-request todo id,
     planner job, brief sha, actor → **provenance** ("what produced
     this paragraph"), pairing with the `provenance` kind.
   The edit / move / retire helpers each append one row; low volume
   (human-paced + agent edits), so cheap.
3. **Concurrency (agent + human).** A job rewriting a chunk while the
   human reorders/edits in the web UI. Use **optimistic CAS on
   `content_sha`**: a write supplies the sha it read; the `UPDATE`
   succeeds only if it still matches, else the caller re-reads. `pos`
   moves are independent rows, so they rarely contend; concurrent
   `key_between` into the same gap is resolved by the per-group
   rebalance. (Job claiming already uses `FOR UPDATE SKIP LOCKED`.)
4. **Internal cross-references — a strength, not just a gap.** "As
   shown in §2" must survive renumbering, so an internal ref is a
   **handle link** rendered as the computed number. This means LaTeX
   `\label`/`\ref` fall straight out of handles at export — the stable
   anchor *is* the label. Worth building in deliberately.
5. **Citations & `refs.bib`.** The `citation` kind / `\citequote`
   flow and auto-generated `refs.bib` must keep working when the body
   is chunks, not tex: gather `rel='cites'` links off draft
   chunks at export and regenerate `refs.bib` then.
6. **Non-prose chunks.** See §9 (chunk types & math). All kinds are
   agent-regeneratable — the difference is the *edit register* (prose
   vs verbatim markup), and embedding targets the prose face (skipped
   only when there is none).
7. **Search scope is the searcher's job — drafts aren't special.**
   *All* chunks embed (drafts included); nothing is excluded at write
   time. Scoping is set per-query: global search **may** surface draft
   chunks, `paper`-scoped search filters them out by kind/tag. (This
   resolves the apparent tension with "search for free" in the intro —
   the draft is searchable; whether a given search *includes* it is the
   query's scope, not a property of the chunk.)
8. **Draft TOC is the heading tree, not the DP-cluster.** Unlike
   papers (`view='toc'` DP-clusters keywords at request time), a
   draft has *real* `chunk_kind='heading'` nodes — its TOC is a
   direct `parent_chunk_id` walk. Better, but a distinct code path;
   "TOC for free" in the intro overstates it.
9. **Heading level = tree depth.** Don't store a level integer;
   derive chapter/section/subsection from depth so a reparent
   *re-levels* automatically, and map depth → `\chapter`/`\section`/
   `\subsection` at export.
10. **Genesis / empty state.** See "Genesis, empty state & skill set"
    — the first-chunk MCP flow, empty-case `pos`/parent handling, and
    the skill set. (`format='tex'` workspace scaffold becomes
    export-only.)
11. **Reparent validation.** A `parent_chunk_id` must stay within the
    same ref and must not create a cycle — reuse the cycle/scope
    checks from ADR 0027's reparent-via-link.

## Reference graph — markers materialise as bidirectional links

Inline markers are the **canonical** source (they render and
serialise), but text-only references are not *bidirectionally*
discoverable — you can't ask "what points here?" without scanning every
chunk. So on every edit, the same helper that re-derives `content_sha`
**parses the markers and upserts first-class `link` edges** — a cheap,
local derived pass, recomputed **inline** (always current, like
keywords):

- `[§<paper>~<n>]` → `rel='cites'`
- `[¶<handle>]` → `rel='references'`
- `[surface](¶<term-handle>)` → `rel='uses-term'`

The marker stays canonical; the link edge is a **derived projection** of
it (re-synced on edit, never hand-maintained for inline refs). This
buys Xanadu-style **bidirectional discoverability** — backlinks ("what
cites `miller~34`?", "what references this section?", "what uses this
term?") become graph queries, not text scans — reusing the existing
`link` verb / rel graph. Two payoffs beyond navigation:

- **Efficient dangling-token policy.** Retiring / moving / merging a
  chunk → query its backlinks to find every referrer and flag them,
  instead of scanning all chunk texts.
- **Impact analysis** — "if I change this, what depends on it?" is the
  same backlink query.

Authored links (`rel='derived-from'`/`'related'` → a memory, prior art)
coexist in the same graph: hand-made, and they *don't* serialise. So
the serialize rule (§ Web interface) restates cleanly: **a link
serialises iff it has a backing inline marker** — and every reference,
inline-derived or authored, is now a queryable edge.

## Structural operations & reference integrity

"Delete a heading" is one of a class of structural edits whose
identity/anchor semantics are non-obvious. Two cross-cutting rules
handle most of the class:

- **Retire-with-redirect** (`meta.superseded_by` on the retired
  chunk). Operations that *replace* identity (merge, split, "rewrite
  as N paragraphs") retire the old chunk but leave a forward pointer,
  so an anchor to it resolves to the **successor**, not a dead
  "retired." Generalises the tombstone; chains are followed.
- **Dangling-token policy.** Any token to a retired/missing target
  (`¶`, `§`, `[[…]]`, a change-request `meta.anchor`) renders a
  **visible broken-ref marker and flags the owning todo** — never a
  silent drop. One policy for all token classes; the backlinks (§
  Reference graph) make finding the referrers cheap.
- **Required mode for ambiguous/destructive ops.** Like delete
  (`cascade`/`promote`), any op with a destructive or ambiguous-
  consequential branch **requires** an explicit mode when the
  ambiguity is present (e.g. a heading with children) — no default,
  destructive branch never implicit. Unambiguous cases (empty heading,
  leaf) need no parameter. Soft-delete/undo is the safety net, not a
  licence to default.

Operation catalogue:

| Operation | Resolution |
|---|---|
| Delete heading | cascade vs promote; never orphan (gap 1) |
| Promote / demote heading | reparent ±1 depth; explicit "subtree follows?" (default yes) |
| Split a chunk | original keeps its handle (first part; text edit → re-derive); new chunk minted for the remainder; anchors to original stay valid |
| Merge adjacent chunks | survivor keeps its handle; the other retires **with redirect** → its anchors follow to the survivor |
| Clone / duplicate a section | every cloned chunk gets a **new** handle (clone = new identity); originals' anchors unaffected |
| Rename a heading | plain text edit (`content_sha` bump) — no structural ripple; handle cross-refs still resolve (why handle-not-slug anchors win) |
| Move chunk to another document | explicit op; global handle survives; `ref_id`/tags/ownership update; change-requests from the *old* project flagged |
| Insert at a boundary | `key_between(start, first)` / `key_between(last, end)` with sentinel bounds |
| Span endpoint retired | flag the request (selection ill-defined), or fall back to nearest living endpoint |

All of the above are soft (reversible via `retired_at` / the
`chunk_events` log), touch text only where content actually changes
(so re-derivation stays correct), and preserve handles except where the
operation deliberately mints or supersedes identity.

## Web interface

A server-rendered view in `precis_web` (ADR 0026 — sibling package
calling the handler layer directly, like Tasks/Papers and the per-todo
PDF viewer).

**Entry — the draft hangs off its project; no free-floating picker.**
A draft is 1:1 with its project (`draft-of`), so the primary route is
**Project → "Open draft"** (the existing `view='projects'` dashboard
follows the link). A **Drafts index tab** (flat list, each showing its
project) is a convenience, not the main path. URLs are flat:
`/draft/<name>` for the document, `/c/<handle>` to deep-link a chunk.

Layout: a TOC rail + three panes:

- **TOC rail (far left)** — the heading tree (`parent_chunk_id` walk)
  with computed `1.2.3` numbers; click scrolls + selects.
- **Chunk meta panel (debug, "for now", toggle)** — for the selected
  chunk: `handle`, `parent`, DFS `prev`/`next`, `content_sha` + retired
  state, and the `chunk_events` history (created/edited/moved/…).
  Behind a toggle so the reading view can be clean later.
- **Document pane (center)** — chunks in DFS order, **resolved for the
  web target**: glossary term → short form + mouseover definition;
  `[¶…]`/`[§…]` → resolved marker with hover preview; click-a-link →
  open target in a **new tab** (paper chunk `[§miller~34]`, another doc
  section `[¶<handle>]`, a memory, …). Each chunk is a click-target that
  drives the meta/request/links panels. Retired chunks hidden (toggle).
- **Request / status panel (right)**, three sections:
  1. *Change-request box* — anchors to the selected chunk / its
     subtree (heading) / a span; structural/destructive ops surface
     the **required mode** as the two-button choice. Submit →
     `put(kind='todo', parent_id=project, meta.anchor=…, mode=…)`.
  2. *Open / unattached requests* — list with status (open / running /
     done / blocked) and **dangling ones flagged** (anchor target
     retired → broken-ref policy surfaces here).
  3. *Links panel* — outbound links from the chunk, split by the
     serialization rule (below).

**Serialize vs. not — the rule is location.** A link serializes
(appears in the export) **iff it is an inline token in the chunk's
source text**: `[§<paper>~<n>]` (→ citation + `refs.bib` + endnote),
`[¶<handle>]` (→ cross-ref number), `[surface](¶<term>)` (→ glossary).
The author-only `[[<address>]]` form and ref-level graph links via the
`link` verb (`rel='derived-
from'`/`'related'` → a memory, a `think`, prior art) are **authoring
metadata** and never serialize. The links panel groups the chunk's
links under "serializes →" / "does not serialize →" on exactly that
split; each is clickable to open in a new tab.

Handler wiring (no new API layer): select chunk → `get(<handle>)`
(chunk + DFS neighbours + meta + events + links); TOC → heading-tree
walk; document pane → the DFS render pass, web target; submit request
→ `put(kind='todo', …)`; request list → the project's open
change-request todos.

## Freeze / snapshot

**One draft per project** (the workspace's single entrypoint). A
**freeze** captures an immutable point-in-time version — both a
**release** and the project's **backup**. It is idiomatic here because
the system *already* has immutable chunks — ingested papers. So a
freeze is essentially **"ingest the draft's current state as a frozen
ref"**:

- Copy the draft's current chunks into a **new immutable ref** (a
  `paper`-like frozen kind), append-only like any ingested paper, with
  a version tag, **linked `snapshot-of`** the draft (`link` verb).
- The snapshot is then **first-class frozen corpus**: searchable,
  citable (you can cite `[§v2~…]`), permanently addressable, and
  exportable — at zero new machinery, because it *is* the paper shape.
- The **draft keeps evolving** independently; freezes accumulate
  (v1, v2, …) as sibling frozen refs.
- Snapshot chunks are a **copy = new identity**; they record the origin
  draft handle in `meta` for traceability. Being immutable and never
  reordered, they can use paper-style `ord` (no `pos`/`handle` minting
  needed), and `content_sha` is stamped at copy time.
- **Copy the derived rows too** (embeddings/summaries/keywords) — the
  text is identical, so they are valid as-is; copying avoids a
  re-embed burst per freeze.
- Optionally also **render + attach the exports** (LaTeX/PDF/Word) at
  freeze, so the release bundles both the queryable Postgres state and
  the rendered artifact.

(Alternative considered — reconstruct any past state by replaying
`chunk_events`: possible, since the log has `prev_text` + structural
events, but fiddlier. `chunk_events` is for per-chunk undo/audit;
freeze is whole-doc versioning — the copy-to-frozen-ref is the robust,
idiomatic tool.)

## Genesis, empty state & skill set

**Empty draft → first chunk.** An empty draft has no chunks, so
`key_between` has no neighbours and the first chunk has no parent. The
flow:

```
# 1 — create the draft, bound to its project. Born with a title `heading`
#     chunk, so it is NEVER empty. Mints a `draft-of` link → the project.
put(kind='draft', name='nanotrans', project='<project-todo-id>',
    title='Nanoscale Transistors',
    meta={workspace:{path:'projects/nanotrans', format:'tex'}})
#    → mints the draft ref + title heading ¶t0 + the draft-of link

# 2 — add a section heading after the title
put(kind='draft', ref='nanotrans', chunk_kind='heading',
    text='Introduction', at={after:'¶t0'})        # → mints ¶k7m2aQ

# 3 — a paragraph under it
put(kind='draft', ref='nanotrans', chunk_kind='paragraph',
    text='Nanoscale transistors …', at={into:'¶k7m2aQ', last:true})
```

**Project ↔ draft relationship.** A *project* (strategic-root todo
owning `meta.workspace`) and its *draft* are linked **1:1** by a
**`draft-of`** link (draft ref → project todo), mirroring `snapshot-of`
(the owned/derived row points at its owner). This reuses the link graph
rather than overloading `parent_id` (the todo-tree parent; ADR 0027
reserves structural moves to the `parent` relation). The **brief** stays
on the project's `meta.workspace.brief` (already cascades into the
planner prompt) and the draft reads it via the link; the draft ref
carries only the export-facing `path`/`format`. So the project is the
hub: `parent_id` → change-request todos, `draft-of` → the draft, each
change-request `targets` → a draft chunk.

The `draft-of` link is minted **atomically by the creating `put`**
(genesis step 1), in the same transaction as the draft ref — so a draft
is never project-less. `project` is **required** on create and the 1:1
is enforced there (a second `draft-of` the same project is rejected).
It is stable thereafter (removed only on a hard purge); a freeze adds a
separate `snapshot-of` edge and never touches `draft-of`.

**A draft is never empty — the empty case is designed out.** Create
mints a default **title `heading` chunk** in the same transaction (text
from `title=`, else the name), and `delete` **refuses to retire the
last live chunk** (§ Structural operations). So the awkward no-neighbour
state exists only *internally*, for that one auto-minted block (initial
midpoint `pos = key_between(⊥,⊤)`, `parent_chunk_id = NULL`); there is
no user-facing empty draft. The `at` position intent **mirrors the
`move` grammar** (DRY): `{first|last}`, `{into:'¶h'}`,
`{before|after:'¶x'}`. Genesis-from-brief: the planner, seeded
by the brief, emits this sequence (outline headings, then fill) as its
first action on a new project.

**Skill set to author/embed** (`src/precis/data/skills/precis-draft-*.md`,
served via `get(kind='skill')`). Always-on, injected, *not* in the
menu: **brief, style-guide, glossary**. Agent-selected task skills:

| skill | scope |
|---|---|
| `precis-draft-help` | overview / toolpath for drafts |
| `precis-draft-prose` | write / expand / tighten prose |
| `precis-draft-structure` | move / insert / retire / promote / demote |
| `precis-draft-math` | equations (KaTeX-subset LaTeX) |
| `precis-draft-code` | code / listing chunks |
| `precis-draft-table` | tables (cells/`tabular` + legend) |
| `precis-draft-figure` | attach / caption figures (payload-gen deferred) |
| `precis-draft-citation` | citations `[§…]`, link papers, `refs.bib` |
| `precis-draft-glossary` | terms, `[surface](¶term)`, surface-forms |
| `precis-draft-decompose` | planner: fan a broad request into per-section subtodos |
| `precis-draft-export` | render / freeze / export (LaTeX / PDF / Word) |
| `precis-draft-research` | find / triage cite-worthy sources |

**Much of this repurposes existing machinery — few skills are net-new.**
`precis-draft-structure` ← the `structural`/`deep_review` review tiers
(`workers/review.py`); `precis-draft-decompose` ← the planner;
`precis-draft-citation` ← existing `precis-citation-help`;
`precis-draft-research` ← existing research/websearch. Genuinely new
are the per-register authoring skills (`prose`/`math`/`code`/`table`/
`figure`/`glossary`), `export`, and the `draft-help` overview.

**Reviewers become an automated *source* of change-requests.** A draft
is a tree, so the existing `Reviewer` framework applies directly:
adding a draft review pass is a new `Reviewer(...)` instance (per the
existing pattern) — but instead of a memory digest it **emits anchored
change-request todos** (drift, sibling-contradiction, imbalance, gap,
overlong-section) on the draft. Those flow through the **same §5 path**
as a human note — so the human and the automated editor feed one queue;
the reviewer is a tireless annotator, not a separate mechanism.

## Open questions (gating implementation)

1. **Handler — DECIDED: dedicated handler; kind name DECIDED:
   `draft`.** A `paper` is frozen; this kind is living/editable, with
   handle grammar, DFS ordering, soft-delete, and a term registry — too
   divergent to overload `paper`. Name is `draft` — shortest *and*
   aptest: it names the living-vs-frozen distinction (the perpetual
   working copy). Chosen on meaning; the token difference between
   candidate names was ≤1/call (negligible).
2. **Handle grammar token — DECIDED: `¶`** (pilcrow), e.g. `¶5BL5xQ`, as
   the **single** bare-inline handle sigil. ASCII sigils collide with
   our own syntaxes — `#` is LaTeX's macro-param char + markdown
   headings; `@` is our pandoc citation marker (`[@slug]`); `|`/`*`
   clash with tables/emphasis; `†`/`‡` mean footnotes. `¶` is ~never
   special in prose/markdown/LaTeX (lowest collision) and means
   "paragraph," matching the chunk grain; token cost ≤1/handle. **One
   sigil only** — the `pos` fractional key is internal order metadata,
   never a handle, so it gets no sigil; identity (the handle) is the
   only thing addressed. Mitigations: parser also accepts an ASCII
   alias (e.g. `c:5BL5xQ`) for hand-typing; the bare handle string is
   **sigil-free in URLs** (`/c/<handle>`), while in prose it carries the
   `¶` inside a markdown ref (`[¶<handle>]` / `[text](¶<handle>)`).
   Visibly distinct from numeric `~N` (papers only).
3. **`ord` constraint — DECIDED: accept `ord`-as-insertion-serial.**
   `pos` (the insertable fractional sequence) is the canonical order;
   `ord` is vestigial. Draft chunks get a per-ref monotonic
   `ord` (`COALESCE(MAX(ord),-1)+1`) purely to satisfy the inherited
   `NOT NULL` + `UNIQUE(ref_id, ord)` (and the body-row CHECK), and it
   is *never read* for ordering. Rejected relaxing the shared
   constraint via migration: its only gain is cosmetic (NULL `ord`),
   at the cost of altering papers' invariants + baseline-snapshot
   churn + migration risk — against the minimal-blast principle. Lone
   detail: mint `ord` under the insert's row lock (or retry on the
   rare concurrent unique collision).
4. **Web interface** — designed in "Web interface" below.

## Future work

- **Universal terse handles.** If the base-58 chunk handle (§1) earns its
  keep here, promote it to a uniform *additive* terse-pointer handle
  across kinds — type-tagged (`c…`/`r…`/`e…`), fitted into the
  tri-identifier scheme (ADR 0006), coexisting with meaningful names
  for human-facing top-level refs rather than replacing them. Later
  ADR; prove on draft chunks first.
- **Papers adopt `content_sha` (and maybe drop `ord`).** Today papers
  leave `content_sha` NULL and keep `ord` as their order/address.
  Longer-term, papers could set `content_sha` at ingest (enabling the
  same content-addressed re-derive) and — further out — replace the
  serial `ord` with the same `pos`/`handle` scheme, retiring `ord`
  entirely. Not now (it touches the frozen-corpus invariants and the
  baseline snapshot); noted so the draft scheme is built in a way
  papers can converge onto.
- **Image & graph payloads (next phase).** This ADR handles the prose
  face (caption) + asset attachment; authoring image payloads, and
  data-supplement-driven graph/plot regeneration (data → figure render
  step), are deferred (§9).
