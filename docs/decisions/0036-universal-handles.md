# ADR 0036 — Universal handles: one address system for every record and chunk

- **Status**: Accepted (2026-06-23) — the **Final design** (computed,
  id-encoded) below is implemented; the design-history sections that follow
  it describe a superseded intermediate scheme, kept for the record.
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0006 — Tri-identifier scheme](./0006-tri-identifier-scheme.md)
  - [ADR 0008 — Drop slug identifier normalisation](./0008-drop-slug-identifier-normalisation.md)
  - [ADR 0027 — Reparent via `parent` link relation](./0027-reparent-via-parent-link.md)
  - [ADR 0033 — Draft chunks as an editable document](./0033-draft-chunks-editable-document.md)
  - [ADR 0005 — Forward-only migrations](./0005-greenfield-migrations.md)
- **Supersedes / partially supersedes**:
  - The identifier-scheme lineage as the *current authoritative* address form
    ([0006](./0006-tri-identifier-scheme.md) / [0008](./0008-drop-slug-identifier-normalisation.md)),
    and the `pub_id` short code ([0002 §identifier](./0002-pub-id-and-toon.md), already partly superseded).
  - [ADR 0033 §1](./0033-draft-chunks-editable-document.md) (draft chunk handle:
    6-char base-58 + `¶` sigil + `¶<handle>-B+A` window + paper-only `~N..M`).
    This ADR is the realisation of 0033's "Future work → Universal terse
    handles", but it goes **further than "additive"** — it *replaces* the
    per-kind address forms rather than coexisting with them.

## Final design — id-encoded, computed (revises the scheme below)

> The sections below record the *design history* (a random, Crockford-base32,
> stored-and-minted, then briefly sigil-marked handle). We simplified to a
> bare, id-encoded, **computed** handle — strictly less machinery. This section
> is the authoritative current design.

**Anatomy:** `<2-char type code><raw id>` — e.g. `pa5` (paper, `ref_id` 5),
`pc10` (paper chunk, `chunk_id` 10), `tg42` (tag, `tag_id` 42). Bare ASCII.

- **Computed, not stored.** The handle is a pure function of `(kind, id)`. No
  handle column, no minting, no backfill, no NOT NULL, **no data migration**.
  `resolve()` decodes; emit formats. (The `chunks.handle` / `refs.handle`
  columns added by earlier iterations become unused — drop or leave nullable.)
- **Body = the row's decimal primary key:** `ref_id` for the 21 refs-backed
  kinds, `chunk_id` for their chunks, `tag_id` for `tag` (the `tags` table,
  routed by prefix). **No Crockford** — decimal digits are already unambiguous,
  so base32 only bought compactness, not worth the encode/decode. Variable
  length, no padding; **self-delimiting** (letters = type, then digits = id).
- **Not rows, folded in by prefix:** `skill` (`sk`) and `python` (`py`) are
  *file-backed* — body is the slug/path, and `resolve()` routes `sk`/`py` to a
  file lookup. `random` is a stateless generator → **no handle**.
- **No sigil.** Recognition is the 2-char code over the known registry (a
  lookup / `\b(pa|pc|…)\d+\b` regex), confirmed by `resolve()`. A distinctive
  Unicode marker (`🄿` / boxed glyphs) was considered for at-a-glance grep but
  rejected: astral-codepoint token cost, transport fragility, untypeable, and
  poor terminal rendering. Any badge styling is a render-time concern, not the
  canonical handle.
- **`resolve()`** = 2-char prefix selects table+kind → `int(body)` PK lookup
  (or slug/path lookup for `sk`/`py`) → validate the prefix matches the row's
  kind (typo guard). Per-table PK; the prefix disambiguates → **no shared
  index**.
- **Relative grammar unchanged** (`+N`/`-N` sibling, `^N` ancestor, `lo..hi`
  span) — resolved against current structure, off the computed anchor.

