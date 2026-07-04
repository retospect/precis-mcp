# Draft inline editor — click-to-edit prose, no LLM in the loop

Status: **design agreed, incremental build in progress.** This is the
canonical plan for direct (non-LLM) human editing of `draft` prose from
the web reader (`/drafts/<ident>`). The first server-side slice — the
validation core — has shipped; the editor UI is the remaining
multi-cycle work (see *Build order* / OPEN-ITEMS).

## Goal

From the draft reader, click a paragraph, edit its raw text, and have it
save when you click out — plus add/remove paragraphs directly — **without
routing through the LLM change-request path**. Validation runs on save
and *bounces you back* if you broke something serious (a dead internal
link); softer issues are surfaced but don't block.

## Why this is lighter than it looks

Drafts are the **mutable exception** to the append-only body-chunk rule.
`store.edit_text(handle, text, base_sha=…)` does an in-place
`UPDATE chunks SET text, content_sha`; the `content_sha` bump is exactly
what re-runs the embed / summarize / keyword / link cascade
(`WorkerBase._claim_fresh`). Ordering, hierarchy (`parent_chunk_id`),
handles, and `chunk_id` are all preserved. Insert (`_insert_draft_chunk`
at a fractional `pos`), soft-delete (`retire_chunk`), and reorder
(`move_chunk`) already exist, as does optimistic concurrency (`base_sha`)
and busy-row protection in the scroller (`_rowBusy`/`_dirty`). So the
backend is largely reused; the new work is the editor surface, a thin
save endpoint, and the validation gate.

## Architecture — **Model B: a box per chunk**

We evaluated two models and chose B deliberately:

* **Model A — one editor per section.** The whole section (a heading +
  its subtree) is one ProseMirror doc; caret flow / split / merge across
  blocks is native. *Rejected:* it fights the virtual scroller (must lift
  a section out and reflow scroll on exit) and it admits a **mass-orphan
  disaster** — Cmd-A + retype tears down every block-node, and reconcile
  retires N chunks (orphaning every `[pc…]` cross-ref, discarding their
  embeddings) and inserts fresh ones.
* **Model B — a box per chunk (chosen).** Each chunk is its own small
  editor. Edits are contained: the blast radius of any keystroke is **one
  box**. Cross-block refactors ("resort these blocks like X") are LLM
  tasks, not hand-editing. This (a) deletes the scary section-wide
  reconciler, (b) makes the mass-orphan structurally impossible (Cmd-A
  selects one box), and (c) fits the existing virtual scroller — an
  editable row is just a scroller row whose rendered HTML is swapped for a
  live editor, instantiated lazily on focus so only ~one editor is live.

### Keyboard navigation

The caret **passes between boxes** for smooth vertical nav (arrow-down at
a box's last line → focus the next box's top). This handoff is *wired*,
not native, and cooperates with the scroller: if the next row is an
off-window placeholder, hydrate it first, then place the caret. You
**cannot** select or drag across a box boundary (selection is per-box) —
an accepted cost, since cross-block work is an LLM task.

### Split / merge (wired gestures, mirroring the caret handoff)

