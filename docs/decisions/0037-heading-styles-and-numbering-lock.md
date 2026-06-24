# 0037 — Heading styles (self-contained sections) and numbering lock

- **Status**: proposed (2026-06-22)
- **Deciders**: Reto + agent
- **Extends**:
  - [ADR 0033 — Draft chunks as an editable document](./0033-draft-chunks-editable-document.md)
  - [ADR 0034 — Figure assets & permission provenance](./0034-figure-assets-and-permission-provenance.md) (shipped)
  - [ADR 0035 — Computed chunks (payload + recipe)](./0035-computed-chunks-recipes-and-the-recompute-boundary.md) (data/render recipes — the figure plot/data supplement)
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) (the address scheme — **accepted + implemented**; see "Handles" note below)
- **Handles follow ADR 0036 (final scheme, implemented).** The handle is
  **`<2-char type code><decimal id>`, computed from the row's primary
  key** — a draft chunk is `dc<chunk_id>` (e.g. `dc149`), a paper chunk
  `pc10`, a todo `td158`, a tag `tg42`. Flat, bare ASCII, **no `¶`/`§`
  sigils** (retired); recognition is the 2-char prefix + digits. In-prose
  references are `[[dc149]]` (mention) or `[label](dc149)`; a reading
  window is `dc149-B..A`, an ancestor walk `dc149^N` (0036 §4). A
  **skill** — hence a **style** — is `sk<slug>` (file-backed, body = the
  slug), which is why `meta.style` references a skill **by name**
  (`patent-claim`), not a minted code (§2). Where 0033 §1 said
  `¶<handle>`, read `dc<id>`.
- **Already live in the MCP surface.** ADR 0036 is implemented on `main`
  (`utils/handle_registry.py` `format_handle`/`parse`/`normalize`;
  resolution in `runtime.py`): the id verbs (`get`/`edit`/`delete`/`tag`/
  `link`) **accept a handle** (prefix → table → PK, no `kind=` needed),
  and **search/get output emit handles**. So the addressing this ADR
  builds on is not aspirational — the editor/agent already sees and passes
  `dc…`/`pc…` handles today; 0037 adds `meta.style` and the section model
  *on top of* that live addressing.
- **Genre + per-heading review are partly shipped too.** Main already
  has the **document-type picker** (`meta.workspace.doc_type`: research
  paper / patent / technical report / review / general article — its
  guidance leads the brief; `precis_web/routes/drafts.py`), and a
  **per-heading "review ▾"** that files anchored `structural`/`deep_review`
  todos scoped to a heading's subtree (→ `plan_tick`). So §1 (genre) and
  §3a/§3b (review/issues) already have live foundations; 0037 extends
  them (scaffold skill, section `meta.style`, the unified `issue`).
- **Reframes / supersedes**: the framing of
  [`docs/design/patent-drafting-merge.md`](../design/patent-drafting-merge.md)
  (a pre-`draft` proposal to port `patentorney-mcp` as a `patent-*`
  **kind family** behind a `.patent.yaml` side-store). That doc is
  rewritten as a thin *application* of this ADR; the kind family and the
  side-store are dropped.
- **Drafted styles**: the style catalogue (patents, research/review
  papers, animation scripts, books, shared + silent styles) is drafted
  in [`docs/design/draft-section-styles.md`](../design/draft-section-styles.md).

## Context

ADR 0033 made a draft a single chunk-native, editable kind. Three of
its decisions are the foundation this ADR builds on — they were already
taken there, and we do **not** re-open them:

- **§8 — stable-id tokens, numbers computed at render.** "Anything in
  the output that depends on position or number is a stable-id-keyed
  token (handle or slug) stored in the chunk's source text and resolved
  at render time — never stored already-resolved." Section numbers,
  cross-refs, glossary first-use all obey it. This *is* the
  handle-is-identity / number-is-render-output principle.
- **§ Reference graph — markers are canonical, links are derived.**
  Inline `[[dc…]]`/`[[pc…]]`/`[surface](dc…)` markers in the chunk text are
  the single source of truth; the first-class `link` edges are a
  **derived projection re-synced on every edit** (`_sync_draft_links`),
  never hand-maintained. In-text references are therefore *derived*,
  not authored — DRY by construction.
- **§9 — `chunk_kind` drives render dispatch + a face/payload split.**
  A chunk has a prose **face** (`text`, embedded) and an optional
  verbatim **payload** (`meta`). `term` chunks already store
  `{short, long, surface_forms}` in `meta`. Kind-aware code is confined
  to render dispatch and a one-line edit-register lookup.