**Supersedes, from the sections below:** the random body, Crockford base32, the
stored handle column, mint-on-insert, the `chunk_handles` backfill pass, and the
entire NOT-NULL / eager-mint / backfill discussion — all unnecessary once the
handle is `(kind, id)` computed. **Code impact:** rewrite the registry
(id-encode, drop the alphabet/minter), `resolve()` (decode + PK), and emit
(compute); delete the `chunk_handles` pass + mint-on-insert; the merged
stored-handle columns go unused.

## Context

precis accumulated a zoo of record/address forms, and the confusion is real
(which is which, how do I `get` it, is `miller23~4` stable):

- **slug / `cite_key`** for papers (`miller23`, `wang2020state`).
- **`pub_id`** — `base32(sha256(paper_id))[:6]` — a *second* short paper code.
- **`slug~pos`** paper chunk anchor (`miller23~4`, `~5..8`) — **positional**,
  rots on re-chunk.
- **`block.slug`** — an optional *third* per-block id.
- **`¶<handle>`** draft chunk handle (random 6-char base-58; ADR 0033) —
  stable, but its own sigil and grammar.
- **`§slug~N`** draft-prose citation mention.
- **bare numeric `ref_id`** for memories/todos/gripes/jobs/… (`42`, `todo:158`).
- **`kind:identifier~sel`** link-target grammar (`paper:wang2020~38`).

Five of these are *record addresses* doing the same job five different ways; an
LLM (and a human, and the code) has to know which kind speaks which dialect
before it can fetch anything. We want **one address system**: see a handle, know
what it is, `get(id=…)` it without passing `kind=`.

Two facts make now the moment, and make it cheap:

1. **Prod is test material.** The corpus is ingested papers (re-import targets
   for docs not yet written) plus generated/test content. We can wipe to the
   corpus and re-mint rather than running a delicate in-place migration.
2. **Storage is already numeric `ref_id` / `chunk_id` everywhere.** Links store
   resolved numeric pairs (`_link_target.py`), derived tables key on
   `chunk_id` (ADR 0033). So a handle is **purely an addressing/display layer** —
   there is no data re-shape, only a new id column + a resolver.

And the design we want already exists in one kind: **draft chunks** (ADR 0033,
`utils/handles.py`) are stable random handles, edited **in place** (`content_sha`
bumps, derived data re-derives), with hierarchy in columns (`parent_chunk_id` +
`pos`). That pattern — *stable opaque handle + structure-in-columns* — is what
we generalise to every kind.

## Decision

### 0 — The shape

> **Every addressable thing — every record and every chunk — gets exactly one
> flat, globally-unique, type-prefixed handle, minted at insert, immutable for
> life. Containment, sequence, and tree structure live in columns and links,
> never in the handle string.**

This is ADR 0033's draft-chunk machinery (`utils/handles.py`) promoted to all
kinds, with a 2-char type prefix and a transcription-safe alphabet.

### 1 — Anatomy: `[2-char type][7-char Crockford base32]`

```
dr7k9q2mx      a draft            pa4m8p1rz      a paper
dc4m8p1rz      a draft chunk      pc7k9q2mx      a paper chunk
td9q2mx4p      a todo             me1rz7k9q      a memory
└┬┘└───┬───┘
 │     └ body: 7 Crockford base32 chars
 └ type: 2-char lowercase mnemonic
```

- **Type prefix — 2 chars.** A lowercase mnemonic from a **type registry**
  (`pa` paper, `pc` paper-chunk, `dr` draft, `dc` draft-chunk, `td` todo,
  `me` memory, `gr` gripe, `ci` citation, `pt` patent, `ic` patent-chunk, …).
  26² = 676 codes — ample for "many more kinds". Adding a kind is one registry
  row.
- **Body — 7 chars Crockford base32** (`0-9 A-H J-N P-T V-Z`, no `I L O U`),
  **case-folded, lowercase-canonical**. 32⁷ = **34,359,738,368 ≈ 34.4 billion**.
  At the current ~1M chunks the mint-collision-retry rate is ~0.003%; it stays
  a rounding error at any realistic corpus size — "mint forever, never look
  back". (6 chars / ~1.07 B was also adequate to ~10 M; we took the extra char
  to stop thinking about collisions. The token cost of 9 over 8 is a *fraction*
  of one token per handle — noise.)
