---
status: draft
title: The fisheye rail — a right-sized, machine-proposed context you ratify instead of build
---

# The fisheye rail — a right-sized, machine-proposed context you ratify instead of build

## Thesis

A writing human's scarce resource is **attention**, not tokens. They have a few
minutes before the next interruption and no discipline to assemble context by
hand. The LLM is the opposite: patient, disciplined, and it has *already mined
the space* for what context a task needs (every plan-tick snapshots its working
set to `job.meta`, ADR 0051 §15).

So flip the labor. **The machine constructs context; the human only approves
it.** You never face a blank rail — you arrive at a proposed, right-sized set
drawn from structure (`around here`), the AI's own mined snapshots, and
salience, and you nudge it. Your one real input is *how much attention you
have*; the machine fits the context to that budget.

Everything below — the rail, the three marker lanes, the sidebar edit — is in
service of one sentence: **a fleeting-attention human gets to an engaging,
right-sized, editable context in one or two gestures.**

## Motivation / why

We shipped the hand-driven working set (main `162d56e9`,
`precis_web/draft_eyes.py`): pen/eye markers on paragraphs, `around here` ring
promotion, a submit tray. It works, but it asks the human to *build* context —
place eyes, expand rings, curate. That is exactly the discipline a short,
interrupted session does not have. The bottom tray + the per-block "around
here…" change box also fragment the surface: two request paths, a right column
that competes with the document for width.

The insight that reframes it (from the design conversation): the human's view
of the document and the LLM's context are **the same fidelity map** — the
extent ladder (`hidden → title → summary → verbatim`) we already built. How
collapsed a branch is *for you* is how much of it the AI *carries*. Navigating
is curating. Once that is true, the whole surface can collapse to **one editing
document + one nav rail**, and the rail can do the context assembly *for* you.

## The model: one fidelity map, two viewers

Each node in the chunk tree has a **fidelity** on the extent ladder
(`working_set.Extent`: `TOC/kwd < SUMMARY < FULL/verbatim < FIDELITY/fisheye`).
That single per-node value drives, simultaneously:

- **Your view** — how collapsed the branch is (title only / one-line summary /
  full text).
- **The AI's context** — how much it carries, snapshotted **at ask time only**
  (locked decision; casual navigation does not live-rewrite the AI's context).

Progressive disclosure keeps navigation cheap: **hover peeks the full paragraph
without committing it** to either view. You read anything without paying for it
in context.

Two provenances over that map (already in `working_set.py`):

- **inferred / transient** — the machine's proposals (auto-fisheye around your
  focus, salience candidates, search hits). Dim; they *fade* at the next crunch
  if you ignore them.
- **requested / normal|pinned** — what you *ratified*. `adopt()` promotes an
  inferred eye to requested. These survive navigation.

The human's whole job is moving things from the first lane to the second, with
a glance.

## The rail (left) — a nav lens, not a second surface

**One editing surface** (the document). The left rail is a **lens** over it, not
an editable duplicate:

- A **clean TOC of summaries** — rows are the *summary text*, not heading names,
  so you navigate by meaning. Collapsible for density, **independently of the
  document's expanded state** (locked decision — the rail is pure nav; the
  document's expansion is what feeds the ask).
- **Scroll-synced** — the doc scroll highlights the rail row; clicking a rail
  row scrolls + expands the doc. Orientation is free.
- It hosts the **three marker lanes** and the **search candidates** — all one
  interaction grammar at different opacities: live outline · your marks · dim
  adoptable proposals.

The document itself is the fisheye: mostly collapsed to summaries, expanded
where you are, hover to peek, click to walk in. The bottom tray and the
right-column change box both **dissolve into the rail**.

## Three marker lanes (the entire noun budget)

| Glyph | Lane | Lifetime | Reaches the LLM |
|-------|------|----------|-----------------|
| 👁 **eye** | context — "hold this" | sticky-with-TTL (`PRECIS_DRAFT_EYES_TTL_HOURS`) | yes (grounding) |
| 🖊 **pen** | edit target — "change this" | transient, for the next ask | yes (as `edit_hint`) |
| 📌 **post-it** | note — "remember / revisit" | **persistent** (editorial annotation layer) | **only when promoted** |

