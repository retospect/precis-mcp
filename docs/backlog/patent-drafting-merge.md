# Patent drafting — a genre over the draft model

> Status: **proposal** (rewritten 2026-06-22). Patent drafting is an
> **application of [ADR 0037](../decisions/0037-heading-styles-and-numbering-lock.md)**
> (self-contained heading styles = skills, behavior styles, numbering
> lock) over the [ADR 0033](../decisions/0033-draft-chunks-editable-document.md)
> editable-document model and [ADR 0034](../decisions/0034-figure-assets-and-permission-provenance.md)
> figure assets. **There is no `patent` document kind, no `patent-*`
> kind family, and no `.patent.yaml` side-store.**
>
> **This supersedes the earlier framing of this file** — the
> "bring `patentorney-mcp` in as a `patent-*` kind family behind an
> engagement-triggered `.patent.yaml`" proposal. That predated the
> `draft` kind; with drafts, claims/figures/numerals are just **managed
> chunks in a styled draft**, not new kinds. The git history holds the
> old version. The existing read-only `kind='patent'` (EPO OPS lookup)
> is untouched and unrelated to drafting.

## TL;DR

A patent **is a `draft`** whose **`meta.workspace.doc_type` is `patent`**
(the shipped document-type picker; ADR 0037 §1). That genre leads the
brief (shipped) and selects a scaffold skill that lays the sections down.
Each section is a **styled subtree**, not a new kind:

| Patent section | section style (archetype) | `manages` (leaf chunk_kind) | notes |
|---|---|---|---|
| Field / Background / Summary / Detailed description | `patent-description` (prose) | — | prose; patent voice from the brief (doc_type) |
| Claims | `patent-claim` (managed) | `[claim]` (v1: `paragraph`) | one claim per leaf; number computed/pinned |
| Drawings + reference numerals | `patent-image-part` (managed) | `[figure, part]` | figure leaves (0034 blobs) + part leaves (`term`); numeral in `meta` |
| Prior art / IDS | `patent-prior-art` (managed) | `[reference]` | `reference` leaves → `[[pc…]]` to `paper`/`patent`; IDS is a *view* |
| Glossary | a glossary section (prose) | `[term]` | `term` leaves (0033 §9) |

Note the split: the rows above are **section styles**; the `manages`
column names the **leaf chunk_kinds** they own. A claim/figure/part is a
*leaf* (identity = handle, type = chunk_kind), **not** a style (ADR 0037
§3).

Each style is **self-contained** (its own complete prompt; no cascade).
Everything else — search, embed, dream, export, freeze, the change-
request/review loop — comes from the draft model for free.

**v1 (ADR 0037 §0):** the simplest version is just these section
**skills as prose prompts** — the LLM writes claim/figure numbers as
prose, no engine. Managed numbering, behavior code, and the prior-art
fetch path below are **additive expansions** built when the pain is
real; none changes v1.

## Why this replaces the kind family

The old proposal created `patent-claim`, `patent-figure`,
`patent-numeral`, `patent-prior-art`, `patent-ids-submission`,
`patent-term` handlers plus a `.patent.yaml` store and an
engagement-trigger that registered them mid-session. With ADR 0033/0037
all of that collapses:

- **Claims/figures/parts are managed chunks**, addressed by `dc…`,
  in the one chunk store. No per-kind handler, no per-kind table.
- **Distinct addressing** is already covered (handles); the presentation
  number was always *computed*, not stored identity (ADR 0037 §5).
- **Per-kind search** becomes scoped search:
  `search(kind='draft', scope=<claims-heading>)` (ADR 0033).
- **Per-kind validation** becomes a **patent review pass** (a `Reviewer`,
  ADR 0037 §3a) emitting anchored change-requests — not per-style code.
- **IDS** is not a kind — it is a `view`/export over the `prior-art`
  references in the disclosures section.
- **No side-store, no engagement trigger, no `id='*'` bulk sentinel.**
  Numeral "renumber" is just the numbering engine recomputing from order
  (ADR 0037 §5); it needs no bulk verb.

## 1. The `patent` document type (`doc_type=patent`)

Selecting `doc_type=patent` (the shipped picker) does three things
(ADR 0037 §1–§4):

1. **Voice + scaffold.** Leads the brief with the patent-drafting
   guidance (formal patent prose, present tense, antecedent-basis
   discipline — *shipped*) and — via the planner's genesis-from-brief
   (ADR 0033) — **scaffolds the standard patent sections and sets each
   subsection's style automatically**
   (field, background, summary, detailed-description, claims, drawings,
   abstract, prior-art). The standard section list is prose in this
   skill; the author does not hand-pick styles for the common case
   (ADR 0037 §2).
2. **Names the self-contained styles** `patent-claim`,
   `patent-image-part`, `patent-prior-art`, `patent-description` for the
   sections it scaffolds. Each is standalone (its own prompt + render);
   shared *mechanisms* (figure-asset storage 0034, the numbering engine,
   the citation token) are reused, but the styles are not (ADR 0037 §3).