- **Total: 9 chars, identical length for records and chunks.** The type prefix
  is the only thing that varies.

**Why Crockford, not base-58 (ADR 0033's choice).** base-58 drops the visually
ambiguous glyphs but is **case-sensitive**, so it still corrupts on any
lowercasing path (URLs, careless transcription, case-insensitive filesystems).
Crockford does both — excludes confusables *and* folds case on decode
(`i/I→1`, `o/O→0`, `l→1`). This is **for the human-eye / case-folding paths a
handle travels** (logs, screenshots, read-aloud, OCR, copy-paste), *not* for the
LLM — to a tokenizer `O`≠`0` already. Cost: ~17% longer than base-58 at equal
entropy (one extra char) — bought for stability, which is the priority.

### 1a — Type registry

The codes live in **`src/precis/utils/handle_registry.py`** (the SSOT), guarded
by a **totality test** (`tests/test_handle_registry.py`) that asserts every
registered persistent-ref kind has a code — so a new kind without one fails CI,
not review (`news`/`message`/`cron` all slipped through manual lists). The table
below mirrors the module. A kind with addressable body chunks also gets a paired
**chunk code**. External identifiers (DOI, URL, …) are **metadata, not handles**
(§6).

Authoritative kind set = the 25 addressable records registered in
`dispatch.boot()`. The 7 **providers** (`web`, `youtube`, `wikipedia`,
`semanticscholar`, `websearch`, `perplexity-reasoning`, `perplexity-research`)
and 3 **stateless tools** (`calc`, `math`, `provenance`) mint no persistent ref
and get **no code** — they are addressed by URL / query / compute.

| group | kind | record | chunk | external id |
|---|---|---|---|---|
| **corpus / documents** | paper | `pa` | `pc` | DOI / arXiv |
| | patent | `pt` | `pk` | patent no. |
| | news | `nw` | `nc` | source URL |
| | draft | `dr` | `dc` | — |
| | conv | `co` | `cc` | discord path `discord/<g>/<c>/<t>` (messages are chunks) |
| | pres | `pr` | `ps` | — |
| | markdown | `md` | `mc` | — |
| | plaintext | `pl` | `lc` | — |
| | tex | `tx` | `xc` | — |
| | python | `py` | — | filesystem path |
| **thoughts / generated** | memory | `me` | — | — |
| | oracle | `or` | — | — |
| | finding | `fi` | `fb` | — |
| | citation | `ci` | — | — |
| | flashcard | `fc` | — | — |
| | random | `rn` | — | — |
| **operational** | todo | `td` | — | — |
| | job | `jo` | `jc` | — |
| | alert | `al` | — | — |
| | agentlog | `ag` | — | — |
| | cron | `cr` | `cp` | — |
| | message | `ms` | `mb` | discord `target` path |
| | gripe | `gr` | `gc` | — (body + comment chunks) |
| **system / meta** | skill | `sk` | — | — |
| | tag | `tg` | — | — |

The only hard rules: 2 lowercase chars, globally distinct (records ∪ chunks),
one row per addressable kind.

**Plugin-contributed codes.** A plugin's refs-backed kinds (e.g. precis-chain's
`service`/`x402`/`payment`) get first-class handles without precis-mcp knowing
the kinds: a plugin advertises a `precis.handle_codes` entry point pointing at a
module with `RECORD_CODES` / `CHUNK_CODES` dicts, mirroring the
`precis.handlers` / `precis.skills` / `precis.migrations` groups. The registry
loads these lazily and merges them into the lookup/parse maps **only** — the
built-in `KIND_CODES` (and its totality test) stay the SSOT for precis-mcp's own
kinds. A plugin code that collides with a built-in (or another plugin) is
dropped with a warning; built-ins win. Plugin kinds are assumed refs-backed
(decimal-pk) handles. This keeps precis-mcp chain-unaware while the universal
address system spans installed plugins.

### 2 — Flat, not path; distinct chunk type codes