0033 also composes the editor prompt (§8 "Editor prompt composition")
from an **always-on document-level identity** (brief + *one* style
guide + glossary) plus an **agent-selected skill menu**. That is the
seam this ADR widens.

**What is still missing.** 0033 supports *one* document genre (a
scientific paper) with *one* document-level style guide. We now want:

1. **Many genres** — patent, book, video script, blog — each with its
   own voice, its own export skeleton, and its own set of
   special-purpose sections.
2. **Within-genre section variation** — even a sci paper wants
   different prompts/length-rules for an *abstract* vs a *conclusion*
   vs a *methods* section.
3. **Structured, managed sections** — patent *claims*, *reference
   numerals / parts*, *figures*, *prior-art* are not free prose; they
   are managed entities with their own render (and parse, for structured
   payloads), correctness via a review pass, and — for numbers — a
   **lock** lifecycle (once drawings are finalised, a reference numeral
   can no longer renumber).

The naïve answers are both wrong. A flat enum of genre×section styles
(`sci-abstract`, `patent-claims`, `book-scene`, …) is a closed megalist
that rots. A kind family (`patent-claim`, `patent-figure`, …, the
ancient patent doc) duplicates all of `draft` and forces a side-store.
This ADR takes a third route.

## Decision

### 0. The minimal version (build this first)

The whole of v1 is one sentence: **a heading carries `meta.style` = a
skill slug; authoring that section surfaces that skill as the prompt;
the genre (`meta.workspace.doc_type`, shipped) selects a scaffold skill
that lays the sections down.** Nothing else.

That is barely more than ADR 0033 (which already injects a
document-level style guide + a skill menu): v1 just makes the style
**per-section** and lets the **root** style scaffold. With only this you
can already draft a patent — each section (`patent-claim`,
`patent-description`, …) is a **prose section with a good skill prompt**;
the LLM writes claim/figure numbers as prose; there is no engine, no
behavior code, no handoff. It works; it is just not yet *managed*.

**Everything in §3–§7 below is an expansion — build it only when the
pain is real, and each is additive:**

| Expansion | When | How it adds (no v1 change) |
|---|---|---|
| Behavior **render** (§2/§3) | a section needs managed rendering (FIG. n, numeral substitution, claim formatting) | a skill's frontmatter names a render module; absent = default prose render |
| Numbering engine + `pinned`/lock (§5) | manual renumbering hurts | a `meta` number on the entity + one render-time function |
| **Validation = a review pass** (§3a) | correctness checks (antecedent basis, name consistency) matter | a `Reviewer` instance (0033) emits anchored change-request todos — *not* per-style validator code |
| **Issues** — anchored concerns (§3b) | the agent must ask the author (elicitation) or the author flags a concern | one anchored-item flavor (`origin`/`scope`); reuses change-requests + `asking-reto` + dispatch. *Mostly exists* (change-requests); net-new is the inline answer box, next/prev "issues" mode, dismiss double-check |
| Mint-stub → review handoff (§6) | cross-section forward-refs need coordination | a stub tag + an anchored review todo (0033 §5); the stub's `needs-review` is an agent-origin issue (§3b) |

Because each expansion is a new field / module / workflow and never
mutates `meta.style`-is-a-skill, expanding causes no pain. The sections
that follow specify the *shape* each expansion should take when we reach
for it — not v1 scope.

### 1. Style on a heading — self-contained; genre (doc_type) scaffolds

A draft heading chunk (`chunk_kind='heading'`) may carry a **style** in
its `meta` (`meta.style`, a skill slug). A style **is a skill** (§2):
its prompt applies to the chunks within that heading's own section. That
is the whole axis — there is no separate "genre," "document profile," or
"style guide" concept to model: each is just **a skill set on a
heading**, differing only by *which* heading it sits on.

**Self-contained, not cascading.** Each section's style carries its
**own complete prompt** — what you see is what applies. A section's
style does **not** merge with its sibling/ancestor sections' styles. We
considered cascade (accumulate ancestor styles general→specific) and
**rejected it**: merge-precedence rules, contradiction risk, and the
doubly-defined-term / cross-section side effects the chunk model avoids.
The *one* shared layer is the **document-level brief + glossary** that
0033 already injects always-on — not a per-section merge.

