---
status: draft
title: nm stick placement — interaction-aware module pose solve (graded π-stack, form-finder seeded)
prio: high
model: opus
---

# nm stick placement: boxes → modules with live interaction ranges

Request (Reto, 2026-09-10): go from boxes (L1 envelopes) to **modules**
that keep their atomic interactions at the distances those interactions
are sensitive to — a benzene π face stacks when parallel within a
center-to-center window, degrades gradually with tilt/offset, and does
nothing at range — and have coupling *engaged or avoided during
assembly*, not merely checked afterwards. Evaluate BuildAMol as the
stick-chemistry substrate; consider commit `1530050d` (the se
force-density form-finder) for the solve.

## Position in the pipeline

Sits between the logical nm design and the module fitter's splice
(`docs/backlog/nm-module-fitter.md`, in flight in the
`snug-mapping-giraffe` tree). Fitter v1 deliberately places by seam
construction from declared poses, translation-only, and its
`interaction`-kind seams are **post-hoc pass/fail checks**
(`fit.interaction_met`/`unmet`). This item supplies what that leaves
open: poses *derived from* the interaction intent — the top-down half
gets a solver instead of hand-set poses. Output is poses only
(stamped `origin: 'proposed'`); the fitter still owns realization.

## Interaction features on modules

A module (or any block) carries typed **features**, each a pose plus
range parameters — the coarse stand-in that preserves the sensitive
geometry of the underlying atoms:

- `pi_face` — center, unit normal, effective radius (a benzene ring:
  centroid, ring normal, ~1.4 Å).
- `hbond_donor` / `hbond_acceptor` — site + direction vector.
- `charge_site` — position + sign/magnitude (screening-tier).

One fact, two projections (the standing port rule): **declared** while
the block is unfilled (the spec), **derived** from the bound structure
once filled (ring centroid/normal from the existing `structure`
rings/fragments machinery) and checked against the declaration — drift
is a finding, never a silent second copy.

## Graded pair potentials (advisory-honest, cited)

Per feature-type pair, a smooth score — not a force field, a
screening-tier shaping term, provenance-tagged like `mechanics.py`
("coarse dimer-benchmark fit; dimer-DFT slice is the truth layer"):

- π-stack v1: `A · f_r(d) · f_θ(θ) · f_off(s)` with `f_r` a well
  centered ~3.4–3.9 Å plane separation, `f_θ = cos²θ` between normals
  (gradual, per the request), `f_off` tolerant of ~1–2 Å lateral offset
  (parallel-displaced is the benzene-dimer optimum, ~2.7 kcal/mol
  CCSD(T); perfect sandwich is slightly weaker — cite Sinnokrot/
  Sherrill). T-shaped is a real second basin: *noted, not modelled* in
  v1. Steric floor comes from the existing overlap/SDF checks, not from
  this term.
- **Avoid-coupling is the same machinery negated**: two π features
  *without* a declared interaction connect that score above threshold →
  repulsive term in the optimizer + `unintended_coupling` warn — the
  graded upgrade of the fitter's flat vdW+1 Å through-space flag, and
  the same pairs feed its later dimer-DFT correction slice.

## Azobenzene: features are per-state (Reto, 2026-09-10 — "azobenzene
interactions in particular are cool")

Block states are already specced
(`functional-block-library-and-assembly-states.md`,
`photoswitch-states-and-spectral-dof.md`, and the fitter's per-state
bound structures) — features ride that mechanism, one feature set per
state, no new state machinery here. The states differ in exactly the
way that makes this layer interesting:

- **E (trans)**: near-planar — one coherent π face spanning both rings;
  stacks well.
- **Z (cis)**: bent (~55° ring twist) — two small misaligned faces, no
  single stacking plane; scores near zero against a flat partner at the
  E-optimal pose.

So a declared π-stack connect can be **engaged in E and released in Z
at the same poses** — light-toggled coupling, evaluated by scoring the
same arrangement under each state's feature set (the fitter's
fit-each-state pattern, extended from geometry delta to coupling
delta). The avoid side is load-bearing here too: tightly stacked
azobenzenes (H-aggregate territory, dense SAM packing) are known to
shift absorption and *suppress isomerization* — an unintended-coupling
warn on a switch module protects its function, not just its optics.
Both effects are exactly the pairs the fitter's dimer-DFT correction
slice later computes properly.

## The solve — two stages, 1530050d in stage 1 only

