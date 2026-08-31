---
status: draft
title: nm (nanomachine) kind — hierarchical envelopes over atoms, the fourth keystone kind
prio: high
model: opus
---

# The `nm` (nanomachine) kind

## Decisions (Reto, 2026-08-31)

- **Kind name: `nm`** — nanomachine.
- **Units: ångström, float64, everywhere.** No fixed-point/integer
  coordinates: rotations produce irrational coordinates regardless (fixed
  point would round after every transform, accumulating error rather than
  avoiding it), relax/anneal needs continuous gradients, and the whole
  reused substrate (structure Scene, cad kernel, ASE, numpy) is float64.
  Precision is a non-issue: ~15–16 significant digits resolves ~1e-12 Å at
  a 1000 Å assembly scale. The one thing fixed point buys — stable equality
  for caching/dedup — is solved the house way already: quantize **at the
  hash boundary only** (`structure/canonical.py::geom_hash_c` pattern),
  never in the representation. Declare the unit once, resolver-style; the
  cad kernel is unit-agnostic float64 and is used here as Å.
- **Building blocks are not pcb components.** A pcb component is a part
  bought from a store; an `nm` building block is *made* — from atoms, or
  recursively from other blocks — with software-style modularity and reuse.
  To honor one-word-one-meaning (the `component` kind is procurement),
  the `nm` object is called a **block** below.

Design session 2026-08-31 (Reto + agent), from the nano3d worktree. Goal:
an LLM tool surface for designing molecular machines — rotaxanes, molecular
motors, length-changing structures — as a **hierarchical description first**
("disc, 2 Å high, 10 Å diameter, alternating rim charges; fork; axle joins
them"), then LLM-guided **fill** with real chemistry (lit-searched fragments
attached at named ports), then validation, then property views (charge,
optical), and eventually mechanism/dynamics.