**Genre is `meta.workspace.doc_type` (shipped), and it leads the brief.**
Main ships genre selection as **`meta.workspace.doc_type`** — the "+ New
draft" document-type picker (research paper / patent / technical report /
review / general article) — whose standing-guidance sentence **leads the
project brief** so the planner writes in the right register from the
first tick, and which is earmarked for the export `documentclass` switch.
This **supersedes** the earlier "genre = a style on the root heading"
framing (which also dodges the "which heading is root" ambiguity): genre
is a **document-level property on the workspace**, not a heading style.

This also settles the voice question (revising the 2026-06-22 "bake voice
into each section" note): the genre's **standing voice lives once in the
brief** (via `doc_type`, shipped — the always-on layer), **not** copied
into every section style. Section styles carry only the **section-specific
deltas** ("an abstract is ≤250 words, no citations"). No inter-section
cascade; one doc-level standing layer (brief) + per-section deltas
(styles).

**The 0037 evolution: `doc_type` selects a scaffold skill.** On top of
the shipped field, `doc_type` names a thin **scaffold skill** that lays
the standard sections down and stamps each section's `meta.style`
(genesis-from-brief, ADR 0033). So the clean split is: **`doc_type` =
genre** (standing voice + scaffold + export class, on the workspace);
**section `meta.style` = per-section behavior** (a skill, on the heading).

Style is set by an ordinary `edit`/`tag` on the heading chunk — no new
verb. The scaffolder sets section styles automatically; a human/agent
only sets one by hand for the rare off-template section.

**Silent headings.** A heading may be **silent** — no title text — when
its job is purely structural: a scene-break (`***`) between passages in a
book, a part divider, a transition in a script. It is still a real
heading chunk (it owns the subtree after it, gets a `dc…`, takes a
style), but its style is a **`separator` archetype** (§3) that renders a
divider glyph and contributes no title to the TOC. This reuses 0033's
heading machinery — a silent heading is just one whose `text` is empty
and whose style says "render me as a break, not a title." (It does not
violate 0033's never-empty-*draft* rule; the draft still has its title
heading and content — a silent heading is an interior structural node.)

### 2. One catalogue of styles; guidance↔behavior is a continuum

There is **one style catalogue**, keyed on style name (the dispatch key,
the way `kind=` dispatches to a handler). *Every* style is in it and is
therefore discoverable — listable, describable, validatable, advertisable
as a recommended child of a parent style (the picker).

**A style is, at root, a skill.** Its markdown **body** is the guidance
prose; its **frontmatter** carries whatever **structured pointers the
MCP reads** — and because the skill is MCP/LLM-facing ("nobody else
reads it"), the frontmatter can be as machine-oriented as it needs.
All fields are optional:

- `description`;
- `archetype: prose | managed | separator` (§3);
- authoring hints **as data** — length policy, etc.;
- for a **managed** section: `manages: [<chunk_kind>…]` — the leaf
  chunk_kind(s) it owns (e.g. `[claim]`, `[figure, part]`). (Numbering
  series bind to those *leaves*, not the section — §5.)
- optionally a **behavior binding**: the *name* of a code module
  providing `render[target]` (object → LaTeX / Word / web), and `parse`
  if the leaves carry a structured payload. (Validation is **not** here
  — it is a review pass, §3a.)

**Guidance and behavior are the two ends of one continuum, not disjoint
registries** — the difference is *which optional fields are filled*:

- a pure **prose voice** fills only `description` + the guidance body
  (`conclusion`, `scene`);
- a **richer guidance style** adds data pointers but no code
  (`abstract`: length policy + "no citations");
- a **behavior style** additionally names a code binding (`patent-claim`,
  `patent-image-part`, `patent-prior-art`) — which *governs how the
  chunks within its own section are rendered* (and parsed, for structured
  payloads); its own section only, not inherited by other sections (§1).
  Correctness is a review pass (§3a), not part of this binding.

So there is **no hard separation to defend.** The *only* thing that
cannot live in the skill is executable code, so a behavior style's
frontmatter merely **names** a module in `precis/draft/styles/`;
everything else — including all structured MCP pointers — rides the
skill. Discoverability is uniform (it is the skill corpus). Adding any
style is **writing a skill**; adding a *behavior* style is writing a
skill whose frontmatter names a code module you also add.

This is the answer to "do we hardwire all the styles?": **no — only the
behavior code is hardwired.** The pointers, the guidance, the picker
metadata all live in skills (runtime-editable). The behavior code is
pure `render` (plus `parse` for structured payloads) — *not* a stateful
manager and *not* a new
kind. The LLM half of "managing" a section (fleshing out a stub, §6)
rides the generic `claude_inproc` executor + the style's skill; it is
not special code.

**Picking is the exception — the genre scaffolds itself.** The primary
flow is *not* a per-section dropdown: choose the **document type**
(`doc_type=patent`, the shipped picker), type a request, and the patent
**scaffold skill primes the planner**, which **lays the standard sections
down and sets each subsection's `meta.style` automatically** — exactly
ADR 0033's "Genesis-from-brief:
the planner, seeded by the brief, emits this sequence (outline headings,
then fill) as its first action." The genre knows its own anatomy; nobody
hand-picks styles for the common case. The standard section list lives as
**prose in the genre skill** the planner follows — *not* a structured
`recommended_children` field requiring picker UI.

A manual picker exists only for the rare override (rename a section, add
a non-standard one, change a style), and for that a **plain searchable
list — `search(kind='skill', …)` — is fine**; a long box is acceptable
because it is seldom used. No scoped per-parent curation machinery is
needed (rejected as over-engineering).

**Optional second field, deferred.** If a real case needs the *same*
renderer with *different* guidance (e.g. US vs EP claims rendered
identically but drafted differently), split `meta.render_type` (code
key) from `meta.style` (skill). Until such a case appears, **one field**
(`meta.style`) carries both. (Decision: single field for v1.)

### 3. Four axes, three section-style archetypes

The rule that keeps styles from re-tangling: **four orthogonal axes**,
and one test.

- **Genre** → `meta.workspace.doc_type` (§1).
- **Identity** → a handle (`dc149`), ADR 0036 — *this specific* chunk.
- **Type** → a `chunk_kind` (a *leaf* type: `paragraph`/`heading`/
  `figure`/`term`/…) — a column, a **vocabulary slug, not a skill**, no
  handle.
- **Behavior** → `meta.style` on a **heading** = a skill (a *section*
  style).
- (plus **inline tokens** — `[[pc…]]` citation, `\citequote` — which
  point *out*; neither a style nor an ordinary leaf.)

**The test — "is X a style?":** does X have children (a heading +
subtree)? Then it's a *section* and carries a style. Is X a single leaf
you point at (a figure, a claim, a part, a character)? Then it's a
**`chunk_kind`**, never a style. (This is exactly why `citation` and
`figure` are not styles.)

**A section style has one of three archetypes:**

1. **prose** — voice/guidance only (`patent-description`, `sci-*`,
   `chapter`, `scene`). No code; default render.
2. **managed** — a section that **owns and renders leaves of a declared
   `chunk_kind`**, named in frontmatter `manages: [<chunk_kind>…]`:
   - `patent-claim` → `manages: [claim]` — claim leaves; numbered from
     order (§5); antecedent basis via review (§3a).
   - `patent-image-part` → `manages: [figure, part]` — the unified
     drawings registry: figure leaves (with blobs, 0034) + part leaves
     (term-like); two series (§5).
   - `patent-prior-art` → `manages: [reference]` — a curated
     disclosure / IDS list.
   Its code is `render` (+`parse` for structured leaves); correctness is
   a review pass (§3a); stubs resolve via the handoff (§6). **The leaves
   it manages are chunk_kinds, not styles** — the style governs *the
   section*; the leaf keeps its own identity (handle) and type
   (chunk_kind).
3. **separator** — a silent heading (§1, no title) rendering a divider
   (`scene-break`, `part-divider`). No payload/number/review; a tiny
   render. The minimal archetype.

(The earlier "self-contained entity" and "cross-corpus reference"
archetypes were the *same* thing — a **managed** section — split only by
whether its leaves are self-contained or point out. That split is a
property of the **leaf chunk_kind**, not the section style.)

**Cross-corpus leaves (the `reference` chunk_kind).** A `patent-prior-art`
section manages `reference` leaves that point OUT to a corpus chunk via
0033's inline `[[pc…]]` token. The flow is **inline on the MCP path**:
find the patent (`search`) → attempt fetch (EPO-OPS, fast enough live) →
continue, referencing the chunk; only the **slow read** (keyword +
summary) is deferred to a subtask (ADR 0007). A paper's bare `[[pc…]]`
tokens render as numbered references; the prior-art section renders an IDS
disclosure list (its own render). Bibliographic citation itself is **not
a style** — it is the inline token; the IDS is a view/export, **not** a
kind. Dangling-token policy is the fallback.

**Self-contained styles, shared mechanisms — not one style
context-switched per genre.** A patent's drawings registry section and a
sci paper's figure are **different self-contained styles**, each with its
own complete prompt and render (no inherited render-context to read).
What they *share* is **mechanisms**, not a style: the figure-asset
storage (ADR 0034), the one numbering engine (§5), the citation token
(§3.2). So there is no `render(payload, target, ancestorContext)`
switching on an inherited genre — the style itself is already specific.
Reuse lives in the shared mechanisms; the prompts and render rules stay
per-section and standalone.

### 3a. Validation is a review pass, not a per-style validator

Correctness checks are **not** `validate()` code baked into a style.
Antecedent basis on claims, name/`surface_forms` consistency on parts and
characters, "claim too broad", "section drifts from brief" — these are
**judgment** checks an LLM does better than a parser, and 0033 already
has the machinery: the **`Reviewer` framework** (a draft review pass
emits **anchored change-request todos** that flow through the existing
change-request → dispatch loop). So validation is two existing things:

1. **Prevention** — the section style's prompt (e.g. `patent-claim`
   already instructs antecedent-basis discipline). Most defects never
   occur.
2. **Detection** — a genre `Reviewer` instance reads the draft and files
   anchored change-requests for residual defects.

The only checks that stay as **code** are **structural invariants that
already fall out of other systems** — a dangling reference is the
derived-link graph (0033 §ref-graph); a numeral collision is the
numbering engine (§5) refusing it. Neither is a per-style validator. So
"behavior code" (§2) shrinks to **render** (plus `parse` only when a
section adopts a structured payload); `validate` is reviews.

### 3b. Issues — anchored concerns (the agent↔user loop + elicitation)

An **`issue`** is a first-class **anchored open concern on a draft
chunk** — the unit through which the agent and the author negotiate a
draft. It is the **elicitation mechanism**: from a thin disclosure ("a
paperclip with a ball so it doesn't poke the paper") the agent drafts
what it can and **opens issues** for what it can't infer (ball material?
diameter? welded or moulded?); the author answers; the agent
incorporates. (Distinct from `gripe`, which tracks *system/code* bugs —
an `issue` is a *draft-content* concern.)

**One concept, three origins — not three mechanisms.** What 0033 calls a
*change-request* (user → agent), the agent's *question* (agent → user),
and a §3a *review finding* (reviewer → agent) are the **same anchored
item** differing only by `origin`. So we model **one `issue`**, not a
parallel stack:

| field | values |
|---|---|
| `anchor` | the chunk `dc…` (where it was noticed) |
| `origin` | `agent` \| `user` \| `reviewer` |
| `scope` | `point` (this chunk, default) \| `area` (the enclosing subtree) \| `match` (find others like it, in a scope) |
| `needs` | `user-answer` (→ `asking-reto`) \| `agent-action` (→ doable) |
| `thread` | concern + responses + any re-ask — an append-only timeline (the gripe pattern: body + comments as chunks) |
| `state` | `open` → (`answered`) → `resolved` \| `dismissed` |

A user *opening* one ("add an issue: consider the impact on X") and the
agent *raising* one are the same act with a different `origin`; a 0033
change-request is just `origin=user`.

**Surfacing — three views, all existing affordances.**

- *Per-chunk* — the chunk's sidebar lists its open issues (0033's
  anchored-backlink panel).