3. **Configures the numbering series** (§3 below) — an expansion, not v1.

A `book` or `sci-paper` root scaffolds different sections with their own
self-contained styles; only the underlying mechanisms are shared.

## 2. The managed sections

### 2.1 Claims (`patent-claim` — managed section, `manages: [claim]`)

- A claim is a chunk under a `patent-claim`-styled heading. Its **face**
  (`text`) is the rendered claim prose; its **payload** (`meta`, ADR
  0033 §9) is the structured form — preamble / transitional / elements,
  each element optionally referencing parts by handle.
- **Number** = computed from claim order, or pinned (ADR 0037 §5).
  Dependent claims reference an antecedent by **handle** ("the method of
  `[[dc…]]`"), rendered as "claim 1" — survives reordering.
- **Correctness** (antecedent basis, single-vs-multiple dependency rules,
  element/part cross-refs resolve) is the **claim style prompt's
  discipline** while writing, backstopped by a **patent review pass**
  (ADR 0037 §3a) that files anchored change-requests — not a `validate()`
  the writer calls.
- Authoring uses the mint-stub-then-review handoff (ADR 0037 §6) when a
  prose section forward-references a claim that isn't written yet.

### 2.2 Drawings registry (`patent-image-part` — unified, self-contained)

- A **managed section** `manages: [figure, part]` — holding two **leaf
  chunk_kinds**, `figure` and `part` (the reference numerals), because a
  part exists *because* it is labelled on a drawing. Not split (numbering
  binds to the *leaf*, not the section, so one section owning two series
  is fine — ADR 0037 §5). Its **own** self-contained style (not a shared
  figure style switched by context). Reuses the figure-asset mechanism
  per **ADR 0034** (`chunk_blobs`, `meta.figure`) for drawing assets; its
  render emits "FIG. n" + the drawings-description convention and lays out
  the reference numerals.
- `figure` leaves feed the `figures` series; `part` leaves the `parts`
  series (each via `meta`, e.g. `numerals_shown`).
- *Drawing generation (CAD/figure synthesis) is out of scope* — ADR
  0034 covers storage/attachment of an asset; here a drawing may have no
  rendered sheet yet and still drive numeral assignment.

### 2.3 Parts (the `part` leaf chunk_kind, managed by `patent-image-part`)

- A part is a **leaf** — a `term`-like chunk (ADR 0033 §9: `{short, long,
  surface_forms}` in `meta`, v1 reuses `term`) with one extra `meta`
  slot: its **numeral** (when pinned). It is **not** a kind of its own,
  **not** a style, and **not** its own numbering machinery —
  `patent-image-part` (the section style) owns the `part` leaves; the
  description merely references them by `[[dc…]]`.
- The detailed description references a part by **handle alongside the
  noun phrase** — `the widget [[dc149]] meshes with the gear [[dc150]]` —
  not a bare handle (ADR 0037 §7). This keeps export a pure
  find-replace (`the widget [[dc149]]` → `the widget 100`) and lets a
  **review pass** check the noun phrase against the part's `surface_forms`
  (ADR 0037 §3a — catches handle/noun mismatches), rather than a
  hardcoded validator.

### 2.4 Prior art / IDS (`patent-prior-art` — managed, `manages: [reference]`)

- A prior-art entry **refers to a corpus chunk directly** — a patent
  chunk (EPO-OPS `kind='patent'`) or a `paper` chunk — using 0033 §8's
  citation token (`[[pc…]]`), extended to patents. No bespoke link-or-fetch
  engine: resolution is the corpus kind's existing behavior (the patent
  kind fetches-on-`get`; a not-yet-ingested paper is the existing
  request-a-missing-paper flow). The `patent-prior-art` section renders
  its list as an IDS disclosure (its own style decides); a paper's inline
  `[[pc…]]` tokens render instead as numbered references. (The inline `[[pc…]]`
  token is 0033's citation mechanism, **not** a style; bibliographic
  citation is not a section.) 0033's dangling-token policy is the
  fallback. Reuses the `citation` kind + 0033 derived links.
- **IDS** = a `view`/export over those references in the disclosures
  section ("what was disclosed, when") — not a kind.

## 3. Numbering series (ADR 0037 §5)

The `patent` root configures these series for the one numbering engine:

| Series | Scope | Ordering | Convention |
|---|---|---|---|
| claims | document | claim list order | "claim n" / bare n |
| figures | document | first reference in reading order | "FIG. n" |
| parts (reference signs) | document | first mention in description | increment (e.g. 100, 110, …) |
| citations / prior-art | document | appearance | "[n]" + reference list |

All numbers are computed at render unless **pinned**. `pinned` is the
only dial (ADR 0037 §5): empty = fluid (the default, all of drafting —
no auto-pin); one entry = a conscious override (`set FIG dc… to 120`);
fully materialised = locked (the **last** step).

