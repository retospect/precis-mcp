---
status: shipped
title: Interactive reaction-pathway explorer — clickable energy diagram + per-state atomic cell + measures
model: opus
---

> **Shipped 2026-08-06** (`fix/reaction-pathway-explorer` merged). Kept —
> not deleted — while `pathway-frame-capture.md` (draft) still delegates
> its motion follow-on to this spec, and the `meta.measures` writer
> residual in `OPEN-ITEMS.md` cites it. Delete once both resolve.

# Interactive reaction-pathway explorer — clickable energy diagram + per-state atomic cell + measures

> Extends the catalyst/autocatpath pipeline (`docs/design/autocatpath-integration.md`,
> `docs/design/catalyst-discovery-quest.md`) and the `structure` kind (ADR 0043)
> onto a `pathway` ref's web view. Siblings: `catalyst-physical-realism.md`
> (improves *what* is screened) and `bundle-pathway-in-tree-plugin.md` (relocates
> the pathway glue — see Target + blast radius). First customer: pathway `175949`
> (`cand-86c7531724`), the 0.41 eV Pd(111) NO→NH₃ candidate quest `164903`
> graduated 2026-07-30.
>
> **Scope note (split 2026-08-06).** This proposal is deliberately scoped to
> what ships on **existing data, precis_web-only**: the clickable diagram, the
> per-state 3D cell viewer, and per-state measures. The *motion* — animating the
> atomic cell over each barrier, and a single measure that follows the same atom
> across states — depends on capturing NEB/relax frames and establishing
> cross-state atom identity, which is its own cross-repo deliverable:
> **`pathway-frame-capture.md`**. The "Motion follow-on" section below is
> `blocked-by` that proposal.

## Motivation / why

A `pathway` ref today is a table of numbers — states, relative energies,
per-step barriers, a span. The web view (`/refs/pathway/{id}`,
`routes/refs.py:_pathway_detail`) renders the graph as text/TOON/Mermaid and
links the per-state `structure` refs, but you cannot *see the mechanism*. You
can't click the diagram, you can't look at the Pd slab with NO adsorbed at a
chosen state, and you can't read off a bond length. Candidate `175949` — a
genuine result worth looking at — is exactly the case where "here's a number"
should be "here's the reaction, look at it." Everything this proposal needs is
**already in the DB**; it's a presentation gap, not a data gap.

## Current state (what exists vs. what's missing)

Established by investigation of catpath + precis_web + prod:

- **Graph** (nodes+energies, edges+barriers+uncertainty) → `refs.meta.graph`. ✅
- **Per-state keyframe geometries** (16 for 175949) → Postgres `struct_atoms`
  (fractional `fa/fb/fc`, one row/atom), linked from the pathway via
  `meta.structure_refs`. ✅ **renderable today.**
- **3D cell viewer** → 3Dmol.js already vendored on `structure/detail.html.j2:274`
  (clickable atoms/bonds, input/relaxed/overlay, clip-plane, "what moved"). ✅ reuse.
- **Measures** → "Eyes & Measures" markers on `structure`
  (`handlers/structure.py:_render_markers`): a *measure* live-computes a
  distance/angle between named atoms with an ok/warn/error verdict + hover-glow.
  ✅ reuse — scoped per-*structure*.
- **MISSING (this proposal):** the energy diagram is not rendered interactively
  (only the raw NetworkX graph is stored — template notes it "omitted — large");
  the per-state structures aren't wired into a viewer on the pathway page;
  measures aren't surfaced at the pathway level.
- **MISSING (out of scope → `pathway-frame-capture.md`):** the NEB image band
  and relax trajectories are discarded (`neb.py` returns `list[float]`; `relax.py`
  has no trajectory logger), and there is **no cross-state atom identity** —
  `catpath/precis/ingest.py::scene_from_ase` labels atoms by a *per-state-local*
  element counter, so "the same physical atom across states" does not exist yet.