- *In-viewer* — the editor's search **next/prev** gains an **"open
  issues"** mode: walk the chunks that carry an open issue, in reading
  order. (Reuse, not new nav.)
- *Background* — out of the viewer, issues live in `view='attention'`
  (the `asking-reto` subset for `needs=user-answer`) with elevated
  `PRIO` for an active draft, so they surface ahead of background asks.

**Raise → answer → spawn.** An `agent`-origin issue is parked, not
blocking — the agent opens it and keeps drafting elsewhere (the
per-chunk form of a `plan_tick` `ask-user` yield). On the author's reply
(appended to the thread, leaving `asking-reto`), `auto_check` fires and
`dispatch` mints a task carrying **the whole thread** (`dc…` +
question + answer + prior re-asks), executor = the draft's agent.

**Resolution outcomes:**

- **Resolve with edit(s)** — incorporate the answer, edit the anchored
  chunk *and/or related chunks*, close. **May chain a follow-on issue**
  capturing an unresolved remainder, linked so provenance survives (new
  issue → chunk → prior issue); a follow-on must be genuinely *narrower*
  so chains converge.
- **Re-ask / refine** — append a follow-up, back to `open`.
- **Propose-dismiss → double-check (two pairs of eyes).** Dismissal is a
  *proposal*, never a silent delete: the "✕" spawns a cheap agent check
  that **agrees and closes** or **contests and re-surfaces**. Rationale:
  resolve-with-edit and answer-a-query already keep the agent in the
  loop, so dismissal is the *only* path that would otherwise skip review.
  A `force-close` escape exists for the certain author; a contested
  dismissal that ping-pongs is capped (after N, force-surface to the
  author).

