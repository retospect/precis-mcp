# Design — the catalyst-discovery quest

> A perpetual striving ("a Pd catalyst with the lowest achievable
> rate-limiting barrier for reaction R") realised as an autonomous quest
> loop that **designs catalyst models, measures their barriers, and
> Pareto-ranks the designs** — with a light hand: the big model
> orchestrates via tools, the compute is the referee.
>
> Status: **design of record.** No code yet. Grounded against the live
> build (quest layer, `structure` kind, `catpath.precis` bridge) as read
> 2026-07-17. Companion specs it composes: `docs/design/catpath-integration.md`,
> `docs/design/catpath-pathway-tool-surface.md`, `docs/proposals/quest-layer.md`.

## 1. The idea in one paragraph

A **quest** is the perpetual, unachievable striving (the medieval sense —
you strive, never finish). Beneath it, the achievable unit of work is a
**catalyst design = a precis `structure`** (atoms in a cell). Each tick,
the big model edits a structure (substitute an atom, add an adatom, change
the facet, add water), then two compute jobs score it: an ML **relax** for
stability/formation-energy, and **catpath** for the reaction's
rate-limiting **barrier**. Both results harvest into the quest's WORM
logbook and onto the structure's measures. A **generalised Pareto frontier**
ranks the designs on arbitrary named objectives (barrier, stability, …).
A design that crosses a declared barrier ceiling **graduates** to
`needs-experiment`. The whole thing runs on the existing quest frame
(logbook · dossier · frontier · allocator · cascade), dark until enabled.

## 2. The boundary (who owns what)

The load-bearing architectural decision: **the `structure` owns all
geometry; catpath is a barrier oracle over a *given* structure.** This
supersedes the earlier "catpath builds its own fcc(111) slab from a label"
model and the "candidate = pathway" workaround.

| Layer | Owner | Status today |
|---|---|---|
| Slab · alloy · adatom · facet · water (**the model**) | precis `structure` | edits exist; **build/adsorbate ops missing** (§7.4) |
| Starting slabs (seed the model) | catalysis-library pull / slab-builder op | not built (§7.6) |
| Reaction topology (intermediates, steps) | catpath | built (state library) |
| Adsorbate placement on an *arbitrary* slab | LLM anchors the active site (`eye`) → `place_fragments`; best-site search optional | v1 folds into Slice 2; search = Slice 5 (§7.5) |
| NEB · barrier · uncertainty graph | catpath | **built** (`neb.py`, MACE backend) |
| Persist a run + compare pathways (TOON) | `catpath.precis` bridge | **built, dark** (§7.1) |
| Rank designs by barrier + stability | the generalised quest frontier | **built** (Slice 1) — ranks arbitrary named measures; barrier arrives on the candidate's own meta (§7.2) |
| Strive · log · pace · escalate | quest frame | **live** (dark until enabled) |

**The candidate is the `structure`.** Its frontier measures = its own
relax (formation-energy / max-force stability) **+** its linked pathway's
`rate_Ea` (barrier). This unifies the design unit, the memory (structures
are content-addressed → the `serves`-graph *is* the record of explored
space, deduped), and the objective, in one object.

## 3. What is already real (do not rebuild)

Verified by reading the live build + the catpath repo (`/Users/reto/work/projects/code/catpath`):

- **The quest frame** — `kind='quest'` with logbook (WORM, typed entries),
  dossier (a `draft` the quest owns, `dossier-of`), gaps/health, the
  research **tick** (`quest/tick.py`), the **cascade** (cheap↔frontier
  escalation, `quest/cascade.py`, signals `first-review` / `new-evidence≥5`
  / `stalled≥4`), compute dispatch + auto-harvest (`quest/compute.py`,
  `struct_relax` jobs), **single-stage graduation** (`quest/graduate.py`),
  and the **allocator** (`quest/run`, EWMA bandit + weekly budget,
  `quest/allocator.py`). Dark unless `PRECIS_QUEST_LOOP_ENABLED`;
  `precis quest tick <id> --compute --force` steps it by hand.
- **The `structure` kind** — atomistic IR (ADR 0043): cell + atoms + bonds;
  edit ops (`structure/ops.py`: `set_cell`, `add_atom`, `set_element`,
  `vacancy`, `displace`, bonds, `constrain`, `measure`); read probes
  (`structure/probe.py`: `find(element=…, undercoordinated=True)`, `plane`,
  `neighborhood`, `coordination`, `path`); a relax ladder
  (`clean` local geometry-repair · `ml` MACE/CHGNet · rented `dft-*`
  dispatched as `struct_relax`).
- **The `catpath.precis` bridge — built and tested, but DARK.** In the
  catpath repo as `catpath.precis`: a `pathway` kind handler (views
  `analysis · compare · intermediates · steps · profile · network · mermaid
  · methods · config`), a **complete** `catpath_explore` job_type (routes to
  a pinned GPU node via `PRECIS_CATPATH_ROUTE_NODE`, else in-process EMT),
  a content-addressed regen cache, and native structure ingest (relaxed
  intermediates → `structure` refs). Slices 0 + 1a built + verified, and
  **live on prod** — 4 `pathway` refs exist on `precis_prod` (verified
  2026-07-17). It is absent from *this dev worktree's* kind list only
  because the catpath plugin isn't in the local venv — do **not** read that
  as "not deployed".
- **The `compare` TOON leaderboard — LOCKED/built.** One row per candidate,
  reaction coordinate as columns, `‡` cells = step barrier Eₐ, always-present
  `RATE` (max single-step Eₐ) + `SPAN` (whole-path apparent barrier), rows
  sorted best-first (`catpath/precis/toon_views.py`). Plus
  `search(kind='pathway')` as a cross-candidate leaderboard and
  `view='analysis'` for selectivity. TOON = `precis.format.toon.dump`.

## 4. What is NOT real (the honest scope)

- ~~catpath has **no structure-input path today**~~ **RESOLVED (Slice 2,
  catpath-side built).** `Network.prebuilt_slab` + `Network.slab()` score an
  injected slab instead of `build_slab`; `_build_net` stamps it from a runtime
  `cfg._prebuilt_slab` side-channel (one chokepoint → all `net.slab()` sites,
  and it never leaks into `to_dict`/`content_key`); `run_pathway(...,
  slab_extxyz=…)` hydrates the wire form. A round-tripped slab that lost ASE's
  `adsorbate_info` gets it transplanted from the cfg reference so named-site
  placement still resolves (clean-fcc(111) first cut). *Still precis-side
  (Slice 3):* the `catpath_explore` job resolving a `structure_ref` → extxyz →
  `run_pathway`. (Structures already flow *out* via ingest; this is the *in*.)
- catpath's default envelope is **fcc(111) single-metal, gas-phase vacuum**.
  Alloy/dopant/adatom/facet all become **`structure` edits on the injected
  slab** (§2); solvent/pH/potential/coverage/temperature are still absent.
- catpath does **not search for the best adsorption site** — sites are
  hand-declared + *rattled*. But two hooks already exist: `poses()`
  (`structures.py:135`, an ensemble over sites×tilts — the unwired best-site
  finder) and `place_fragments()` (`structures.py:115`, explicit
  `{site,dx,dy,height}` placement — "put it roughly here, let relax settle
  it"). §7.5.
- ~~The quest frontier ranks only `{energy, max_force, max_disp, n_steps}`~~
  **RESOLVED (Slice 1, built):** `_candidate_from_structure` now gathers
  arbitrary named measures (all numeric run fields + numeric `structure.meta`
  keys), so a quest ranks on `{barrier, formation_e, …}` via
  `meta.rubric_objectives`. The *producer* is now BUILT too (Slice 3):
  `harvest_measures` lifts a completed `catpath_explore` job's `barrier`/`span`
  onto the candidate's meta (idempotent, `meta.quest_catpath_harvested_upto`) +
  links the evaluating pathway. The catpath bridge *emit-side* is BUILT +
  verified too (the `catpath_explore` job accepts `slab_extxyz` + `structure_ref`
  and emits scalar `barrier`/`span`/`pathway_ref` onto its meta). The precis
  `dispatch_catpath` (export the candidate's extxyz, mint the pathway write-back
  ref, pin the `catpath_explore` job on the candidate) is now BUILT + verified
  round-trip too — what remains is minting the quest + the tick tools-loop that
  sequences relax→catpath.
- The quest **tick is single-shot** (one LLM call, `tick.py:391`), not an
  agentic tool loop.
- precis `structure` can *edit* atoms but cannot *build* a slab or add an
  adsorbate/molecule (ops marked "next increment").
- `precis-dft` is **stale/deprecated** — not a path. DFT is deferred; the
  ML barrier (catpath) is the objective.

## 5. The loop (end-to-end, once built)

```
strive (quest: "lowest R barrier on a Pd catalyst")
  └─ tick (agentic tools-loop; cheap by default, opus on cascade signal)
       reads: statement · dossier · gaps · momentum · logbook tail · frontier
       proposes 1–N MOVES, each an edit to a parent `structure`:
         set_element (alloy), add_atom (adatom), slab/facet, +H2O (later)
       for each proposed design (a new content-addressed `structure` serving the quest):
         ├─ relax(ml)   → struct_relax job → formation-E / stability
         └─ catpath(structure) → catpath_explore job → rate_Ea + graph
       harvest (automatic, later tick):
         each job result → a `result` logbook entry (logs exactly what happened)
         barrier + energy → measures on the candidate structure
       rewrite dossier (living synthesis + a rendered frontier snapshot)
  frontier: Pareto-rank designs on meta.rubric_objectives
            = [{barrier,min}, {formation_e,min}, …]  (arbitrary params)
  graduation: barrier < ceiling ⇒ tag needs-experiment, milestone deed, ★ gap
  allocator: EWMA bandit × weekly budget paces which quest ticks when
```

**A design sits `unevaluated` on the frontier until its barrier returns** —
correct behaviour, not a bug: a catalyst isn't ranked until it's measured.
Content-addressing makes re-proposing a design a cache hit; `dead-end` /
`ruled-out:` entries stop re-treading. The `serves`-graph of structures +
their linked pathways is the durable memory of explored space.

## 6. Slice plan

Ordered so the **first working loop (Slice 3) does NOT need the optional
best-site search** (Slice 5) — placement uses the `eye` anchor (§7.5). Each
slice is independently shippable.

| # | Slice | Repo | Gist |
|---|---|---|---|
| 0 | **Bridge already live — verify, don't build** | ops | catpath is **deployed + live on prod** (4 `pathway` refs, verified 2026-07-17; `can_own_jobs` 8.22 + routed MACE jobs already ran). Only re-confirm `PRECIS_CATPATH_ROUTE_NODE` → the current GPU node and that the deployed bridge matches current precis. Effectively done. |
| 1 | **Generalise the frontier** | precis-mcp | **DONE** — `_candidate_from_structure` ingests *arbitrary* named measures (run fields + numeric `structure.meta`) + `params` passthrough (§7.2); **by-total leaderboard** `view='leaderboard'` (TOON, §7.3). `TestGeneralizedFrontier` + `TestLeaderboard` green. **by-intermediate view deferred to Slice 3** (needs the candidate↔pathway link + catpath's graph→profile — same DRY block as source-3). |
| 2 | **catpath structure-input seam + anchor placement** | catpath | **catpath-side DONE** — `Network.prebuilt_slab` + `run_pathway(slab_extxyz=…)` score an injected slab instead of `fcc111`-from-label; `adsorbate_info` transplanted for clean fcc(111). `test_network.py` (3) + `test_precis_runner_slab.py` (3) green. **Pending (Slice 3):** the `catpath_explore` job resolving a precis `structure_ref` → extxyz; the `eye` active-site anchor for edited slabs (§7.5). §7.1. |
| 3 | **The quest, first light** | precis-mcp (config) + tick + catpath bridge | **Harvest barrier-lift BUILT** (`TestCatpathHarvest`). **catpath emit-side BUILT + VERIFIED** — `catpath_explore` accepts `slab_extxyz` + `structure_ref` and emits scalar `barrier`/`span`/`pathway_ref` onto the job meta (the harvest contract); end-to-end checked in the dev container (injected Pd slab → EMT → `barrier` returned). **Dispatch BUILT** — `dispatch_catpath` exports the candidate's extxyz, mints the `pathway` write-back ref, and pins a `catpath_explore` job **on the candidate** (so the harvest's `parent_id` query finds it); `TestDispatchCatpath` (5, incl. a dispatch→harvest round-trip) green in the catpath-enabled dev container. **Co-dispatch BUILT** — `run_compute_step` reads the quest's `meta.reaction_config` and, when set, co-dispatches catpath alongside the relax for each new candidate (independent lanes — catpath relaxes the injected slab internally, so no cross-tick sequencing needed for first light); `TestReactionCoDispatch` (3) green. **Remaining (precis-side):** mint the NO→NH₃/Pd quest (R decided, §8) — set `meta.reaction_config` + `rubric_objectives=[{barrier,min},{formation_e,min}]` + a graduation ceiling; the agentic **tick tools-loop** (§7.7, the strategic escalation where the big model drives instead of the deterministic co-dispatch). Auto-loop stays dark. |
| 4 | **Structure model-building ops** | precis-mcp | slab-builder op (Miller facet, size, vacuum), adsorbate/molecule add — so the big model can build & edit beyond clean slabs. Unlocks adatom / facet moves. §7.4. |
| 5 | **Best-site search (optional rigor)** | precis + catpath | Wire catpath's `poses()` ensemble; precis narrows candidates via probes; catpath relaxes each + keeps lowest-energy. Upgrade over the v1 anchor, not a blocker. §7.5. |
| 6 | **Catalysis-library pull** | precis-mcp | MP / OC20 / curated slab library → `structure` refs as seed designs + reference anchors. §7.6. |
| 7 | **Optimizer advisor (Optuna)** | precis-mcp | `suggest_next(quest)` — reconstruct a multi-objective Optuna study from candidate history, suggest the next design point. Advisor, not driver. **Rider: stamp `meta.params` from Slice 1/3** so history accrues now. §7.8. |

**Deferred axes (named, not scheduled):** explicit **solvation** (water as
atoms-in-the-cell — bigger, noisier NEB; implicit solvation is a model
catpath lacks); a **DFT confirmation rung** + multi-stage graduation
(ML→DFT→experiment); an **embedding proposer** for candidate generation.

## 7. Component specs

### 7.1 catpath structure-input seam (Slice 2, catpath repo)

The slab was built only in `build_slab()` (`structures.py:48`, `fcc111(...)`);
every placement function already takes a generic `slab: Atoms`, so the seam is
an **input adapter, not a rewrite**. **BUILT (catpath repo):**
`Network.prebuilt_slab` + `Network.slab()` return the injected slab (a copy;
`adsorbate_info` transplanted from the cfg reference when a round-trip dropped
it); `_build_net` stamps it from a runtime `cfg._prebuilt_slab` side-channel so
one chokepoint reaches all `net.slab()` call sites without leaking into
`to_dict`/`content_key`; `run_pathway(..., slab_extxyz=…)` hydrates the extxyz
wire form. What remains (Slice 3, precis-side):

- `catpath_explore` gains a param `structure=<precis structure handle>` (or
  the config YAML carries a `structure_ref`). The bridge hydrates it to an
  ASE `Atoms` → extxyz and calls `run_pathway(slab_extxyz=…)`. (The
  catpath-side entry already accepts the injected slab.)
- The network builder skips slab construction and places the reaction's
  declared adsorbates on the supplied slab. **First cut is scoped to clean
  fcc(111) slabs** so the existing site library (`fcc`/`hcp`/`top`) still
  resolves; on edited/arbitrary slabs, adsorbates place at the structure's
  `eye` active-site anchor via `place_fragments` (§7.5) — no best-site
  *search* yet. The bare slab is relaxed once; bottom layers stay fixed per
  the input structure's `constrain`.
- Backend stays MACE (FAIRChem/UMA is better for adsorbates — a per-quest
  backend choice, deferred). Output unchanged: the pathway graph with
  per-edge `barrier` / `delta_e` / `low_confidence`, persisted via the
  existing `persist_result`.

Everything downstream (persist, ingest, compare TOON, regen cache) is
untouched — this is an input adapter, not a rewrite.

### 7.2 Generalise the frontier to arbitrary objectives (Slice 1, precis-mcp)

The ranking machinery is **already generic** and needs no change:
`_dominates` and `pareto_split` iterate `for key, sense in objectives`
(`quest/frontier.py:51-101`); `_objectives_for` already reads arbitrary
`meta.rubric_objectives = [{"key","sense"}, …]` (`:104-116`);
`Candidate.measures` is an open `dict[str,float]` (`:39`). The **only**
hardwired spot is the measure-supply function `_candidate_from_structure`
(`:119-141`), which copies just the four `struct_runs` columns at `:131`.

**BUILT (Slice 1).** `_candidate_from_structure` now gathers measures from
two sources into `Candidate.measures`:

1. **all numeric fields** of the most-recent converged `struct_runs` row
   (generic dict iteration, not the fixed four — auto-adopts any future run
   scalar; today that's still `energy/max_force/max_disp/n_steps`);
2. **numeric top-level keys of `structure.meta`** — the escape hatch a
   synthesis/harvest pass stamps computed measures onto. **Fill-only**: a
   stamped measure never clobbers a real relax measure of the same name.

Plus a `params` field on `Candidate` (from `meta.params`) that rides along
for the later optimizer advisor (§7.8) — never a ranking measure.

**Why not a third "lift from the linked pathway" source (a spec change).**
The original design lifted `rate_Ea → barrier` by traversing a
structure↔pathway link. Reading the code killed that: (a) catpath stores no
scalar `rate_Ea`/`span` — they are *computed on demand* from `meta["graph"]`
by `catpath.precis.analysis`, which lives in the **catpath** venv (absent
from precis-mcp), so a frontier-side lift would either import catpath or
**re-derive the barrier from the graph — a DRY violation**; and (b) the
candidate→evaluation-pathway link doesn't exist yet (today's `related-to`
links a pathway to its *own* intermediate structures, not a candidate to its
evaluation). So **source-3 collapses into source-2**: the harvest step
(Slice 3) lifts the pathway's barrier onto the *candidate's own* `meta` once,
and the frontier reads a plain scalar — no catpath import, no graph recompute.
The pathway link stays as **evidence** for the by-intermediate view (§7.3),
not as a second measure-lift path. (Rider for Slice 2: catpath's
`pathway_meta()` should also stamp scalar `rate_Ea`/`span`/`low_confidence`
at persist time, so the harvest reads a scalar rather than recomputing.)

No other change: once `measures` carries `{"barrier": …, "formation_e": …}`,
`meta.rubric_objectives` + `pareto_split` rank on exactly those keys (already
generic). A candidate missing any declared objective stays `unevaluated`
(already the behaviour). Kept the `s.kind == "structure"` server filter
(`:154`) — the candidate is the structure; the pathway is a linked
evaluation, not itself a candidate. Tests:
`tests/test_quest_compute.py::TestGeneralizedFrontier`.

**Quest config shape:**

```
meta.rubric_objectives = [{"key": "barrier", "sense": "min"},
                          {"key": "formation_e", "sense": "min"}]
meta.graduation        = {"key": "barrier", "sense": "min", "threshold": 0.75}  # eV
```

`graduation` stays single-stage (§4: DFT deferred) — crossing the ceiling
tags `needs-experiment` + logs a `milestone` deed + surfaces a ★ gap.

### 7.3 Leaderboard views — by-total and by-intermediate (Slice 1)

Two TOON views over the same frontier/graph data, primarily for LLM
legibility (they don't change ranking — they render it):

- **by-total — BUILT (`view='leaderboard'`).** One row per design; columns =
  identity + the objective vector + a `frontier|dominated|awaiting` band + a
  `★` graduation flag; sorted best-first per band by the primary objective.
  A pure `frontier.leaderboard(fr) → (rows, schema)` helper builds it; the
  handler renders via `precis.format.toon.dump`. This is the design
  leaderboard. Sits alongside the pre-existing banded human `view='frontier'`
  — both render the *same* `quest_frontier`, so there is no second ranking to
  drift. Tests: `TestLeaderboard`.
- **by-intermediate — DEFERRED to Slice 3.** One row per design, the reaction
  coordinate as columns (state rel-eV and `‡` step barriers) — the shape
  catpath's `compare` view emits, lifted to the quest. Blocked on the same two
  things as source-3 (§7.2): the candidate→pathway link doesn't exist yet, and
  the per-path profile is computed by catpath's graph code (catpath venv), so
  building it here would re-derive catpath logic. Lands when the harvest wires
  the link + a catpath-side profile stamp.

**Canonical rule:** the **quest frontier is authoritative** for the striving
(designs, multi-objective); catpath's own `compare` view is a compute-side
diagnostic over sibling pathways. The two never drift into "which leaderboard
is real": the quest's `leaderboard`/`frontier` views rank *designs*; catpath's
`compare` ranks *pathways*.

### 7.4 Structure model-building ops (Slice 4, precis-mcp)

`structure/ops.py` can edit atoms but not build surfaces. Add:

- a **slab-builder** op: `slab{element(s), miller=(1,1,1)|…, size, vacuum,
  layers, fix_bottom}` → a periodic slab cell (facet control lives here);
- an **adsorbate/molecule add** op: place a small molecule (H₂O, NO, …) at
  a Cartesian/frac anchor (the atoms-in-a-cell way to do solvation and
  reactant placement);
- these compose with the existing `set_element` (alloy) / `add_atom`
  (adatom) / `vacancy` (defect) so the big model builds a design from a
  seed and mutates it freely.

Alternative seed source: import real slabs from the library (§7.6) and edit
those, deferring the slab-builder. Decision at build time.

### 7.5 Adsorbate placement — anchor (v1), then site-search (rigor)

Placing an adsorbate needs an `xy + height`. Two tiers, and the cheap one
covers v1.

**v1 — the LLM anchors the active site (folds into Slices 2–3).** The LLM
does *not* hand-place every intermediate (the reaction network expands to
many). It marks the **active site once** on the structure via the existing
`eye` op (a named active-site embodiment). The Slice-2 adapter reads that
marker → an explicit anchor `xy` and passes it to `place_fragments()`
(`structures.py:115`) as the placement site for *every* intermediate; the ML
relax settles each into the nearest local minimum. **Gotcha:** an arbitrary
(non-fcc111) slab has no `adsorbate_info`, so `site_xy` falls back to a crude
"first top-corner atom" (`structures.py:73-83`) — the anchor must therefore
be an explicit `xy`/reference-atom, not a named site. That small
`place_fragments` spec extension *is* the real work of this seam.
Trade-off: a local minimum *at the anchor*, not the global-best site. If
placement matters, make it a **variable** — propose 2–3 anchors as separate
candidates and let the frontier keep the winner.

**Rigor (Slice 5, optional) — best-site search.** Wire catpath's existing
`poses()` (`structures.py:135`, an ensemble over sites×tilts): relax the
adsorbate at each pose, keep the lowest-energy, thread it into the NEB.
precis can *narrow* the poses geometrically first — `plane()` for the top
layer, hollow/bridge/top over surface-atom triangles/edges, filtered by
`coordination`/`neighborhood` — so catpath scores a short list, not a blind
sweep. Cost: N poses × a relax per intermediate. A rigor upgrade, not a v1
blocker.

### 7.6 Catalysis-library pull (Slice 6, precis-mcp)

A later, planned import (the stale `precis-dft` had a Materials-Project
ingest — do not reuse it; build fresh against the live `structure` kind).
Two uses: (1) **seed designs** — real relaxed slabs as starting structures
the big model edits; (2) **reference anchors** — known barriers/energies to
ground the loop and calibrate the ML backend. Source TBD (Materials
Project / OC20 / a curated set). Not on the critical path; promoted from
"optional" to "the natural slab source" for Slice 4's alternative.

### 7.7 The agentic tick tools-loop (Slice 3)

The current tick is a single LLM call (`tick.py:391`). For this quest the
*escalated* (opus, cascade) step becomes an **agentic tools-loop** so the
big model sequences its own investigation (do A, look, decide B) and every
tool call is a logbook line (the "harvest logs exactly what happened"
property, by construction). The substrate exists (the `claude_agent`
tools-loop dispatch). Toolbox:

```
edit_structure(parent, ops)     # deterministic ops → a new content-addressed structure
propose_sites(structure)        # (Slice 5) probe-proposed adsorption anchors
relax(structure, fidelity='ml') # struct_relax job → stability
catpath(structure)              # catpath_explore job → barrier graph
search_literature(q)            # grounding; papers serve the quest
log(entry, type)                # WORM logbook append
rewrite_dossier(text)           # living synthesis + frontier snapshot
suggest_next(quest)             # (Slice 7) Optuna acquisition → suggested next design point
```

The cheap default tick (haiku) stays single-shot bookkeeping/harvest; the
loop is reserved for the strategic escalation. Variant generation is **not**
delegated to a lesser model — an edit is a deterministic op-list the big
model emits; the machine applies it; the relax repairs geometry.

### 7.8 Optimizer advisor — suggest the next experiment (Optuna; later, but collect data now)

The *acquisition* layer on top of the frontier: given the history of
`(design params → measured objectives)`, suggest the next design point to
try. It **advises** the tick's big model — one more tool alongside the
literature and the frontier — it does **not** drive. The frontier says
"what's best so far"; this says "what to try next."

**The requirement it imposes — parametrize the design (do this NOW).** For
an optimizer to reason, each candidate must be a point in a named parameter
space, not just a pile of atoms:

- `meta.param_space` on the quest — the knobs the LLM chose + types/ranges,
  e.g. `{n_cu: int[0..6], n_h_embedded: int[0..4], roughness: float[0..1],
  facet: cat[111,100,211], adatom: cat[none,Pd,Cu]}`. Editable as the quest
  learns a new lever (define-by-run; TPE tolerates a growing/conditional
  space, at the cost of a data-poor new dim).
- `meta.params` on **every candidate `structure`** — its point in that
  space, stamped at propose time. **Start collecting this from Slice 1/3**
  so a clean `(params → barrier, formation_e)` dataset accrues before the
  optimizer tool lands. This is the cheap "collect data now" move; without
  it Optuna arrives to an empty history.
- a **param → structure decoder** the LLM authors per quest: how each knob
  becomes `edit_structure` ops. Fuzzy knobs need a concrete
  operationalization — `roughness` = RMS z-spread of surface atoms, or an
  adatom/vacancy count — so the decode is deterministic.

**The tool (later slice).** `suggest_next(quest) →` next param point + the
current Pareto set + a one-line rationale. Implementation: reconstruct an
Optuna study from the candidate history each call (`tell` all past trials,
`ask` the next) — **no persistent study; the `serves`-graph IS the study.**
Multi-objective sampler (MOTPE / NSGA-II) so the suggestion is drawn against
the Pareto front, not a scalarization. Runs in-process (cheap, no GPU). A
**pruner** maps onto the cheap-screen-before-catpath idea: a coarse signal
(catpath `preview`, a quick relax) kills a bad point before the full NEB is
spent.

**The LLM stays in charge.** It reads the suggestion, then either decodes it
into a structure or overrides with chemical intuition — Optuna proposes, the
model disposes. Early on (few trials) the suggestion is near-random and the
model's priors dominate; the advisor earns its keep once dozens of designs
exist. Categorical/combinatorial knobs (which *specific* atom) are the
optimizer's weak spot and stay the LLM's call. Honest framing: an advisor
that sharpens with data, not an oracle that finds the optimum.

## 8. Open decisions (for the build)

1. **Site-finder ownership** (§7.5) — recommend precis-proposes / catpath-scores.
2. **Seed slabs** (§7.4 vs §7.6) — slab-builder op now, or library import first.
3. **Backend** — MACE (deployed) vs FAIRChem/UMA (better for adsorbates) —
   a per-quest choice; MACE for the first light.
4. **First reaction R** — **DECIDED: NO → NH₃ on Pd(111)** (`network:
   ammonia`), catpath's own worked Pd example (`examples/no_to_nh3_pd.yaml`):
   substrate `NO`, target `NH3`, 3×3×4 slab, 10 Å vacuum, 2 fixed layers, EMT
   for dev / MACE for the real run. The dissociative-hydrogenation chain
   (NO → N+O → NH → NH₂ → NH₃) exercises the built `ammonia` state library, so
   first light rides an already-validated network — no new chemistry to author.
   (Alt on hand: `no_to_no3_pd.yaml`, NO → NO₃ oxidation, if a shorter linear
   chain is wanted for the very first smoke run.)

## 9. What this explicitly does not do (v1)

DFT confirmation · implicit or explicit solvation · non-(111) facets ·
multi-stage graduation · an embedding proposer · autonomous scheduling
(`PRECIS_QUEST_LOOP_ENABLED` stays off; force-stepped until trusted).