A chunk handle is **its own flat handle** (`dc4m8p1rz`), not the document handle
with the chunk appended (`dr7k9q2 + as23`). The doc↔chunk relationship lives in
a column (`ref_id` / `parent_chunk_id`), as it already does.

Why flat over path-embedding:

- **Reparent-safe.** A path handle bakes the parent into identity, so any move
  re-identifies. The **todo tree reparents on purpose** (ADR 0027's `parent`
  link). One flat shape works for *both* physical containment (doc→chunk) and
  movable hierarchy (todo→subtodo); a path shape would force two systems.
- **No query benefit lost.** "All chunks of a draft" is already
  `WHERE ref_id = X` (indexed) — a path prefix-scan buys nothing the column
  doesn't.
- **The renderer supplies the "which doc" readability** the path would give —
  see §7.

**Consequence — record and chunk need distinct type codes** (`dr`/`dc`,
`pa`/`pc`): under flat handles you cannot tell a record from a chunk by length,
so the 2-char prefix carries it. This is *better* self-description than a path —
`dc…` says "draft-chunk" outright; resolve to learn which draft.

This means **none** of the fixed-width-slicing / `0000`-sentinel / parent-prefix
machinery is needed — records and chunks are simply independent handles.

### 3 — Resolution: one chokepoint, prefix-as-checksum

A single `resolve(handle) → (ref_id, chunk_id | None)` chokepoint, with
precedence over **enumerated sets**, never lexical/length guessing:

```
resolve(s):  handle table (exact)  →  cite_key alias  →  not-found
```

The body is **globally unique across all handles**, so the type prefix is a
**checksum, not a namespace**: `resolve` looks up by body and **asserts the
prefix matches the resolved kind**, turning a wrong prefix (`pa…` on a draft
chunk) into an *error* instead of a silent mis-describe. (A `cite_key` like
`pa2020` starts with a valid type code but is disambiguated by table lookup —
it's simply not a minted handle.)

### 4 — Relative grammar (ephemeral query sugar, never persisted)

A stable handle is the **anchor**; relative operators resolve against *current*
structure and yield another handle. They are reading/navigation sugar — **never
stored** in a link or anchor (the stored form is always the bare handle).

```
dc4m8p1rz          identity
dc4m8p1rz+1 / -1   sibling step      — next / prev sibling under the same parent
dc4m8p1rz+3        three siblings forward
dc4m8p1rz-2..3     signed sibling span (anchor = 0, inclusive)  → 2 before … 3 after
dc4m8p1rz^         parent / enclosing heading
dc4m8p1rz^2        two levels up
```

- **`+N` / `-N` = sibling step** (same `parent_chunk_id`, ordered by `pos`).
  On a heading this is "next/prev **heading** at that level"; on a flat **paper**
  (a one-level tree, all blocks siblings) it is "next/prev block" — one rule,
  no special-casing.
- **`lo..hi` = signed sibling span** — both endpoints are signed offsets from
  the anchor; the range crosses zero so anchor-inclusion falls out. This single
  construct subsumes the old "window" operator (`+1..3` = pure-forward,
  `-3..-1` = pure-back, `-2..3` = centred). `..` present ⇒ range; absent ⇒
  single step.
- **`^N` = ancestor walk** — the tree analog of `±N`. Generalises to any nested
  kind (drafts *and* todos). Clamps at the doc/root.
- **Aliases (optional ergonomics):** `++`≡`+1`, `--`≡`-1`, `^^`≡`^2`. Canonical
  is the signed-int form — it does multi-step *and* dodges the `--` SQL
  line-comment hazard; `++`/`--` are accepted on input only.
- **No chaining, one trailing operator.** Compose by resolving and re-addressing.
- **Deferred:** descending to a child / next-non-sibling. `±N` (siblings) +
  `^N` (ancestors) cover the heading-skeleton walk; add a child op later if a
  concrete need appears.

base32 excludes `+ - . ~`, so every operator parses cleanly off the end of the
bare handle — which is **why the handle body carries no internal separators**
(§7).

### 5 — Lifecycle: mint, immutable, merge, re-ingest

- **Mint-on-insert, immutable, never reused.** Every addressable row gets its
  handle at creation; it never changes and a retired handle is never re-minted.
- **Merge (`A + B = C`).** Dedup/merge collapses two refs into one: **union C's
  links from A and B**, redirect `A→C` and `B→C` (tombstones), dedupe identical
  `(src, rel, dst)` triples. Merge is an **LLM operation**, so it is provisional
  and **reviewed after** — contradictory inherited links (A `supersedes X`, B
  `superseded-by X`) are **flagged, not auto-resolved**.
- **Re-ingest is *not* merge.** Papers are **ingest-once**, so chunk handles are
  as stable as drafts' in practice. On the rare re-chunk (marker/OCR upgrade),
  boundaries move and there is no 1:1 remap: **tombstone + redirect** the old
  chunk handles, and for **durable cross-paper citations anchor to a quote**
  (Web-Annotation style), resolved to the current chunk handle — which is
  exactly the legacy-import path (Migration §3). Merge-rule ≠ rechunk-rule.

### 6 — Address vs metadata: `cite_key` / DOI are not the handle

A handle is an **internal pointer**; it says nothing about the paper's
real-world identity. The two are orthogonal and have different fates:

- **As an *address*** (`get(id='miller23')`) — retired; the handle replaces it.
- **As *metadata*** — **kept unconditionally.** DOI (and author/year/title/
  venue) is how precis touches the outside world: BibTeX `\cite` keys +
  bibliography (`latex.py`, `\citequote`), dedup, PDF re-fetch, the `/papers`
  "verify on doi.org" link. Ingesting into handles does **not** capture these,
  and the handle cannot stand in for them — `pa4m8p1rz` is useless in a
  bibliography.

The `cite_key` *string* specifically is *derivable* from author+year, so the
column is technically droppable (re-derive at export); we keep it as a cheap
stored field unless export is made to re-derive. **Retire `cite_key`/DOI as a
resolvable address; keep DOI + bibliographic metadata as data.**

### 7 — Rendering faces (deferred, documented)

One identity, three faces — the canonical form is bare; the readable
decoration is the renderer's job (deferred to a later phase, so interim output
emits the **bare handle + the gloss the handlers already print** next to it):