**`scope: match` — pattern issues fan out, with per-match verification.**
"Find others in this area with the same issue and fix them" resolves as
**find → verify each → fix the confident → open a sub-issue for the
doubtful → report coverage** — never a blind sweep (the bulk-edit
footgun the rejected `id='*'` sentinel warned of; this is the per-match
form of the two-eyes rule). A candidate sitting in a **locked** region
(frozen numerals, §5) or already carrying a **conflicting open issue** is
**flagged as a sub-issue, not stomped**. Each fanned edit records the
originating issue id in `chunk_events.source` (0033), giving the audit
chain "these N edits descend from issue Q" for free.

**Reuse vs. new.** Reuses the anchored-todo + `dispatch` + `auto_check` +
`asking-reto`/attention machinery, the gripe-style comment timeline, the
0033 anchor (subtree/span/frozen-set already exist), draft scoped search
(`scope=`, semantic/lexical), the planner's fan-out (a leaf task minting
children, 0033 §5), and per-edit provenance. **Net-new is small:** the
inline sidebar answer box, the next/prev "open issues" mode, the
dismiss-double-check task, and the `scope` field + verifying fan-out
resolver.

### 4. No new kind — patent/book/video are just `doc_type`s

There is **no new kind** per genre and **no `.patent.yaml` side-store** —
genre is the shipped `meta.workspace.doc_type` (which selects a scaffold
skill, §1), not a new record kind. A patent is a `draft` whose `doc_type`
is `patent`; its claims,
drawings/parts, description, and prior-art are **styled sections of that
one draft**, living in the chunk store with everything else (search,
embed, dream, export, freeze) for free. This is the central reversal of
the ancient patent doc: *claims/drawings/numerals are managed chunks,
not a kind family.*