## In scope

1. **Interactive energy diagram.** Inline SVG rendered client-side from
   `meta.graph`, mimicking catpath `viz.draw_profile`'s grammar (level lines,
   transition-state humps, Ea labels, ±1σ bands). Each node/edge is a clickable
   target. **precis owns layout; catpath exports no pixel anchors** — the stable
   graph node id is the only anchor, and must match the `meta.structure_refs`
   keys. Layout (x = topological order, y = rel energy, path grouping) is
   recomputed in JS — a small, accepted duplication of `draw_profile`'s logic
   (catpath owns data, precis owns presentation).
2. **Per-state 3D cell viewer.** Embed the existing 3Dmol viewer on the pathway
   page; clicking a state node loads that state's `structure` ref (via the
   structure viewer's existing `struct_atoms` data source, exposed as a small
   JSON payload/endpoint) into the viewer. A stage list + prev/next stepper
   walks the states.
3. **Per-state measures.** Surface Eyes & Measures at the pathway level: a
   measure defined on the pathway (`{name, op, atoms:[label,…]}`, stored on
   `refs.meta.measures` — see decision below) is evaluated against **each
   state's own structure independently** and shown next to that state (in the
   viewer panel and, where a value exists for every state, as a trace on the
   diagram). Reuse means calling **`evaluate_measure(scene, m)` directly**
   (`src/precis/structure/measures.py`) on an ad-hoc `Measure` built from the
   pathway meta — the per-structure `struct_measures` table / ops persistence
   path is **not** used here. Two identity-safety rules (see decision below):
   a new **`min_distance` op** (`{op:'min_distance', atoms:[label],
   element:'Pd'}` = labeled atom → nearest atom of an element) expresses
   "N to nearest metal" without naming any one slab atom; and a plain
   `distance`/`angle` whose anchor's element **repeats** within a state is
   evaluated but rendered flagged **"label-order identity — unverified across
   states"** (dashed trace + warning), never as a trusted physical trace.
   (A measure that *follows the same atom* across states is the Motion
   follow-on, below.)

## Motion follow-on (blocked-by `pathway-frame-capture.md`)

Not built here; recorded so the split is legible. Once `pathway-frame-capture`
lands NEB/relax frames and a cross-state atom identity (propagated along the
band chain: state ↔ band-endpoint ↔ state), two things unlock and become a
slice of *this* explorer: (a) **play/scrub animation** of the atomic cell over
each barrier's captured band (labeled morph fallback for barrierless supply
edges), and (b) a **single tracked measure that follows one physical atom across
all states**, drawn as a continuous curve on the diagram. Both are `blocked-by`
the capture proposal and are out of scope until it ships.

## Explicitly NOT in scope

- **Frame capture / animation / cross-state identity** — the entire motion
  story is `pathway-frame-capture.md`. This proposal touches no catpath code and
  ships no `struct_frames` write path.
- **New DFT/physics, a general MD viewer, structure editing, real-time sim.**
- **Catpath's standalone matplotlib PNG export** — precis renders its own
  interactive diagram from `meta.graph`; the `viz.py` PNG path is untouched.
- **Backfilling** — works against whatever pathways already have
  `meta.structure_refs` populated (175949 does).

## Acceptance criteria

1. `/refs/pathway/175949` renders an **interactive energy diagram** from
   `meta.graph` (levels, TS humps, Ea labels, uncertainty bands); every state
   node is clickable.
2. Clicking a state node (or stepping prev/next) loads **that state's atomic
   cell** into an embedded 3D viewer reading the existing `struct_atoms` data;
   the 16 states of 175949 are all reachable.