Post-its are the previously-deferred **annotation layer** ("editorial, does not
export"): a durable note pinned to a chunk (*"contradicts §3 — revisit"*). By
default it is *yours*; you promote it into an ask ("address my notes here") when
you want the AI to act on it (locked decision).

The lanes are the whole vocabulary. See **Non-goals** for the discipline that
keeps it that way.

## The size lever — the one knob you touch

Your constraint is time-before-interruption, so make **size** the primary
control: a three-stop lever (or slider) —

- **glance** — titles + the hottest few summaries; a screenful.
- **working** — the current section verbatim + its ring at summary; a normal
  edit context.
- **deep** — the fisheye+1hop everywhere it matters.

You pick the *budget*; the machine fills it **optimally by salience** — expand
what matters, collapse the rest to hit the target. You never choose *which*
nodes, only *how much*. This is "proper-sized, quickly" as a single gesture, and
it is the antidote to hand-curation.

## The propose → approve loop (never a blank rail)

On opening a section, the rail is **already populated** — you approve, you don't
assemble:

1. **Structure** — `around here` on where you are (neighborhood + reference
   ring, promoted to candidate eyes).
2. **The AI's mined snapshots** — the eyes recent plan-ticks used on *this*
   material (see below), ranked by salience.
3. **Salience fill** — trimmed/expanded to the current size-lever budget.

All of it lands **dim (inferred/transient)**. You **adopt** the good ones with a
glance (`adopt()` → requested); the rest fade. Engagement comes from arriving at
something alive, not building it.

## Mining the AI's snapshots (inherit its context)

Every plan-tick already stores its working set on `job.meta` (§15). That corpus
is a set of *proven* answers to "what context does this task need." We do not
build a manager for it — we **expose it read-only and make it actionable**:

- Clicking a task / plan node / recent tick → **adopt what it saw** as your
  working set. You mine its discovery instead of redoing it.
- Over many ticks the snapshots reveal implicit *recipes* ("a methods-section
  edit typically eyes X/Y/Z"). These surface as proposals in step 2 of the loop
  — the corpus is the memory, so there is nothing to name or save.

This is the only place "stored context sets" appears, and it is **inspection,
not management** (see Non-goals).

## Nav = triage, not scrolling

The rail leads with **what needs you** — sections with open post-its, a failed
tick, pending pens, hot recent activity. `n`/`p` step through them. Navigation
becomes a byproduct of triage: you engage the live edge, never hunt a cold
outline. Combined with summaries-as-labels and scroll-sync, "easy nav" reduces
to *navigate by meaning and by your own marks*.

## The sidebar AI-edit box (contextual, dumb on purpose)

The AI-edit box lives at the **bottom of the rail**, right under the pens and
eyes, so the panel reads top-to-bottom: *targets (🖊) · context (👁) ·
instruction*. The box is deliberately just an instruction field — **the marks
are the scope** (you pointed already; you don't re-describe). The rail can hold
the whole exchange (sending → the tick's "✋ needs you" question → the result),
which a cursor popup cannot.

Clean split, no either/or:

- **Inline, in place** = *direct human edits* (the existing ProseMirror
  `draftEdit`) — the fast path for "fix this word."
- **Sidebar** = *AI edits* (instruction → working-set-scoped tick).

## Search = candidate eyes, not a results list

Adding a pin that is not near your cursor is a search — but the results **are
not a separate list**; they render as **dim candidate eyes** in the rail and
*adopt on click* (the provenance model, verbatim). Two things make it better
than a papers-style list:

- **Context-seeded** — the box defaults to "related to what I'm working on"
  (seeded from the pens / current section), so the rail *proposes* candidates
  before you type. The human twin of the dream self-search / `+recall` rung.
- **Land on the chunk** — a paper hit expands to its **keyword-cluster map**
  (the doc-eye shipped in `162d56e9`), so you pin the right passage (`pc<id>`),
  not the whole paper.

The search widget is **reusable** — a self-contained component
(`refSearch({ kinds, placeholder, onPick })`) backed by the cross-kind search
primitive (`search_chunks_across_kinds`); the `[` citation picker, link pickers,
and "add source" flows all adopt it later.

## Locked decisions

- **Coupling: ask-time only.** Navigation never live-rewrites the AI's context;
  the expanded state is snapshotted when you ask, and eyes/pens ride through as
  the stable pins.
- **Fisheye: manual for now.** No cursor-following auto-collapse in v1;
  add a "follow me" toggle later once the mapping feels right.
- **One editing surface** + a nav-lens rail.
- **Rail collapse independent** of the document's expanded state.
- **Sidebar** hosts the AI edit; inline editor stays for direct typing.
- **Post-its** yours by default, **promote** to reach the LLM.
- **Search results = candidate (transient) eyes**, adopt-on-click.

## Explicitly NOT in scope (the "few nouns" rule)

The noun budget is **three marker lanes + one live working set.** Every future
marker must kill one first. In particular, **not** building:

- **Named / saved context sets.** A set regenerates from structure (`around
  here` on the section rebuilds it in a click) — storing a floating name is the
  Emacs-register-you-forget. If reuse-pressure ever proves itself, hang the set
  on an artifact that already exists (a **project**, a **plan node**, a
  **post-it**) — the artifact is the name and it does not rot.
- **A context ring buffer / kill-ring.** The AI's per-tick snapshots already
  *are* the history; expose it read-only, never a thing to cycle or curate.
- **Auto-fisheye that live-edits the AI's context** (deferred to a toggle;
  ask-time snapshot only).
- Cross-kind post-its / a general annotation surface beyond the draft reader
  (draft-scoped first).

## What's already built (substrate this reuses)

- `working_set.py` — the `Extent` ladder, `Persistence`
  (transient/normal/pinned), `Provenance` (inferred/requested), `adopt()`, decay
  ladder + bunched eviction. The propose/approve loop *is* this model.
- `precis_web/draft_eyes.py` — the sticky-with-TTL set, pen/eye toggles, `around
  here` ring promotion, `to_working_set_meta`.
- `utils/working_set_render.py` + `utils/eye_render.py` + `utils/fisheye.py` —
  the deduped multi-eye render; the size lever is a policy over these.
- `utils/toc_db.cluster_blocks` + the doc-eye keyword-cluster map — search
  "land on the chunk."
- `job.meta` §15 per-tick snapshots — the mineable corpus.
- The draft reader's virtual scroller — already does mostly-collapsed 10k-block
  documents; summary-collapse is *lighter*.

## Slice plan

Each slice ships dark-ish and green on its own; the rail replaces the tray
incrementally.

1. **Rich rail rows.** Pens *and* eyes in one list; row = summary snippet (not
   the handle); hover → `/preview/chunk` popover; click → scroll-to-`dc`.
   `_marks_view` gains `preview` + base-58 handle. (Smallest; pure enrichment of
   the shipped tray.)
2. **The rail proper.** Promote the tray to a persistent left rail: clean TOC of
   summaries, collapsible, scroll-synced. Retire the right-column "around
   here…" box (needs-you panel → Col B; `＋figure` → Col A hover controls);
   collapse the grid to 2 columns.
3. **Post-its.** The persistent annotation lane — per-chunk note storage
   (editorial, non-exporting), rail badge + inline margin marker, promote-to-ask.
4. **The size lever + propose-on-arrival.** Glance/working/deep as a salience
   policy over the render; never-blank rail seeded from `around here` + salience.
   Sidebar AI-edit box (folds the tray's submit + the needs-you exchange).
5. **Search-as-candidate-eyes + snapshot mining.** The reusable `refSearch`
   widget → dim candidate eyes (adopt-on-click), context-seeded, land-on-chunk;
   "adopt what a tick saw" from the §15 snapshots (read-only, actionable).

## Acceptance criteria

- The rail is a persistent left panel showing a scroll-synced, collapsible TOC
  of **summaries**; the bottom tray and the right-column change box are gone.
- A row hover previews the full paragraph; a click scrolls the document to it.
- Opening a section shows a **non-empty** proposed context (dim candidates) with
  zero clicks; adopting one is a single click.
- The **size lever** changes the rendered/asked context volume without the human
  choosing individual nodes.
- Eyes / pens / post-its are all placeable from the rail; post-its persist
  across reloads and do **not** appear in exports.
- Search results render as adoptable candidate eyes; a paper hit can pin a
  specific `pc<id>` via its cluster map.
- "Adopt what this tick saw" reconstructs a working set from a `job.meta`
  snapshot.
- No named-set / ring-buffer surface exists (the non-goal holds).

## Target + blast radius

- **Routes** — `precis_web/routes/drafts.py` (`/marks`, `/around`,
  `/request-ws`; new `/preview` reuse, a `/search/quick`, a `/postit`, a
  snapshot-adopt endpoint); retire the `/request` box (keep the endpoint
  dormant).
- **Templates** — `drafts/detail.html.j2` (the rail + size lever + sidebar
  edit), `drafts/_row.html.j2` (gutter → rail markers; `＋figure` rehome).
- **Web module** — `precis_web/draft_eyes.py` (size policy, post-it storage,
  snapshot-adopt); a new reusable `refSearch` component + `/search/quick`.
- **Core** — `utils/working_set_render.py` (size-lever policy); `draft_eyes` /
  `_marks_view` (preview enrichment). No migration if post-its live on chunk
  meta; a small table if they need their own lifecycle (decide in slice 3).
- **Planner** — unchanged (`_m_fisheye` already consumes `meta.working_set`).

## Open questions / decisions log

- **Post-it storage** — chunk `meta` (no migration, simplest) vs a dedicated
  `annotations` table (own lifecycle, cross-kind future). *Lean:* chunk meta for
  the draft-scoped v1; revisit if post-its go cross-kind.
- **Salience signal for the size lever / propose loop** — reading-order distance
  + reference-ring membership + recent-tick-touch is the cheap v1. A learned
  salience from the snapshot corpus is a later refinement.
- **Auto-fisheye** — deferred to a toggle; revisit after the manual mapping is
  validated (does collapse-as-fidelity feel right in practice?).
- **Snapshot-adopt granularity** — adopt a whole tick's eyes, or let the human
  cherry-pick rows from the "what it saw" view? *Lean:* whole-set adopt first,
  cherry-pick if needed.