What this **removes** vs the kind family, and what it **costs**:
distinct addressing is already covered (chunks have `dc…`; the
presentation number is computed anyway, §5); per-kind search becomes
0033's scoped search (`search(kind='draft', scope=<heading>)`);
per-kind validation becomes a **review pass** (§3a). What it
**gains**: one document tree, claims beside the spec prose, uniform
infra, no kind-registration dance, no engagement trigger, no side-store.

### 5. Numbering — one function, a per-series `pinned` set, lock

0033 §8 computes every display number at render and never stores it.
This ADR adds the **single exception** that the patent workflow forces:
a number can be **pinned** (stored), and a pinned number is authoritative.

**One global engine, per-series config, per-document assignment.** The
abstract operation — tokens → display labels by an ordering within a
scope — is one shared pure function, not a service per series:

```
assignLabels(tokensInOrder, convention, pinned) -> { token: label }
   // honour every pinned value; assign the rest by order, skipping collisions
```

- **Engine** — global, one implementation.
- **Series bind to the *entity* (chunk_kind), not the section.** A
  `figure` chunk feeds the `figures` series, a `part` chunk feeds the
  `parts` series — *because of what they are*. So **one section may hold
  entities of several kinds feeding several series**: the patent
  **drawings registry stays unified** — figure entities and part entities
  in one section, each on its own series, no split needed and no
  multi-valued `numbering` on the section. The series config (*scope*:
  document-wide / hierarchical / per-section; *ordering*: first-mention /
  explicit position / tree position) is a property of the entity kind.
- **Convention is the section style's render.** The *counter* is the
  series (on the entity); the *prefix/format* ("FIG. n" vs "Figure n",
  increment-of-10) is decided by the **section style's render**. So the
  drawings registry renders "FIG. n" while a paper figure renders
  "Figure n" — same `figures` series, different render.
- **Assignment** — per document; mostly computed, some pinned.