```
dc4m8p1rz                      canonical — storage, get(id=…), tool I/O, resolve()
[[dc4m8p1rz]]                  in-prose mention (wiki-link form; nothing in precis collides)
[Miller · Methods](dc4m8p1rz)  rendered for humans — label = decoration, handle = target
```

- **No internal separators in the canonical handle.** `-`/`.`/`~` are relative-
  grammar operators (§4); putting them in the body (`dc4.m8p.1rz-1`) is
  ambiguous, costs tokens, and breaks copy/double-click-select. Human visual
  grouping (phone-number style) is a **render-only** concern using a non-input
  glyph (middle-dot / thin space / CSS), never part of the stored handle —
  deferred with the rest of the renderer. (The LLM never needed grouping;
  `4`≠`m` to a tokenizer already.)

### 8 — The retire set

| Form | Example | Verdict |
|---|---|---|
| `pub_id` (base32 sha) | `ab12c3` | **Retire** — already a short paper code; the handle generalises it. |
| paper chunk anchor `slug~pos` | `miller23~4`, `~5..8` | **Retire** — positional, unstable. → `pc…`. |
| `block.slug` | — | **Retire** — a third, half-used chunk id. |
| `¶`/`§` addressing sigils | `¶5BL5xQ`, `§slug~N` | **Retire the sigils**; the base-58 handle is *absorbed* into `dc…` (Crockford). |
| agent-facing bare numerics | `158`, `todo:158` | **Retire** (agent surface) — `ref_id` stays the internal PK; agents see `td…`. |
| verbose link-target grammar | `paper:wang2020~38` | **Simplify** — accept a bare handle; keep `kind:slug` only for import. |
| `cite_key` slug | `miller23` | **Keep as metadata + import alias** (§6); retire as a resolvable address after import. |
| DOI / arXiv | `10.1234/…` | **Keep** — external truth, metadata, never a handle. |