3. A **per-state measure** defined on the pathway is evaluated against each
   state's structure and shown per state; where every state yields a value it
   is plotted as a trace on the diagram. The worked example is identity-safe by
   construction: `min_distance` from the (singleton) N to the nearest Pd
   traces across all 16 states of 175949. A `distance`/`angle` anchored on a
   repeated-element atom renders **flagged unverified** (dashed + warning),
   never as a trusted trace — verified by a test asserting the flag appears
   for a Pd-anchored measure and not for the N-anchored one. No catpath
   change and no cross-state identity is required for this to pass.
4. The page degrades cleanly for a pathway lacking `meta.structure_refs` (diagram
   still renders from `meta.graph`; viewer shows "no geometry linked").

## Target + blast radius

- **precis_web:** `routes/refs.py:_pathway_detail`,
  `templates/refs/pathway_detail.html.j2`, new inline-SVG diagram JS, reused
  3Dmol assets (`structure/detail.html.j2`), a small structure-atoms JSON
  source for the embedded viewer.
- **precis core (one small extension):** `src/precis/structure/measures.py` —
  the new `min_distance` op (labeled atom → nearest atom of an element) in the
  evaluator; existing `distance`/`angle` untouched.
- **DB:** none new — pathway measures live on `refs.meta.measures` (JSONB;
  decision below). No migration.
- **Docs:** `state-map.md` (pathway web surface), the `precis-pathway-help` skill.
- **Sibling `bundle-pathway-in-tree-plugin.md`:** that proposal moves the
  pathway *glue* into `src/precis_pathway/` but leaves the `pathway` kind's
  public shape and web route unchanged — so this proposal, being precis_web-only
  and reading `meta`/`structure_refs`, is **unaffected by bundling order**. No
  `blocked-by` needed.

## Open questions / decisions log

- **Pathway-measure storage — DECIDED.** Measures live on `refs.meta.measures`
  as a JSONB list (`[{name, op, atoms:[label,…]}]`), evaluated live against the
  linked per-state structures at render time — no new table, no migration. This
  mirrors how the pathway already carries `structure_refs`/`graph` on `meta`.
  (Resolves the `/ready` advisory that this was left as an unresolved "or".)
- **Diagram layout ownership — DECIDED.** Recompute `draw_profile`'s layout in
  JS; catpath emits no layout hint. Data in catpath, presentation in precis.
- **Atom-identity scope — RESOLVED BY SPLIT.** Cross-state identity is no longer
  claimed here. Per-state measures (in scope) need none; the same-atom-across-
  states measure moved to the Motion follow-on, `blocked-by`
  `pathway-frame-capture.md`. (Resolves the `/ready` blocker that the risk was
  scoped-away when it wasn't.)

- **Identity-drift guard for per-state measures — DECIDED** (resolves the
  2026-08-06 `/ready` blocker: `scene_from_ase` labels are a per-element
  counter over each state's own ASE order — stable for a singleton element,
  not guaranteed for a repeated one, so a `distance` anchored on one Pd of a
  12+-Pd slab could silently plot different physical atoms across states).
  Two-part fix, now in In-scope item 3 + AC 3: (a) a new `min_distance` op
  (labeled atom → nearest atom of a named element) expresses "N to nearest
  metal" identity-free — the chemically right quantity anyway; (b) a
  `distance`/`angle` whose anchor's element repeats within a state is
  evaluated but rendered flagged "label-order identity — unverified across
  states", never as a trusted trace, with a test asserting the flag. True
  same-atom tracking stays in the Motion follow-on.
- **Evaluator reuse layer — DECIDED** (resolves the 2026-08-06 `/ready`
  advisory that "reuses the evaluator as-is" was ambiguous): pathway measures
  call `evaluate_measure(scene, m)` directly on ad-hoc `Measure` objects
  parsed from `refs.meta.measures`; the per-structure `struct_measures`
  table, `anchor_atom_id` FK, and `structure_save` versioning are **not**
  touched at the pathway level.

*`/ready` pass (2026-08-06, post-split): 4 pre-split blockers verified cleared
against code + prod 175949; this pass's 1 blocker + 1 advisory resolved by the
two decisions above.*