This is the **fourth keystone kind** (glossary: "owns a legible IR and rents
the heavy kernel only at export; the LLM traverses a graph, never pixels"),
sibling to cad (ADR 0041) / pcb (0042) / structure (0043). Ship as a
**plugin** (Route B: entry points, own migration namespace, dark behind a
`requires_setting` flag) so core dispatch stays untouched.

## Corrected premise

There is **no VSEPR, hybridization, bond-angle, or torsion validation
anywhere in src/** (verified by grep 2026-08-31). `structure`'s
`validate.py` checks overlap / over-coordination / bond length / valence
budget only; angles are *measured* (`measures.py`) but never judged. The
chemistry-rules layer is new work, and it is useful on its own (slice 1).

## Reuse map — what exists one domain over

| Need | Existing piece | Anchor |
|---|---|---|
| Atom fill layer, molecule mode | `structure` Scene/Cell (`pbc=(F,F,F)`), typed ops, probes, eyes/measures with graded goals, relax ladder clean→emt→ml→dft, fragments/rings/diff | `src/precis/structure/` |
| DRC findings shape | `rule`/`atoms`/`measured`/`expected` + `suggested_fix` **in the op vocabulary**; hard-reject gate before GPU spend | `src/precis/structure/validate.py::validate` |
| Envelope geometry + interlock clearance | analytic primitives, `component_sdf` exact-sign CSG SDF — its docstring names the shaft-in-bored-hub false-collision trap, which is *precisely* rotaxane topology | `src/precis/cad/relate.py::component_sdf`, `::clearance` |
| Instancing (reuse a sub-assembly) | `Design.instance(template, xform)` — in the kernel, not yet in any text language | `src/precis/cad/graph.py::Design.instance` |
| Progressive-enrichment IR | six-level ladder; L2 combinatorial embedding stored explicitly, never re-derived from coordinates; arrays-not-objects | `src/precis/pcb/ir.py::Level` |
| Optimizer discipline | cost/estimator-fidelity/constraint-hardness as three axes; two-sided admissibility; move-reachability property test; per-move delta locality; undefined ≠ 0; money-sum vs margin-max aggregation | `src/precis/pcb/cost.py`, `optimize.py` |
| Intent without rule tables | objective vectors on connections ("decap near IC" is a consequence, not a rule) | `src/precis/pcb/objectives.py` |
| One rules resolver | most-specific-first, always clamped to the physical floor, every consumer reads through it | `src/precis/pcb/rules.py::resolve_net_rules` |
| Attachment capacity precomputed | escape capacity is footprint-intrinsic, cached at L0 | `src/precis/pcb/escape.py` |
| Assembly-tree verbs | `contains` edge with cycle check, `view='tree'`/`'bom'` rollup (right *shape*, wrong substrate — procurement) | `src/precis/store/_component_ops.py` |
| Synthesizability gate | retrosynthesis `route` kind (AiZynthFinder/ASKCOS in containers) | `src/precis_chem/` |
| ms property panel shape | epistemically graded hypothesis-generator, ladder to slower rungs | `src/precis_estimate/` |
| LLM fill loop | propose-only jobs: no tools given to the model, dry-run the reply before anyone sees it | `job_types/cad_propose.py`, `structure_propose.py` |
| Lit search | paper / semanticscholar / perplexity-* / patent / wikipedia + `structure` `view='literature'` deterministic design→query loop + paper-provenance links with rationale notes | `handlers/structure.py` |
| SMILES/name → 3D fragment | rdkit lives in `[chem]` extra, off the request path — conformer gen (ETKDG) belongs in a worker job/container | pyproject `[chem]` |

## The IR — six levels, molecular content

Same invariant as pcb: dropping everything above level k leaves a valid
level-k object; a move at level k dirties only levels above.

- **L0 — block hypergraph.** Blocks + ports + intent connections
  (bond-to-be, non-covalent interaction, "must-face"). No geometry.
- **L1 — envelopes.** Per-block analytic envelope (cad primitives, in Å) +
  rough pose; clearance/enclosure via `component_sdf`.
- **L2 — topology & stereochemistry, stored explicitly.** Mechanical
  interlocking (macrocycle threaded on axle), declared DOF (axle =
  rotational DOF about the port–port axis), chirality. A rotaxane's
  threading is a topological invariant — it must never be re-derived from
  L3 coordinates, exactly the pcb L2 rule.
- **L3 — fragment placement.** Chosen chemical fragments posed inside
  envelopes, ports mapped to real attachment atoms.
- **L4 — metric annotations.** Measure values, charge estimates, strain.
- **L5 — realized atoms.** A full `structure` Scene: validated, relaxed.
  Fill state lives in `structure`; this kind owns L0–L4 and the binding.

## Core objects

- **Block** — a building block made from atoms or, recursively, from other
  blocks: nested (children), instanceable (a sugar defined once, placed
  seven times), with envelope, ports, declared DOF, and free-text
  `desc:`/`use:` for intent search. Combines `component.contains`'s tree
  shape with `cad`'s `Design.instance` geometry — that combination exists
  nowhere yet and is the genuinely new core. Unlike a pcb component
  (bought), a block is *designed* — the library grows by composition, the
  way a software module tree does.
- **Port** — a named attachment point: position on the envelope, bond
  vector, expected hybridization/valence budget. Per-fragment port
  capacity precomputed (escape.py analog).
- **Objective vectors** on connections/ports instead of a rules table:
  "photoswitchable", "low rotational barrier", "rim charge alternation",
  "crown 2–3 Å from rim" — measures with graded goals already carry the
  verdict machinery (`structure` measures: target/tol, hard/soft/gauge).

## Level-scoped DRC (the "suspend checking" answer)

Checking is not suspended — it is **scoped to the level that exists**. A
ring under construction is an L3 object: valence budgets and port
compatibility apply (graph feasibility, ir.py side), VSEPR angles and
strain do not until L5 realization + a `clean`/`emt` relax, after which the
geometric rules run (drc.py side). Two-tier margins throughout: physically
impossible (error) vs house style / high strain (warn), every finding with
numbers and a `suggested_fix` in op vocabulary. And the maze.py warning
transfers verbatim: **zero DRC is trivially achievable by filling nothing**
— every validation number reports beside a filled-fraction count.

## New chemistry-rules content (absent today)

- Hybridization inference from the bond graph (neighbor count + bond
  orders → sp/sp²/sp³), VSEPR ideal angles + tolerance by hybridization,
  torsion sanity, ring-strain screen (small-ring angle deficit), planarity
  of aromatic/conjugated systems. All as new rules in the
  `validate.py` findings shape; resolved through one resolver
  (rules.py pattern). Extend the 16-element Cordero table as needed
  (Br, I at minimum).

## The fill loop

1. LLM reads the scaffold (L0–L2 TOC) + objective vectors.
2. Lit search ("disc-shaped molecules, alternating rim charges" →
   cyclodextrins, pillararenes…) via existing kinds; candidates linked
   with paper-provenance + rationale notes.
3. Fragment realization: SMILES/name → 3D conformer in a worker job
   (rdkit in container), landing as a `structure` design with port
   metadata; envelope fit-check (bbox/SDF) before any attach.
4. `attach` op: instantiate fragment at a port (bond vector aligned),
   graph-feasibility DRC inline, relax + full DRC at realization.
5. Propose-only jobs for the generative steps (cad_propose pattern);
   heavy ops always enqueued, never inline (thread-pool lesson).
6. Synthesizability: `route` retrosynthesis as an advisory cost term /
   gate on chosen fragments and (later) on the assembled molecule.

## Later phases (plan for, don't build)

- **Charge/optical panel** — estimate-shaped (ms, epistemically graded):
  Gasteiger/EEM partial charges, dipole, crude polarizability/chromophore
  flags; ladder to xtb/DFT rungs on the GPU node.
- **Mechanism analysis** — rotational DOF: rigid-group torsion scan via
  the relax ladder (barrier profile about a declared axle); interlock
  verification via SDF sweep; force/compliance chains (crown→fork→hub
  compressive path, rim stretch) from relaxed-geometry deltas under
  applied constraint displacement.
- **Dynamics** — no MD engine in-tree (no openmm/xtb dep); rent it like
  everything else (container job), consume trajectories as measure
  time-series over declared DOF. Representation deliberately deferred;
  L2 declared DOF + measures are the hook it will attach to.

## Slices (ordered, each independently valuable)

1. **SHIPPED 6389cb66 (2026-08-31)** — chemistry DRC deepening in
   `structure`: `vsepr.py` warn tier (hybridization inference, angle
   strain, π-twist, small-ring, hybridization conflict), two-tier
   `validate` view, Br/I elements. Warn tier never gates a relax.
2. **SHIPPED 5bae2429 + 2c7e46f8 (2026-08-31), complete** — fragment
   ops in `structure`: `ring` template, rigid-body `attach` (open-valence
   alignment, MIC-correct bond images), handler-level `import_fragment`
   with label-mapping echo, `from_smiles` (lazy rdkit behind `[chem]`,
   seeded ETKDG). Port *metadata* is slice 3's (plugin-owned).
3. **The new plugin kind (L0–L2)** — IN PROGRESS. Round 1 SHIPPED
   618d516d (2026-08-31): plugin skeleton, 0001 migration (all three
   tables), block tree + instancing (read-time template resolution,
   expansion-cycle guard at op AND render time), cad-DSL envelope
   validation, tree/block views, search card, dark behind `nm.enabled`.
   Side-fix that ship forced: `tools/core.py::edit` now declares +
   forwards `ops=`/`args=` (ratchet entries ("structure","edit","ops"/
   "args") retired; doors round-trip test added; tools/list cap 23→24 KB
   with ledger entry; testmon selection gap → gr277298). Round 2 SHIPPED
   730d6a93: ports with capability roles (declared-intent trust model),
   name-keyed connects + objectives slot, capability gate at op time,
   L0 validate view (2 tiers), lockstep persistence, instance-mediated
   guard fixes. Remaining: round 3 = envelope clearance (cad SDF) +
   bind_structure + declare_dof/declare_threading; then the closing
   round (skill file + overview row) ships the slice.
4. **Fill loop wiring** — propose jobs, lit-search integration,
   level-scoped DRC, objective-vector verdicts, filled-fraction honesty.
5. **Synthesizability + charge/optical panels.**
6. **Mechanism (torsion scans, interlock proof), then dynamics.**

## Slice 3 design (drafted 2026-08-31, pre-build)

**Package**: `src/precis_nm/` (Route B plugin) — `precis_nm.handler:NmHandler`
(kind `nm`), entry points in pyproject (`precis.handlers`,
`precis.migrations` → `precis_nm.migrations`, own 0001), dark behind
`requires_setting=("nm.enabled",)` — the `precis_chem` skeleton verbatim.

**Storage** (0041 rule: ONE `card_combined` chunk per design for intent
search; geometry/graph in dedicated tables, never chunks):
- `nm_blocks` — ref_id · parent_block_id (tree, cycle-checked) ·
  template_block_id (instances — define a sugar once) · name · pose
  (xyz Å + rot deg) · envelope (cad mini-DSL config string, Å units —
  reuse `precis.cad.dsl` + primitives; one grammar, two kinds) ·
  desc/use · dof JSON (`{"kind":"rotational","axis_ports":[…]}`).
- `nm_ports` — block_id · name · roles text[] (capability set) · bond
  vector · expected element/hybridization · bound atom (structure slug +
  atom label, NULL until filled — the one-fact-two-projections port).
- `nm_topology` — L2 invariants stored explicitly, never re-derived:
  threading (A through B), chirality marks.

**Verbs** (no new ones): put/edit take typed ops (structure-style JSON):
`add_block · instance_block · add_port · connect` (port↔port intent
edge with objective vector) `· declare_dof · declare_threading ·
bind_structure` (+ removals). get: `view='tree'` (nested TOC) ·
`'ports'` · `'clearance'` (two blocks, envelope SDF via
`cad/relate.py::component_sdf` at Å) · `'validate'` (L0–L2 feasibility:
port-capability match on connects, tree cycles, envelope overlap beyond
declared contact — error/warn tiers like structure). search: one card.

**Build order within the slice**: scaffold (plugin skeleton + migration +
dark flag + empty handler registering) → block tree + ops + tree view →
ports + connect + validate → envelope clearance via cad kernel →
bind_structure. Each lands green; skill file only at the end (write no
skills until the slice ships).

**Round-2 constraint (reviewer, 2026-08-31)**: `save_tree` is
retire-all/reinsert-all, so every save rebuilds `nm_blocks.id` — ports
MUST persist in lockstep keyed by (block name, port name), never by
block row id, or every save silently strands the port rows (soft-retire
means no FK error fires). Instance-expansion cycles are guarded at op
time AND at render time (render never trusts stored data to be acyclic).

## Open questions

- **Where slices 1–2 land**: extend `structure` (they are useful to
  catalysis work too) vs start in the plugin. Leaning: slice 1 in
  `structure` core, slice 2 split (library/attach ops in structure,
  port semantics in the plugin).
- **Block identity for leaf blocks**: are library blocks ordinary
  `structure` designs + a port sidecar, or plugin-owned rows referencing
  them?
## Transferred from pcb-component-model.md (read 2026-08-31)

- **Ports carry capability sets, not equivalence relations.** A port
  affords roles ({covalent-single, coordination, π-stack, H-bond-donor…},
  the pin→roles table pattern); legal attachments are derived at bind
  time, never stored. Nothing attaches unless the capability affords it —
  default toward the loud failure.
- **Model the agreement, not the two sides.** The port is ONE fact with
  two projections: the scaffold-side stub (envelope + bond vector) and the
  atom-side attachment atom. Envelope↔fill drift is the five-of-nine
  silent-defect generator one domain over; don't reproduce it.
- **Envelope is derived, not stored twice**: vdW body of the realized
  atoms + a margin parameter (the Body→courtyard rule). While unfilled,
  the declared envelope is the spec; once filled, the derived one is
  checked against it (a measure, not a second stored envelope).
- **Don't model constraints discoverable on the cheap side of an
  irreversible step.** Synthesis feasibility is discovered by the retro
  `route` job (cheap, repeatable), not modelled inline — but sequenced
  before any "order the synthesis" moment, like firmware-before-fab.
- **Write no nm skills until the corresponding slice ships** (a skill
  describing target state confidently misdirects agents).