**Orthogonal — not identifiers, so untouched:** virtual view paths (`/open`,
`id='toc'`), relation names (`cites`, `parent`, `touched`), tag axes
(`STATUS:`, `PRIO:`, `project:`), cluster-cell coordinates (`4.7.1`). Folding
these into the handle system would be a category error.

## Migration — wipe to the corpus, mint, re-import, drop the old forms

Safe to do near-hard-cutover because prod is test data, papers are ingest-once,
and the rule is **accept-old-on-input, emit-new-on-output**.

```
0. INFRA (prerequisite for everything)
   - Schema: handle column on refs + chunks (global UNIQUE, case-folded), type registry.
   - Crockford base32 minter (2 + 7 = 9, lowercase-canonical) — swap the
     alphabet + folded uniqueness check in utils/handles.py.
   - resolve(): handle table → cite_key alias → not-found; prefix-as-checksum.
   - Forward migration (ADR 0005): BACKFILL handles onto every surviving
     paper/patent + their chunks. Column write only — no re-ingest, no embedding
     cascade. Regenerate the baseline snapshot at release per ADR 0031.

1. WIPE everything generated/operational
   - DELETE by an explicit KEEP-LIST (safer than a delete-list):
     keep papers, patents, plaintext, markdown, tex + their chunks; delete
     all else (todos, jobs, memories, dreams, agentlogs, alerts, drafts(+chunks),
     citations, gripes, conv, …).
   - No orphan cleanup — nothing survives to dangle (the JSONB string surfaces,
     citation.source_handle and todo.meta.anchor, are deleted, not migrated).
   - Consequence: this RESETS the scheduler — watches/recurring/projects are
     gone and must be re-created.

2. CUT OVER emit
   - Skills + UI emit bare new handles only (+ the existing adjacent gloss/title).
   - Delete ~pos / ¶ / § parse + emit code — zero referrers remain.
   - resolve() keeps ONLY the cite_key alias, for the import window.

3. IMPORT legacy tex papers (separate importer, in progress)
   - cite_key + verbatim quote → quote-match → chunk handle; fuzzy match
     (dehyphenation / OCR drift) + flag-for-review queue on no-unique-hit.
   - The same quote-matcher is the re-chunk citation bridge (§5).

4. SOAK + verify
   - Run until logs show zero old-form resolution except cite_key import.

5. RETIRE legacy
   - Drop cite_key from resolve()'s alias path; drop pub_id entirely.
   - KEEP cite_key + DOI as metadata columns (export/bibliography/display).
```

## Consequences

- **One address system.** See a handle → know its kind from the 2-char prefix →
  `get(id=…)` with no `kind=`. `kind=` becomes optional for the five addressing
  verbs (get/edit/delete/tag/link); still required for `put`/`search` (which
  name a *class*, not a record).
- **Lighter than it looks.** It generalises existing draft machinery
  (`utils/handles.py`, in-place edit, structure-in-columns) rather than building
  new; storage stays numeric, so no data re-shape.
- **Stable by construction.** Handles are identity, not position; the
  `miller23~4` rot class is gone. Edit-stability holds wherever edits are
  in-place (drafts); ingest-once makes it hold for papers too.
- **Readability moves to the renderer.** Until that phase lands, raw output is
  opaque-handle + gloss — a mild, temporary regression (accepted).
- **The wipe resets operational state.** Watches/recurring/projects are
  re-created after; acceptable on test data, and the cost of a clean slate.

## Cutover policy & guardrail (running note)

> **Legacy address forms (`slug~pos`, bare `slug`, `#ref_id`, `kind:slug~pos`)
> must not appear in any bot-facing output.** Handles are the only address the
> bot should *see* emitted; the legacy forms remain valid on **input**
> (resolution) only.