**`pinned` is the only dial — there are no modes, and numbering stays
fluid until the very end.** Pinned entries are stored numbers (for a
`part`, in its `meta` alongside `surface_forms`; for a `claim`, its own
number attribute). **There is no auto-pin** — through all the drafting
and reordering ("lots of wiggling"), every number is recomputed each
render. Pinning is the **last** thing we do. The behaviours are just
sizes of the pinned set:

| behaviour | `pinned` |
|---|---|
| fluid (the default, all of drafting) | empty — every number recomputed each render |
| conscious override ("set FIG dc… to 120") | one entry; engine routes others around it (no collision on 120) |
| locked / drawings-final | fully materialised — every token pinned, at the end |

**Lock = materialise.** "Lock the drawings" computes the current
numbering and writes the numerals into the parts' `meta`. There is no
separate lock flag, mode enum, or frozen-map table — **a stored number
*is* the lock** (per-part / per-entity granularity, which is more
correct: pin exactly the parts on a finalised sheet, leave new ones
fluid). Post-lock numbering is append-only with conflict-flagging: a new
part gets the next free numeral; a deleted-but-pinned part surfaces as a
dangling/lock-conflict warning via 0033's backlink bubble; reassigning a
pinned numeral is refused without an explicit redraw decision.

**Pinning is a metadata-only write — it does not re-derive.** `meta`
writes do not touch `content_sha` (0033 §4), so going fluid→locked, or
consciously setting a number, costs nothing downstream (no re-embed/
re-summarise). Only an actual prose edit re-derives.

*(Drawing the actual patent drawings — image/CAD generation — is out of
scope; 0034 covers figure-asset storage. Lock here freezes the numeral
**assignment**, which is meaningful even before any sheet is rendered:
it is the commitment that "these numbers are now final.")*

### 6. Cross-section handoff — mint a stub, review in the background

When a prose writer needs to reference a managed entity owned by a
different style (a figure, a part) it may not exist or be correct yet,
but the writer needs a **stable id now** to write the reference. This is
the **eager-id / forward-reference pattern** precis already runs three
times (request-a-missing-paper, the `citation` verifier, patent
`awaiting-fulltext`). It applies in-draft:

1. **Find or make.** The writer first `search`es the target styled
   section (dedup); if no hit, mints a **stub** managed chunk → gets a
   `dc…`. The handle is the contract.
2. **Reference by handle.** The writer drops `[[dc…]]` into the prose.
   The display number is computed/pinned later (§5), so the reference
   survives any renumber.
3. **Stub state.** The stub is tagged `needs-review`/`stub` (the review
   pass, §3a, will flag it), like patent `awaiting-fulltext`.
4. **Auto-spawned review.** `dispatch` mints a review todo/job
   **anchored to the stub** (`meta.anchor=dc…`, 0033 §5), executor =
   the style's manager agent (`claude_inproc` + the style's skill),
   **gated** (`auto_check`) so it fires *after* the writing task closes —
   the writer is never interrupted.
5. **Flesh out in place.** The manager `edit`s the same `dc…` from
   stub → real content. Because drafts are mutable in text with
   `content_sha` re-derivation (0033), **the handle is stable across the
   revision** — the writer's reference is untouched. (This is why drafts,
   not ingested chunks, are the right substrate: ingested chunks are
   DELETE+INSERT / new-handle and would dangle the reference.)
6. **Rejection bubbles.** The prose `[[dc…]]` materialises a derived
   `rel='references'` edge (0033). If the manager rejects the stub, the
   dangling-token policy / backlink bubble warns every referrer (the
   same `child-failed` shape). So a dead reference is never silent.

The child reads usage context the same way: reverse-lookup the derived
links → read the referencing chunks with a window (`dc…-B..A`). It
does **not** lexical-scan; lexical extraction is how the derive pass
*builds* the links, and the fallback if the index is stale.

This is the concrete justification for the behavior tier: the writer
stays in prose mode and never touches the entity's invariants; the
behavior style's agent owns the entity end to end; the handle is the
seam; the anchored auto-spawned subtask is the handoff.

### 7. Linguistic work at ingest, dumb substitution at export

For parts (reference numerals), the writer authors **grammatical prose
with the handle alongside the noun phrase** — `the widget [[dc149]] meshes
with the gear [[dc150]]` — not bare handles. This moves the linguistic work
to **ingest/edit time** and keeps export a pure substitution:

- A **review pass** (§3a) reconciles each mention against the part's
  `surface_forms` (0033 §9 already stores these on `term`-like chunks):
  a noun phrase that doesn't match its handle's part is a **handle/noun
  mismatch**, filed as an anchored change-request; a legitimate new
  phrasing grows the form set; an unknown one is flagged. (Not a
  per-style validator — the review framework, available early.)
- At **export**, substitution is find-replace: `the widget [[dc149]]` →
  `the widget 100` (drop the handle, insert the assigned/pinned number
  after the already-correct noun phrase). No grammar, no article
  agreement, no per-locale logic at render.

`surface_forms` thus does triple duty: mention-detection for numbering,
consistency validation at write time, and keeping export trivial. The
bare-handle alternative is rejected: it forces export to *reconstruct*
"the widget" and get grammar right in every target.

## Consequences

- **One kind (`draft`), one concept (a style = a skill on a heading),
  one number engine.** Genre is `meta.workspace.doc_type` (shipped,
  selects a scaffold skill); a section is a self-contained style. No
  cascade, no activation set, no genre kinds, no `.patent.yaml`, no
  megalist.
- **The escape hatch is small and pure.** The only per-section code is a
  registry of `render` (and `parse`) functions for the behavior styles;
  correctness is a review pass (§3a), not per-style code. The LLM half is
  the generic executor + skills. Adding a *guidance* style is writing a
  skill (no code).
- **Self-contained beats cascade.** Each section style is its own
  complete prompt — inspectable, fixable in one place, no merge
  precedence, no doubly-defined-term or cross-section side effects. The
  only cost is repeated shared phrasing (e.g. the patent voice) across
  sibling section styles — accepted (each is a skill edit, not a merge).
- **Numbering reduces to a function and a `pinned` set.** Fluid (all of
  drafting) / override / locked are sizes of `pinned`; lock = materialise
  into `meta` as the **last** step; pinning is metadata-only (no
  re-derive). This is the one sanctioned exception to 0033 §8's
  never-store-numbers rule, contained to the `pinned` set.
- **Reuses 0033/0034 wholesale.** Style = skill extends 0033's
  document-level style guide to per-section; behavior render extends
  chunk_kind render-dispatch; derived reference-links are 0033's
  already-decided projection; the mint-stub-review handoff is 0033 §5 +
  dispatch + auto_check; figure assets are 0034; prior-art fetch is the
  EPO-OPS kind + ADR 0007 derived passes. Net-new: the style-as-skill
  catalogue, the per-section behavior registry, and the per-series
  numbering config + `pinned` lock.
- **The ancient patent doc is obsolete.** `patent-drafting-merge.md` is
  rewritten as an application of this ADR; its `patent-*` kind family,
  `.patent.yaml` side-store, engagement-trigger, and `id='*'` bulk
  sentinel are dropped.
- **Risk — behavior renderers drift across targets.** Three render
  functions per behavior style (LaTeX/Word/web) can diverge. Start
  explicit (three small functions); refactor to a neutral intermediate
  representation (parse → typed block node → per-target backends) only
  when the count of behavior styles justifies it. Do not build the IR
  speculatively.

## Open questions (gating implementation)

1. **Mint-rights guardrail (probably none — KISS).** Any style is usable
   anywhere and the root scaffold skill just doesn't lay down irrelevant
   sections. Confirm we need **no** hard gate (a `book` never reaches for
   `patent-claim`); add a soft guardrail only if misuse shows up.
   Leaning: no gate.
2. **`meta.style` is a single slug.** With no cascade, each heading has
   one self-contained style; a slug is the natural shape. (An ordered
   list would only matter for composing styles on one heading — which the
   self-contained model explicitly avoids.) Leaning: single slug.
3. **Style = skill: the frontmatter schema + code-binding.** A style is
   a skill (§2): body = guidance, frontmatter = structured MCP pointers.
   Settle the frontmatter schema (`description`, length/`chunk_kind`
   hints, optional `behavior: <module>` name for render; the
   standard-section list is prose, not a field; numbering binds to the
   entity not the section, §5) and
   how a skill is recognised as a style
   (frontmatter field vs naming convention). Behavior code lives in
   `precis/draft/styles/` and is named from the frontmatter. Leaning:
   one frontmatter schema for all styles; `behavior:` names a code
   module (mirroring kind→handler) when present, absent for prose voices.
4. **`render_type` split.** Confirm single-field (`meta.style`) for v1;
   revisit only on a concrete same-render/different-guidance case (§2).