1. **Skeleton form-find** (reuse `1530050d`, already on main —
   `src/precis/structsolve/formfind.py`, pure and domain-neutral).
   Covalent connects → members with stiff tension-positive `q`;
   module centers as nodes; anchors from fixed poses. One linear solve,
   **no initial guess** — that is the property worth renting: a
   topology-only starting arrangement. The nm bridge copies
   `precis_se/formfind.py`'s contract verbatim (proposed-origin
   write-back, `move=` authorization, anchors never move, loud
   singularity) — one more datum for
   `nm-se-shared-blocktree-core.md`.
2. **Rigid-body pose relax.** Orientation terms do NOT go through FDM
   (members are axial, `q` fixed; a ring normal is not linear in node
   coordinates — a rigid-cluster + iterated-q shoehorn buys nothing over
   doing it directly). Instead: scipy minimize over per-module
   `(t, R)`, objective = Σ intended-coupling scores − unintended
   penalties − envelope-overlap penalty, covalent members as stiff
   springs holding stage-1 topology. pcb optimizer discipline applies
   (two-sided admissibility, per-move delta locality, undefined ≠ 0).
   `structsolve` itself stays untouched.

## The cost function (Reto, 2026-09-10: "also, cost function")

One function, one module (`stickcost.py`-shaped, pure over arrays),
consumed by all three clients — the `solve_poses` objective, the
option scorer, and `view='couplings'` — so hints, solver, and verdicts
can never disagree (the one-resolver rule, applied to cost). The pcb
discipline's three axes (`pcb/cost.py`: cost / estimator fidelity /
constraint hardness) transfer directly:

- **Hardness — two layers, not one sum.** *Feasibility* terms are
  constraints wearing penalty coats and aggregate by **worst margin**
  (margin-max): steric interpenetration, and **linker reach** — a
  port-pair whose distance leaves the spacer family's reachable band
  makes the downstream splice impossible, so it is feasibility, not
  preference. At convergence a nonzero worst margin is reported as
  infeasible, never traded away against coupling gains. *Preference*
  terms aggregate by **weighted sum**: per-connect `engaged` cost
  `w·(1 − score)`, `avoid` cost `w·score`, undeclared-proximity at a
  lower default weight (it is a warning, not a contract), plus a small
  regularizer pulling moved blocks toward their declared rough poses —
  the author's sketch is information, not noise. *Gauge* terms are
  report-only (total unintended score, placed fraction) and never
  enter the objective.