**Drawings lock.** Once drawings are finalised, **materialise** the
parts/figures numbering: write each numeral into its part's/figure's
`meta`. A stored number *is* the lock (per-entity granularity); post-lock
numbering is append-only with conflict-flagging (new part → next free
numeral; deleted-but-pinned part → dangling warning; reassigning a
pinned numeral → refused without a redraw decision). Pinning is a
metadata-only write — no re-derive (ADR 0033 §4). *(Drawing synthesis
itself is out of scope; the lock freezes the **assignment**, meaningful
even before any sheet is rendered.)*

## 4. Export

Patent export is the ADR 0033 §7 target-parameterised DFS render with
patent conventions wired into the behavior styles' `render[target]`:

- **Claims** → numbered claim set (LaTeX/Word/web).
- **Drawings description** → "FIG. n" prose; parts substituted to
  numerals from the pinned/computed map.
- **Detailed description** → part handles substituted to numerals
  (find-replace, ADR 0037 §7).
- **References / IDS** → reference list + disclosure table from the
  `prior-art` links.

No `mode='export-*'` codegen verb and no `sections/*.tex` side-files
(both were artifacts of the pre-draft proposal); export is the draft's
existing render pipeline.

## 5. What is dropped from the old proposal

- The six `patent-*` **kinds** and their handlers.
- The `.patent.yaml` **side-store** and boot/engagement registration.
- The `notifications/tools/list_changed` mid-session kind registration.
- The `id='*'` **bulk sentinel** (numbering recompute needs no bulk verb).
- The `mode='export-*'` codegen convention and `sections/*.tex` files.
- The `patentorney-mcp` **merge/retirement** plan — there is no separate
  package to fold in under this design; patent drafting is a genre of
  the existing draft surface. (If a `patentorney-mcp` still exists in any
  workspace, it is retired by reimplementation here, not by code port.)

## 6. What is reused unchanged

- Read-only `kind='patent'` (EPO OPS lookup) — the prior-art fetch
  target.
- `citation` kind + `\citequote` — prior-art references.
- ADR 0033 draft: handles, DFS order, soft-delete, change-request/review
  loop, derived reference links, export pipeline, freeze/snapshot.
- ADR 0034 figures: `chunk_blobs`, `meta.figure` origin + permission.
- The `claude_inproc` executor + skills for the behavior-style review
  agents (ADR 0037 §6).
- **Issues (ADR 0037 §3b) are the elicitation loop** — from a thin
  disclosure the agent opens anchored issues (ball material? diameter?
  welded or moulded?), the inventor answers in the sidebar, the agent
  incorporates. This fills the patent-specific elicitation gap (a one-
  line disclosure is never enough to draft from).

## 7. Sequencing

MVP first; expansions only when the pain is real (ADR 0037 §0). Each
phase leaves the gate green.

1. **v1 — `meta.style` + scaffold** (ADR 0037 §0–§1). Skill slug on
   headings; authoring a section surfaces its skill; the `patent` root
   skill scaffolds the standard sections. Draft a patent as **prose
   sections** (`patent-claim` / `patent-description` / `patent-image-part`
   / `patent-prior-art` skills); the LLM writes claim/figure numbers as
   prose. *No engine, no behavior code.* This is the whole MVP.
2. *(expansion)* **Numbering engine + `pinned`** (ADR 0037 §5) — the
   shared `assignLabels` function, per-series config, metadata-only
   pin/lock.
3. *(expansion)* **`patent-claim` + `patent-image-part` render code** —
   the patent render modules: claim formatting, "FIG. n", and numeral
   substitution at export (the entity render half of behavior; `parse`
   only if claims adopt a structured payload).
4. **Patent review pass** (`Reviewer`, ADR 0037 §3a) — antecedent-basis,
   numeral/part-name consistency, dangling references → anchored
   change-requests. Reuses 0033's review framework; **available early**,
   not gated behind the render/numbering work.
5. *(expansion)* **`patent-prior-art` fetch path** — `[[pc…]]` to patent
   chunks; inline find + fetch on the MCP path; deferred read subtask;
   IDS view. The *active* form of this — the iterative prior-art
   sweep→ingest loop, the freedom-to-operate claims view, and the
   scoping-decision ledger — is the **dynamic authoring loop** in
   [`patent-authoring-loop.md`](patent-authoring-loop.md), which builds
   on this genre.
6. *(expansion)* **Patent export conventions** — claim set, drawings
   description, references/IDS in each `render[target]`.

## 8. Open questions

Inherited from ADR 0037 §"Open questions":

1. Mint-rights guardrail — probably none (KISS); a non-patent genre
   simply never reaches for `claim`/`part`.
2. `meta.style` as a slug vs ordered list.
3. Style-as-skill frontmatter schema + behavior code-binding home.
4. Single `meta.style` vs split `render_type` — relevant here for **US
   vs EP** claim/figure conventions: confirm whether they differ only in
   render (one style, two contexts) or warrant a second field.