* **Enter at end of a box** → spawn a new empty box below, caret into it.
* **Blank line inside a box** → split at the gap into two boxes on save;
  the first keeps the **original handle** (so `[¶handle]` cross-refs to it
  don't orphan). Only genuinely-new paragraphs get new handles.
* **Backspace at start of a box** → if empty, delete it and move caret up
  (safe); if non-empty, merge into the previous box. Merge is the **one**
  place a handle retires (refs to it could dangle), so it is guarded /
  soft-warned.
* **Structure changes only through explicit affordances.** `+` between
  boxes inserts (context-aware kind: an `item` inside a list, else a
  `paragraph`; markdown in a fresh box auto-infers kind — `* `→bullet,
  `## `→heading). **Deleting a whole subtree (a heading + its children) is
  always explicit and confirmed, naming the block count — never a
  keystroke side effect.**

### Editor surface

Each box is a small **ProseMirror** instance (schema + input rules +
decorations + history + keymap), *not* a plain textarea, so we get
markdown input rules, spellcheck, and ref decorations. The virtual
scroller (gradual load/unload) is **kept**; editors are lazily
instantiated per focused row and marked busy so windowing/poll leave them
alone until save.

## References — raw source text, live-decorated

The inline ref grammar is rich (~13 token forms; SSOT `utils/mentions.py`):
`[[dc41]]` authoring links (render to nothing), `[text](target)`,
`[¶handle]`, `[§paper~n]`, `§slug~n` sugar, `kind:id~chunk` mentions across
25 kinds, bare cite-keys, patent pubnums, and opaque `$…$` math.

**Decision: refs stay as literal source text, always** — never modeled as
distinct editor nodes. Modeling 13 lossless node types is the biggest
avoidable bug source; as text they collapse to one code path and the
reconciler never special-cases them (a box's text is one chunk's `text`
string, brackets and all). Display is a *decoration* over that text:

* **v1:** source always visible + a **live squiggle** on any ref that
  doesn't resolve (a distinct style — e.g. dotted amber — so it can't be
  confused with the browser's red-wavy spellcheck).
* **v2 polish (additive, same foundation):** *reveal-on-cursor* — render
  the ref as `§miller89` / the resolved title *unless* the caret is in its
  paragraph (active-paragraph reveal, à la Obsidian Live Preview), where
  it flips back to raw source. Pure display; touches neither the backend
  nor the reconciler. Optional `[`-autocomplete to insert refs is also v2.

**Do not** route through `prosemirror-markdown`'s stock serializer — the
spike showed it escapes our brackets (`[pc123]` → `\[pc123\]`). We use PM's
editor machinery with a **custom schema + serializer** that treats ref
tokens and `$…$` as opaque literals.

## Spellcheck

Native browser spellcheck via `spellcheck="true"` on the contenteditable —
red-wavy underlines + OS-dictionary suggestions, free, in the browser/OS
language. No lang selector in v1 (inherit OS; optionally stamp `lang`).
Details: (1) the ref squiggle must use a *distinct* style from spellcheck's
red-wavy; (2) set `spellcheck="false"` on `$…$` math and `code`/`listing`
nodes; (3) known-finicky: applying a right-click suggestion mutates the DOM
outside PM's transaction model — modern PM copes, watch-item.

## Non-prose kinds → atomic, read-only nodes

`table` (derived from `meta.table`; text-edit rejected, ADR 0035),
`figure` (image bytes in `chunk_blobs` + caption), `equation` (raw LaTeX),
`code`/`listing` (verbatim). These render as **atomic read-only nodes** in
the editor — preserved by handle, serialized back untouched, never
reflowed. The caret handoff skips them; clicking one opens its **existing**
affordance (table regen, figure upload). Creating new tables/figures from
inside the editor is v2. Prose-editable kinds: `paragraph`, `heading`,
`item`, `aside`, `box`, `callout`, `term`.

## Validation gate — hard vs soft

All existing draft validation is advisory (hints in the response body).
We add a **hard/soft split** keyed on *newly-introduced vs pre-existing*
breakage, so an edit is only bounced for damage **it** caused — never for a
dead ref that was already in the chunk (which would rug-pull an unrelated
typo fix, or block an autonomous planner minting a forward reference).

* **Hard (bounce, editor stays open):** a ref this edit introduced that
  resolves to nothing — a `[handle]` (`dc<id>`/`me<id>`/`pc<id>`…) or a
  `finding #slug` placeholder with no live finding. Strictest intent
  ("comes back at you if you broke something serious"), scoped to new
  breakage.
* **Soft (save, show a note):** pre-existing dead refs, abbrev / citation-
  form / literal-cite hints, and best-effort link-graph sync
  (`_sync_draft_links`, which never fails a write).

Blur semantics on a hard failure: **let go, badge it** — the block keeps
its unsaved text under a "invalid — click to fix" badge rather than
trapping focus.

### Shared core (shipped)

The old-vs-new diff lives in `DraftHandler._newly_dangling(new, old) →
(chunk_tokens, finding_slugs)`, built on the extracted
`_dangling_chunk_tokens` / `_dangling_finding_tokens`. It is already wired
into the **MCP/CLI edit path** as a **non-blocking ⚠** (`_dangling_edit_hint`),
so every draft text edit now warns about refs *it* broke (previously the
edit path gave no dangling feedback at all). The web editor endpoint will
call the same `_newly_dangling` and turn a non-empty result into a **hard**
422 that keeps the editor open.

## Concurrency

Per-chunk `base_sha` (already threaded through `edit_text`). If a block
changed underneath an open editor (an agent / the 4-s poll), the save is
rejected and the editor offers "this block changed elsewhere — reload?"
rather than clobbering. Undo is in-session ProseMirror per box for v1; no
cross-save structural undo (`chunk_events.prev_text` leaves the door open).

## The editor bundle — vendored, no repo build step

The reader has no bundler (inline Alpine). Precedent: `tailwind.js`
(407 KB) and `pdf.mjs` (590 KB) are pre-built assets committed straight
into `static/`. **Spike result:** a single minified ESM bundle of the PM
module set we need (`state view model transform commands keymap history
inputrules gapcursor markdown`) is **372 KB raw / 125 KB gzipped**, fully
self-contained (zero bare imports, zero `require()`), browser-loadable like
`pdf.mjs` — *smaller* than `tailwind.js` already in the tree. We run a
throwaway `esbuild` **once**, vendor the single `prosemirror.bundle.mjs`
into `static/`, and import it. Refreshing it later is a one-line documented
command, not a toolchain.

## Endpoints (planned, land with the client)

* `POST /drafts/{ident}/text` — `{handle, text, base_sha}` → `_newly_dangling`
  gate (422 on hard) → `store.edit_text` (+ blank-line auto-split, first keeps
  the handle) → `_sync_draft_links` → re-rendered row(s) + soft warnings.
* `POST /drafts/{ident}/block` — `{after_handle|into_handle, kind, text}` →
  `_insert_draft_chunk` at a fractional `pos` → new row.
* `POST /drafts/{ident}/block/{handle}/delete` — `retire_chunk` (subtree
  delete confirmed, naming the count).

## Build order

1. **Validation core (shipped).** `_newly_dangling` + extracted dangling-
   token helpers + advisory wiring into the MCP/CLI edit path + tests.
2. **Endpoints + minimal client**, browser-verified (`/verify`): text-edit
   box (source + live squiggle), `+`/delete affordances, wired caret handoff,
   busy-row protection, blank-line split-on-save. Vendored PM bundle.
3. **Polish (v2):** reveal-on-cursor ref rendering, `[`-autocomplete,
   creating structured blocks from the editor, per-draft lang selector.

## Deferred / out of scope for v1

Touch/no-hover affordances; within-section windowing for pathologically
huge sections; cross-save structural undo; changing a block's *kind* via
anything but the `+`/style affordances. Auth = the reader's existing write
trust boundary (same as `/request`, `/style`, figure upload).