- **Toggle intents cost the contrast.** A per-state intent ("engaged
  in E, released in Z") is two terms over one pose set — or
  equivalently a contrast term on `score_E − score_Z`. Optimizing only
  the on-state and *hoping* the off-state releases is exactly the
  silent failure the azobenzene section warns about.
- **Estimator fidelity is declared, not laundered.** The screening
  potential shapes poses; it is not admissible against the dimer-DFT
  truth layer and must not double as the verdict. Verdicts come from
  the measures/graded-goals machinery (target/tol, hard/soft/gauge);
  the cost function's job is to *point the solver at* geometries the
  measures will then judge. Every rendered cost carries its fidelity
  tag; a term the estimator cannot evaluate is `unpredicted`, never 0.
- **Pairwise decomposition** so a move re-prices only touched pairs —
  this is what makes the inline op echoes cheap (per-move delta
  locality), and it is a property test, not a hope.
- **Weights** are dimensionless trade-off declarations on [0,1]-scored
  terms, default 1, set only via intent (the "what the LLM sets"
  rule); the Pareto-conflict question above is how a weight gets a
  value — the solver never invents one.

### Thresholds are intent — and the design measure is kT
(Reto, 2026-09-10: "thresholds set by the LLM… acceptable
interaction, acceptable side effect — what is a good design measure?")

The physics/spec split lands here precisely: the *potential
parameters* (well depth, r₀, widths) stay library-owned and cited;
the *acceptance thresholds* — what counts as coupled enough, what
side effect is tolerable — are design intent, declared by the LLM as
graded goals on measures (`target`/`tol`, `hard`/`soft`/`gauge` —
`structure/measures.py`'s existing machinery, no new verdict code).

**The measure: coupling energy in units of kT at a declared operating
temperature** (default 300 K, an explicit design setting). Why kT and
not the raw [0,1] score or kcal/mol:

- it is self-explanatory to the LLM and the reader — "side coupling
  0.4 kT" *means* washed out by thermal motion; "engaged at 4.6 kT"
  means survives it. A [0,1] score is arbitrary units; kcal/mol makes
  every threshold silently temperature-dependent anyway;
- the potentials are calibrated from dimer benchmarks in kcal/mol, so
  the conversion is one declared constant, and the later dimer-DFT
  truth layer reports in the same currency — screening estimate and
  verification stay commensurable;
- it transfers unchanged to the later feature types (H-bond ~2–10 kT,
  charge pairs more) — one yardstick, not one per feature.

Sensible defaults the LLM overrides as intent: `engaged` wants
≥ 2–3 kT (soft at target, hard well below); side effect / `avoid`
tolerates ≤ ~1 kT (warn above soft, error above hard at a few kT);
toggle contrast is the *difference* in kT between states. The
screening estimator's honesty problem (factor-≈2 uncertainty) is
absorbed exactly by the soft/hard band structure — a threshold inside
the uncertainty band renders its verdict with the fidelity tag, and
the pair lands on the dimer-DFT flag list rather than pretending
precision.

## The MCP surface — the interactive build loop (Reto, 2026-09-10:
"hints, feedback, questions to ask the LLM, what to set, collisions")

No new verbs. The agent's loop is: **edit op → inline delta findings →
occasionally `solve_poses` → read `view='couplings'` → adjust.** The
design must stay representable half-built at every step
(filled-fraction honesty), so mid-build problems are findings, not
refusals — with the exceptions named below.

### What the LLM sets vs what it never sets

- **Sets**: intent (`connect` with `interaction: {"couple": "engaged"
  | "avoid", "state": "E", "weight": …}` — `avoid` is *declared*, not
  a default, so the solver gets a sign and the proximity warn is
  dischargeable), anchors/fixed poses, `move=` authorization,
  `state=` for evaluation, optional weights — and **acceptance
  thresholds** as graded goals (see "Thresholds are intent" below):
  how strong an intended coupling must be, how much side-effect
  coupling is tolerable. Thresholds are spec, not physics.
- **Never sets**: potential parameters (`A`, `r₀`, widths) — those are
  library facts with citations, resolved most-specific-first through
  one resolver (the `pcb/rules.py` pattern). An LLM tuning the physics
  to make its design pass is the failure mode this split prevents.
- **Swallowed-facet guard**: any op key or setting the handler does not
  honour errors loudly — never silently ignored (the search-facets
  lesson, applied at design time).

### Feedback: `view='couplings'` + inline op echoes

Each edit op's result echoes delta findings for what it touched only
(per-move delta locality — pair checks against the new/moved block,
cheap and inline); full validate is on demand. `view='couplings'`
renders per declared-or-proximate feature pair:

- the three factors **separately** — distance, tilt, offset, each as
  measured / ideal / score-factor — never one opaque number, so the
  LLM knows which knob to turn;
- verdict vs intent: `engaged` / `intent_unmet` / `unintended` /
  `released` (per state: an azobenzene pair renders `E 0.82 · Z 0.04`
  — the toggle *contrast* is the design goal, so the delta is a
  first-class rendered number);
- a **hint** — the cheapest op that improves the dominant deficit, in
  op vocabulary (the house `suggested_fix` rule): "tilt 38° is the
  dominant loss; `set_pose` rot z≈35° on `pillar-b`, or authorize
  `solve_poses move=['pillar-b']`". Hint preference order: a move
  within a *declared DOF* first (preserves intent), then a pose nudge
  on `proposed`-origin blocks, then move authorization on user-origin
  blocks, then (last) "the topology can't satisfy this — change a
  connect/envelope".

### Questions the machine asks (refusal-with-question, never a guess)

1. **Move authorization** — `precis_se/formfind.py`'s contract
   verbatim: user-origin poses never move silently; `solve_poses`
   refuses and *names the blocks it wanted to move*; the LLM re-issues
   with `move=[…]`. The refusal is the question.
2. **Undeclared proximity** — two π features scoring above threshold
   with no connect → `unintended_coupling` warn phrased as the choice:
   add an `engaged` connect or an explicit `avoid` connect. Warns
   persist until discharged by a declaration; they never auto-resolve.
3. **State ambiguity** — solve/score over stateful modules defaults to
   each module's declared ground state and *says so in the result
   header*; if intent references a state the module lacks, refuse
   naming the states that exist.
4. **Infeasible intent** — two `engaged` couplings geometry cannot
   satisfy together: report the Pareto conflict with both scores at
   the best compromise found, and ask which yields (or what weight
   ratio) — never silently split the difference. `weight` exists for
   the answer, not for the solver to invent.

### Collisions while building

- **Two tiers, existing machinery**: hard steric = envelope SDF
  interpenetration beyond declared contact (error tier); crowding
  within the soft margin = warn. Both render measured numbers +
  `suggested_fix`, findings-not-refusals mid-build.
- **`solve_poses` is the exception**: it refuses to write back a pose
  set that still contains hard overlaps among the blocks it was
  authorized to move — the `formfind` singularity posture (a confident
  wrong shape is worse than no answer). The refusal reports the
  colliding pair, the residual interpenetration, and the resolution
  ladder above.
- **Collision vs a user-origin block** is case 1: the solver may not
  move it, so the refusal asks for authorization or an intent change,
  with the numbers.
- **The maze rule**: "no collisions" renders beside placed/filled
  fractions — collision-free on a sparse design must read as sparse,
  not done.

Skill file only after the slice ships (standing nm rule); until then
the op/view self-descriptions carry the vocabulary above.

## Fisheye reading + scored options (Reto, 2026-09-10: "general idea
of global graph, better idea of local env, excellent hyperlocal")

The LLM never reasons over raw coordinates well; it reasons over
graphs and small egocentric numbers. So the read surface is a
**focus+context view** — `view='around'`, `focus=<block|port|feature>`
— with three rings of sharply different fidelity, each with its own
coordinate convention:

1. **Global — topology, no geometry.** The whole block hypergraph as
   one-liners: names, connect kinds/intents, per-connect a single
   coarse bucket (`engaged · strained · far · colliding`). O(blocks),
   always fits. World coordinates never rendered here — the global
   ring answers "what exists and what touches what", nothing else.
2. **Local — the interaction neighbourhood.** Blocks within feature
   cutoff of the focus plus 1–2 covalent hops: envelope, pose
   *relative to the focus*, feature list, pairwise scores with the
   three factors at ~0.1 Å / degrees resolution. Truncated by
   relevance (score × proximity) under an explicit token budget —
   degrade by dropping the least-relevant neighbour, never by
   coarsening the focus.
3. **Hyperlocal — egocentric and exact.** The focus in its own local
   frame: "partner π face 4.1 Å along +z, tilted 38°, offset 1.9 Å
   toward +x" — the frame the LLM can actually do geometry in. Full
   feature parameters, seam atoms if bound, 0.01 Å. Always complete;
   the budget squeezes ring 2, never ring 3.

This is the keystone-kind rule ("the LLM traverses a graph, never
pixels") made quantitative: fidelity is a function of distance from
focus, and every ring says which frame its numbers are in.

### Options are generated, scored, and materialized — never advice

"Use a longer spacer" is not a finding; a finding's options block is
top-k entries from a **deterministic move-set**, each *evaluated by
the same scorer before being offered* and materialized as the literal
op script that applies it:

- rotate about a declared DOF (cheapest, intent-preserving);
- pose nudge on a `proposed`-origin block / `move=` request otherwise;
- **longer/shorter spacer** — a splice parameter, not a vague idea:
  the fitter's cap-length convention already varies (C3 default, the
  manifest field exists), so the option renders as "re-splice connect
  a↔b with C5: predicted separation 6.8 Å, π score 0.85 → 0.03" —
  predicted by rigid geometry propagation through the potentials,
  cheap, honesty-tagged as a prediction;
- reroute the connect to a different port; swap the module for a
  library variant (ranked library search is
  `blocktree-library-build-plan.md`'s machinery).

Each option = predicted score delta + side-effects (what else moves,
which other pairs' scores change past a threshold) + the op script.
The LLM chooses; nothing applies implicitly. An option whose predicted
outcome the solver cannot honestly estimate renders `unpredicted`, not
a number (undefined ≠ 0).

## BuildAMol — evaluated, not adopted for this stage

[BuildAMol](https://github.com/NoahHenrikKleinschmidt/buildamol)
(J. Cheminformatics 2024) is fragment assembly over *bonded* molecules:
linkage recipes (attach/delete atoms) + torsion-space "Rotatron"
optimizers (distance/overlap/forcefield). It has no multi-body rigid
placement under custom non-covalent fields — the stick stage is exactly
the part it lacks, and its generic linkage deletion knows nothing of the
fitter's seam-preservation convention (which exists to keep precalced
DFT valid). Where it *could* earn a place later, behind the `[chem]`
extra / a container like rdkit:

- library seeding convenience (PubChem/PDBE/CHARMM templates →
  capped modules);
- post-splice conformer polish via a custom Rotatron whose objective
  includes the π-stack score.

License: AGPL-3.0 — compatible with this repo's GPL-3.0-or-later;
container/extra placement keeps the combined-work question away from
the core anyway. v1 of this item needs nothing from it (fitter owns
splicing, rdkit owns conformers).

## Dogfooding: build the library, join it, on the product surface
(Reto, 2026-09-10)

Library growth and joining are themselves the dogfood — done as prod
`nm` designs through the runtime surface (the nm-demo-c60 precedent),
not as test fixtures. The library substrate (cross-design instancing,
ranked library search, complementary ports) is
`blocktree-library-build-plan.md`'s critical path — this section is a
*consumer* of it, not a second plan:

1. **Seed modules**: azobenzene first (both states, capped per the
   fitter convention), then diarylethene/spiropyran/crown ether from
   the fitter spec's curated set + generator families already live.
   Each seeding run exercises `from_smiles` → cap attach → port
   declaration → feature derivation end-to-end.
2. **Join them**: the fitter spec's stitched-tower example — three
   azobenzene pillars at three switching wavelengths, rungs at several
   heights — is the natural target: it needs exactly this item
   (per-state stacking engaged/avoided between pillars) plus the
   fitter's cycle stitching, and it is the photonic-arm quest's
   (qu330435) chemistry. Dogfood designs land as ordinary designs with
   lineage; what breaks becomes gripes.
3. **Recursive growth** (fitter spec): a fitted assembly with open caps
   is itself a library module — the library compounds by composition.

Standing caveat applies: the session MCP writes PROD — seeding is
sanctioned deliberate writing; write-path *testing* stays on the dev
DB.

## Can it be built? Yes — inventory verified 2026-09-10

Every rented piece exists on main; the new code is bounded:

- **Stage-1 solver**: `src/precis/structsolve/formfind.py` shipped
  (`1530050d`), pure numpy — the nm bridge is small glue, shape proven
  by `precis_se/formfind.py`.
- **Blocktree spine**: `src/precis/blocktree/` (ops/types) is on main
  (`28877919`, `96690d37`) — features-as-fat-ports extend one shared
  place, serving nm and se both.
- **Verdict machinery**: `structure/measures.py` hard/soft/gauge is
  live; thresholds reuse it verbatim.
- **Optimizer**: scipy is NOT a core dep (numpy is). Two honest
  options: pure-numpy gradient descent on `(t, R)` with analytic
  gradients of the smooth potentials — precedented by the
  cyclodextrin generator's in-process numpy relax — or scipy behind
  an extra in a worker job. v1: pure numpy, multistart from the FDM
  seed, inline below a size threshold, enqueued above it (the
  thread-pool lesson).
- **Feature derivation**: ring centroid/normal from `structure`'s
  existing rings/fragments machinery.

New code, in build order (each slice lands green and is useful alone):

1. Potentials + cost module (pure, arrays, the three-axis structure) +
   the kT measure plumbing — smallest slice, immediately useful as a
   check even before any solver.
2. Feature schema on ports (blocktree extension + migration) +
   per-state derivation/drift check.
3. `solve_poses` (FDM bridge + rigid relax) with the write-back
   contract; `view='couplings'` + inline echoes.
4. Fisheye `view='around'` + the scored-options generator.

**Risks, named**: (a) nonconvexity — rigid-body pose landscapes have
local minima; mitigated by the FDM seed, multistart, and the fact
that the loop is *interactive* (the LLM steers between solves; this
is a design assistant, not a black-box global optimizer). (b) tree
collision — this touches `precis_nm`/`precis.blocktree` while the
module fitter (`snug-mapping-giraffe`) and the library build plan are
in flight in siblings: sequence after the fitter lands, or slice 1
first (new module, no file overlap). (c) threshold defaults inside
estimator uncertainty — handled by the soft/hard band + dimer-DFT
flag path, but the defaults themselves want one dogfood pass before
being trusted.

## Open questions

- Feature schema home: `nm_ports.roles` already names `π-stack` —
  are features fat ports (role + geometry params) or a fourth table?
  Leaning fat ports: one attachment/interaction vocabulary, no new
  persistence shape.
- Does stage 2 run inside the fitter's `fit` op (a `place='solve'`
  mode) or as its own op the fitter consumes? Leaning own op — poses
  are proposals reviewable before any splice mints atoms.
- H-bond/charge terms in v1 or π-stack only first? Leaning π-stack
  only (the request's example; smallest honest slice).