- **Emit assumes the handle is present.** Emitters write the handle directly
  (`block.handle` / `ref.handle`); the `handle or <legacy>` form keeps the
  legacy branch only as a transition fallback for not-yet-backfilled rows. Once
  the `chunk_handles` / ref backfill passes have run, the legacy branch is dead
  in prod output. After backfill is guaranteed corpus-wide, the fallback can be
  deleted outright (and emitters become `block.handle` with no `or`).
- **Proposed guard — auto-gripe legacy leaks (transition, time-boxed).** A
  response post-processor scans bot-facing output for a legacy-address regex
  (`\b[a-z][a-z0-9-]{2,}~\d+\b`) and raises a deduped `gripe` when one slips
  through — so a missed emitter is caught automatically instead of by manual
  audit. Caveat: help text / examples legitimately contain `~N` / `~0`, so the
  scan must exclude documented example tokens (or run env-gated + log-only at
  first) to avoid false positives. Run it "for a bit" during the cutover, then
  retire. This is the enforcement mechanism behind the policy above.

### Rollout status by kind (running)

- **Done:** records (`pa…`/`me…`/`td…`/… — slug-kinds via `pa123`-style;
  numeric kinds by `<code><ref_id>`), paper chunks (`pc<chunk_id>`, emit +
  chunk-READ relative nav), handle-emitting search output.
- **Done — the draft slice (addressing).** Draft chunks (and therefore
  **figures**, `chunk_kind='figure'` — ADR 0034/0035) emit and accept
  `dc<chunk_id>` end-to-end: the `DraftChunk.handle` property computes it,
  `get`/`edit`/`delete`/`search`-scope take it, the runtime routes `dc<id>`
  (+ relative ops) straight to the draft handler (drafts keep the bare handle
  rather than the paper `slug~ord` rewrite), and all the agent-facing **hints**
  — handler `next=`/render, the planner-prompt draft + anchor blocks, the
  `precis-draft-help` skill, and the web reader/anchors — now show `dc<id>`.
  Change-request / review / figure anchors are stored bare; readers
  (`_requests_by_handle`, the planner anchor block) stay `¶`-tolerant so
  legacy anchors keep resolving.
- **In-prose references unified onto handles.** A `[[<handle>]]` (or
  `[label](<handle>)`) is the one cross-reference form — *a handle is a ref to
  something* — resolved through the single `store.resolve_handle` decoder for
  any kind (`[[dc41]]` a chunk, `[[me5]]` a memory, `[[pc10]]` a paper chunk).
  `draft_markup` (autolinker), `linkify` (reader render), and the dangling-ref
  hint all speak it; the skill/planner teach it. The one non-handle exception
  is the paper **citation** `[§<cite_key>~<n>]`, kept because the bibliography
  is keyed on the cite_key.
- **Residual (intentional transition):** `_insert_draft_chunk` still writes a
  base-58 `chunks.handle` (now vestigial — `DraftChunk.dc` computes the handle),
  and the legacy `¶` address + `[¶<handle>]` prose form still **resolve** on
  input. Dropping the base-58 write + `¶` resolution is a later cleanup once no
  legacy anchors/prose remain.

## Open questions

1. **Paper-chunk stability across re-chunk** — mostly moot (ingest-once); the
   residual is handled by tombstone+redirect + quote-anchored citations (§5). No
   further mechanism unless re-chunk becomes common.
2. **`cite_key` column — keep or re-derive at export?** Kept by default (cheap,
   `latex.py` reads it); drop only if export is made to re-derive (§6).
3. **Renderer** — the three-face rendering (§7), incl. `[[…]]` mention linkify
   and human-grouped display, is a deferred phase.
4. **Type registry seed** — the proposed codes are tabled in §1a; finalise at
   implementation (a few chunk codes, e.g. `python`, are still TBD).

## Future work

- **Child / cross-level navigation operators** (§4) — add a descend op if the
  heading-skeleton `±N`/`^N` pair proves insufficient.
- **Render-time handle grouping** and the `[label](handle)` / `[[handle]]`
  linkify pass (§7).
- **Papers converge fully onto the scheme** — ADR 0033's "papers adopt
  `content_sha` / drop `ord`" future-work item composes with this: once papers
  carry handles, `ord` is purely an internal serial.
