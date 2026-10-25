---
status: draft
title: NEB structure builder — author matched IS/FS endpoints on the structure kind, run one CI-NEB edge, read back a cited barrier
pillar: 3d-design
thread: chemistry
prio: high
---

# NEB structure builder (gr512126)

Status 2026-10-25: slices 1–2 shipped (slice 1 in 49cae7c94; slice 2 —
`view='pair'` — in this tree's commit). Slices 3–5 open.

Reto, 2026-10-25: build it, Opus spec first, and keep it a separate tool —
not merged with gr506762 (structured kinetics/thermochemistry lookup) or
gr515871 (inspectable NEB trust-gate diagnostic).

Bottom line: the NEB engine already exists (`autocatpath.neb.neb_barrier`,
run today only inside the `pathway` kind's fixed ammonia network). What
agents lack is (a) a way to author a final state that keeps the initial
state's atom identity, (b) the CH₄/CH₃ adsorbate groups, and (c) a door that
runs one NEB edge between two `structure` designs they built themselves.
Slice 1 fixes (a)+(b) with no compute; slice 3 adds (c).

## Motivation / why

qu509651 (heat budget: CO₂/CH₄ drawdown) has a DFT strand on methane C–H
activation over a bridging O on Cu(111): `CH4 + O* → CH3* + OH*`. The quest
has two initial-state candidates, st510794 (`Cu111_bridgingO_CH4_v2`) and
st511725 (`_v3`), and zero barriers. The tick executor filed gr512126 on
2026-10-24 and bumped it 21 times in 30 hours (comments 1–21, 06:30Z
10-24 to 11:58Z 10-25). What the comments say agents tried:

- **Hand-placed TS/FS atoms (comments 5, 7, 8, 13, 17, 18).** Five-plus
  proposals rejected: a Cu–H clash, a floating unbonded H, then a malformed
  slab spec rejected before geometry preflight, then op-validation
  rejections. "Hand-built TS/FS geometry is ruled out (fails structure
  preflight)."
- **The generic `slab` template (comments 10–12, 15).** "Both hand-placed
  atoms and the generic slab-template op have failed to produce a usable
  structure pair."
- **Static points do not give a barrier (comments 1–4, 6, 14).**
  "Two disconnected endpoint energies", not an activation energy.
- **Scope creep (comment 20):** "and now PdCu variants" — the same request
  for other surfaces.

What the store shows, read 2026-10-25:

- st510794 fails `validate`: `atom_overlap aCu20/aO1 0.75 Å` (floor 1.19).
- st511725 passes `validate` but is a `clean`-rung, unconverged relax
  (200 steps): no energy, no fixed atoms, the four Cu layers smeared in z,
  O coordination 0, C coordination 5, per-atom |F| up to 26 eV/Å. It is not
  a usable NEB endpoint either.
- `add_adsorbate` knows H·O·N·OH·H2O·NH·NH2·NH3 only
  (`structure/ops.py::ADSORBATES`), so CH₄ and CH₃ are built atom by atom.
- `derive` (branch a design with ops, keep labels, link `derived-from`)
  exists on the handler but only the web `/apply` route calls it. MCP has
  no such verb, yet four structure refusals print
  `next="derive(kind='structure', …)"`. So an agent's FS is a fresh `put`
  with its own atom order, which cannot be an NEB partner.
- The tick prompt that teaches the slab ops (`quest/tick.py::_reaction_context`)
  is only served to quests with `meta.reaction_config`. qu509651 has none,
  so it got the free-form hand-built-cell rules.

The quest's sightings name two other causes, both outside this item: the
undocumented v1–v3 candidate schema (body) and the lack of numeric kinetics
lookup, which is gr506762.

## What exists vs what is new

| piece | status | where |
|---|---|---|
| fcc(111) slab, ASE-exact atom order, `fix_layers` | exists | `structure/ops.py::_op_slab` |
| site-named placement (top/bridge/hollow) | exists | `_resolve_site`, `add_atom_site`, `add_adsorbate` |
| CH₄ · CH₃ · CH₂ · CH · C groups | **new** (catalogue rows) | `ADSORBATES` |
| move one atom onto an acceptor, rebond | **new** op `transfer` | `structure/ops.py` |
| move a whole fragment to a named site | **new** op `move_group` | `structure/ops.py` |
| branch a design, keep labels, `derived-from` | exists, web-only | `StructureHandler.derive` |
| that branch reachable over MCP | **new** `put(args={'derive_from'})` | `handlers/structure.py` |
| `validate` gate, tier-0 preflight | exists | `structure/validate.py`, `preflight.py` |
| `ml` relax on the GPU node, run-cube | exists | `struct_relax`, `struct_runs` |
| endpoint pair check (same atoms, both relaxed, distinct basins) | **new** `view='pair'` | `handlers/structure.py` |
| CI-NEB, k-start, retries, tether, collapse/desorb counters | exists | `autocatpath.neb.neb_barrier` |
| endpoint permutation + per-fragment MIC alignment | exists | `autocatpath.neb.align_endpoints` |
| saddle + band grading | exists | `autocatpath.trust.grade_saddle`, `grade_band` |
| one NEB edge between two designs, as a job | **new** `struct_neb` | `workers/job_types/` |
| NEB run + band images storage | **new** tables | migration 0248 |
| TS geometry as a citable design | **new** (derived design) | `handlers/structure.py` |
| barrier lifted onto a quest candidate | exists for `autocatpath_*` jobs, **new** lane for `struct_neb` | `quest/compute.py::harvest_measures` |
| tick prompt teaching the door to non-reaction quests | **new** | `quest/tick.py` |

autocatpath's `pathway` kind stays as is. It builds its own endpoints from
a fixed network template (`network: ammonia`, the only template in
`autocatpath/network.py`), so CH₄ activation is not expressible there
without an engine change. This item calls the engine's pure NEB function
on endpoints the agent authored, which needs no engine change.

## MCP surface

All on `kind='structure'`. No new kind, no new verb.

**Adsorbates (slice 1).** `add_adsorbate` gains `species` values
`CH4|CH3|CH2|CH|C`. CH₄ binds through C, sits one H pointing down, at a
physisorption default height; CH₃ binds through C, H's up. Starting guesses,
like the existing rows.

**Ops (slice 1).**

- `{"op":"transfer","atom":"aH3","to":"aO1","distance?":<Å>}` moves `atom`
  onto `to` along `to`'s open coordination direction at the covalent-radius
  sum (or `distance`), drops `atom`'s declared bonds, adds `atom`–`to`.
  `{"op":"transfer","from":"aC1","to":"aO1"}` picks the atom bonded to
  `from` that is nearest `to`; the ack names it (rule 9: one reading).
  Refusals: unknown label → `OpError` listing the nearest labels;
  `from` with two equidistant candidates (within 0.05 Å) → refuse with both
  labels as `options=`.
- `{"op":"move_group","atom":"aC1","site":{"type":"top","anchors":["aCu32"]},"height?":<Å>}`
  rigidly moves the fragment containing `atom` so `atom` sits on the site.
  Refusal if the fragment contains a fixed atom or a slab atom.

**Derive (slice 1).** `put(kind='structure', id='<new-slug>',
args={'derive_from': '<parent>', 'ops': [...]})` calls
`StructureHandler.derive`. Ack: `derived st<N> '<slug>' from st<M>: 2 ops ·
42 atoms, labels preserved (NEB-pairable with st<M>)`. If the ops changed
the atom set, the ack says so: `atom set changed (+0/−1): not
NEB-pairable with st<M>`. Refusals: slug exists → `next=` the same call
with `'<slug>-2'`; a `relax` op → `next=` the edit that relaxes the new
design. The four stale `next="derive(kind='structure', …)"` hints are
rewritten to this call.

**Pair check (slice 2, zero compute).**
`get(kind='structure', id='<is>', view='pair', args={'other': '<fs>'})`
returns one row per gate (§Validation): `check · pass|fail|warn ·
measured · next`. Every failed row carries a working `next=`, for example
`edit(kind='structure', id='<fs>', ops=[{'op':'relax','fidelity':'ml','steps':300}])`.

**Run (slice 3).** `put(kind='structure', id='<is>', mode='neb',
args={'final': '<fs>', 'images': 7, 'path_seeds': 2, 'model': 'mace_mp',
'fmax': 0.05})`. Runs the pair check first; any failing gate refuses with
that row's `next=`, and nothing is minted. On pass: `queued struct_neb
jo<N> st<is> → st<fs> · ml/mace_mp · 7 images × 2 seeds · wall cap 2 h ·
poll get(kind='structure', id='<is>', view='neb')`. An identical request
(same endpoint shas, params, code version) is a cache hit and returns the
stored run without minting a job. Over the quest's daily cap (§Compute)
it refuses, names the cap and when the window reopens.
`requested_by=<todo>` works as it does on `relax`.

**Read (slice 3).** `get(kind='structure', id='<is>'|'<fs>'|'<ts>',
view='neb')` lists NEB runs touching the design. `args={'run': N}` returns
the band: image · E−E_IS (eV) · max |F| · the distances of the bonds that
change IS→FS. It also gives E_a forward/reverse, ΔE, the TS handle and the
trust verdict, each check on its own row. A failed run shows its reason
and a `next=` (denser band, more seeds, or fix the endpoint).

## Data model

Endpoints are ordinary `structure` designs. The FS is derived from the IS,
so both share the same labels, elements, cell and fixed set. Pairing is
checked by label, never by array index.

Migration 0248 (forward-only) adds two tables. NEB rows stay out of
`struct_runs` because every relax reader there treats a row's `energy` as a
relaxed minimum, including `harvest_measures` and the cache-first relax
lookup. An E_TS in that column would read as a relax result.

`struct_neb_runs`: `id` · `is_ref_id`, `fs_ref_id`, `ts_ref_id` (FK refs,
`ts` NULL until a band exists) · `is_run_id`, `fs_run_id` (FK struct_runs:
the endpoint relaxes the band started from) · `fidelity`, `model` ·
`params` jsonb (images, path_seeds, fmax, max_steps, retries, tether,
optimizer) · `status` (`queued|running|succeeded|failed`) · `converged` ·
`barrier_fwd_ev`, `barrier_rev_ev`, `delta_e_ev` (NULL when not priced;
never NaN) · `ts_index`, `ts_fmax` · `n_imag`, `imag_cm1` (slice 5) ·
`trust` jsonb (one record per check, the `autocatpath.trust` record shape)
· `trusted` boolean · `multistart` jsonb · `cache_key` (sha256 of IS sha,
FS sha, fidelity, model, params, code version) · `wall_s` · `job_ref_id` ·
`created_at`. Unique on `cache_key` where `status = 'succeeded'`.

`struct_neb_images`: `run_id` FK · `idx` (0 = IS … n+1 = FS) · `energy_ev`
· `max_force` · `positions` jsonb, canonical-rank-indexed like
`struct_runs.final_geometry`. PK (`run_id`, `idx`).

The TS image becomes a design, `<is-slug>-ts-r<run>`, via the same derive
path (labels preserved), linked `derived-from` the IS. Its TOC header
names the run, E_a and the trust verdict, so `st<ts>` is the handle a quest
cites. No new relation type: the run row holds the IS/FS/TS triple.

Quest candidate meta gains nothing new. Slice 4 writes the existing
`barrier`, `barrier_trusted` and the warnings list, in the same keys the
autocatpath lane writes, plus `barrier_source='struct_neb'` and the run id.
The frontier reads it unchanged.

## Compute placement

- **Job:** new core job type `struct_neb`
  (`src/precis/workers/job_types/struct_neb.py`), beside `struct_relax`.
  Executor `ssh_node`, the same GPU-node claim gate and `target_node`
  pinning. The body runs in process on the node, like `struct_relax`'s
  `ml`/`xtb` path: the calculator comes from
  `precis.structure.relax._ml_calculator`, and `autocatpath.neb` /
  `autocatpath.trust` are imported lazily (the `[catalyst]` extra is
  already on the GPU nodes for the pathway seeds). Core does not import
  `precis_pathway`.
- **Atom order:** autocatpath assumes slab atoms come first (`index <
  n_slab`). The job builds both ASE `Atoms` from labels in one order, slab
  atoms first, then adsorbates, and maps results back through labels. It
  never uses POSCAR element grouping. The slab is the largest connected
  metal-atom component of the IS (inferred contacts), so a Cu dopant in Pd
  counts as slab. `preflight._slab_adsorbate_indices` uses the
  dominant-element heuristic instead, and its own TODO says a doped slab
  breaks it. comment 20's PdCu variants are exactly that case.
- **Rung:** `ml` only (MACE-MP by default, `route_ml_model` rules). No
  GPAW: no GPAW relax has ever completed in prod (chemistry thread). Serial
  `NEB`, no `BatchedNEB` pooling, which avoids the GB10 unified-memory OOM
  (gr491506) by construction.
- **Defaults:** 7 interior images, `path_seeds=2`, `fmax=0.05`,
  `max_steps=300`, `retries=1`, the autocatpath tether on, IDPP
  interpolation after `align_endpoints`.
- **Wall:** `PRECIS_STRUCT_NEB_TIMEOUT_S`, default 7200 s, stamped as
  `resources.wall_seconds`. The ssh_node lease is then `max(7200, wall +
  margin)`. On timeout the job self-aborts the way `struct_relax` does and
  stamps `failure_class='timeout'`, and the run row is `failed`, not
  priced.
- **Cost:** own hardware, $0 of API. Estimate only, to be measured in slice
  3: one 42-atom edge × 7 images × 2 seeds × ≤300 steps is ≤4,200 image
  evaluations plus a retry, so minutes to tens of minutes of one GPU. For
  scale, a whole 3×3×4 ammonia network (dozens of edges) takes 15–20 min
  uncontended (`quest/compute.py::_autocatpath_wall_seconds`).
- **Caps:** one in-flight run per `cache_key` (idempotent mint), and at
  most 4 `struct_neb` mints per quest per rolling 24 h
  (`PRECIS_STRUCT_NEB_QUEST_DAILY`). The cap counts jobs whose parent todo
  serves the quest. Non-quest calls are capped only by the key.

## Validation

**Pair gates** (`view='pair'`, and run before any `mode='neb'` mint). A
fail refuses; a warn is shown in the ack and does not block.

| gate | fail when | next= |
|---|---|---|
| same atoms | label sets, element per label, or charge differ | derive the FS from the IS (`derive_from`) |
| same cell | lattices differ by >1e-6 Å or pbc differs | derive instead of re-`put` |
| same fixed set | `fixed` flags differ per label | `constrain` op on the FS |
| slab anchored | no fixed atom in the slab | `constrain` the bottom layers (`slab` with `fix_layers: 2`) |
| valid | `validate` error on either design | the validator's own `fix` text |
| relaxed | no converged `ml` run with the same model on the design's current version | the `relax` edit for that design |
| distinct basins | max adsorbate displacement after `align_endpoints` < 0.3 Å, or equal `trust.fingerprint` | change the FS |
| one elementary step | warn when the inferred-bond diff IS→FS has 0 changes (fail) or >2 changes (warn) | split into two steps |
| no cross-cell drag | warn when any adsorbate atom moves >5 Å after alignment | check the FS placement |

**Run trust** (computed in the job and stored per check in `trust`):
`trusted` is true only when every check passes:

- `converged` on the selected start;
- `trust.grade_saddle` is `pass`: TS not at an endpoint, negative
  curvature, climbing residual ≤ 2·fmax, barrier ≥ 0.02 eV;
- `trust.grade_band` is `pass`: no mid-band detachment;
- `collapsed_images == 0`. A collapsed band stores NULL barriers and the
  run fails as "not priced";
- `multistart.n_valid ≥ 2` and the valid starts' barriers agree within
  0.05 eV (`n_at_min ≥ 2`);
- |E_a| ≤ the quest's existing absurd-barrier bound
  (`quest/compute.py::_BARRIER_ABSURD_EV`);
- slice 5 adds `n_imag == 1`: a finite-difference Hessian over the
  adsorbate atoms of the TS image (ASE `Vibrations`, ~6·n_ads force calls)
  has exactly one imaginary mode, above 50i cm⁻¹.

`marginal` grades keep the barrier, set `trusted=false`, and list the
reason, so the barrier ranks as provisional and never as confirmed. That
matches the frontier's existing trusted/provisional split.

## Slices

Each slice ships alone. Order: slice 1 unblocks qu509651's endpoints today
with no GPU, and each later slice uses the one before.

1. **(S) Endpoint authoring.** CH₄/CH₃/CH₂/CH/C rows in `ADSORBATES`; the
   `transfer` and `move_group` ops; the `put(args={'derive_from'})` door;
   the four stale `derive(...)` hints rewritten; skill section "Reaction
   endpoints" in `precis-structure-help` with the Cu(111)/O/CH₄ worked
   example; `structure_propose._OP_VOCAB` and the tick op menu list the
   new ops. Useful alone: agents build a valid IS and a label-matched FS,
   relax both at `ml` with the existing path, and cite a reaction energy
   ΔE from two relaxed endpoints instead of failing preflight.
2. **(S) Pair check — shipped.** `view='pair'` with the gate table above,
   read-only and zero compute
   (`precis.structure.pair_check.evaluate_pair`). Useful alone: an agent
   learns why a pair is not NEB-ready, with a working `next=` per failed
   gate, before any NEB exists.
3. **(M) One NEB edge.** Migration 0248; the `struct_neb` job; `put(mode=
   'neb')` with gates, cache key and caps; `view='neb'` list and band; the
   TS design; run trust without the frequency check. Measure wall time
   and record it in the job type's docstring.
4. **(S) Quest lane.** A `harvest_measures` lane: a succeeded `struct_neb`
   whose IS is a quest candidate writes `barrier`, `barrier_trusted` and
   warnings onto the candidate's meta and logs a system `result` entry
   citing `st<ts>`. Failed runs are never ruled out, the same as the
   autocatpath lane. Generic (non-`reaction_config`) quests get a
   short "barriers: derive an FS, check the pair, run `mode='neb'`" block
   in the tick prompt. Close gr512126 when qu509651 has one harvested
   barrier, trusted or not.
5. **(S) Saddle frequency check.** The `n_imag`/`imag_cm1` columns filled,
   with the `n_imag == 1` gate added to `trusted`. Existing runs are not
   rewritten. They show `frequency: not checked` in the read.

## Acceptance criteria

**Slice 1.**
- A test builds Cu(111) `[3,3,4]`, `fix_layers: 2`, O on a bridge and CH₄
  on a neighbouring top site, using only `slab` + `add_adsorbate`. It
  derives the FS with `transfer` (`from: aC1, to: aO1`) + `move_group`
  (C to a top site). Both designs pass `validate` and `preflight` with
  zero errors, and have identical label sets, cell and fixed flags.
- The `derive_from` ack names the parent and says "labels preserved".
  With a `vacancy` in the ops it says "not NEB-pairable".
- `transfer` with two equidistant candidates refuses, both labels in
  `options=`. `move_group` on a fragment with a fixed atom refuses.
- `tests/test_mcp_raise_next_ratchet.py` stays green with no new hint-less
  raise. No refusal text in the tree contains `derive(kind=`.
- The same pair built on Pd(111) with one surface Cu (`set_element`)
  passes too, which covers comment 20's PdCu ask.

**Slice 2.**
- `view='pair'` on the slice-1 pair before relaxing reports `relaxed: fail`
  with both relax edits as `next=`. After stubbed converged `ml` runs, every
  gate passes. FS = IS reports `distinct basins: fail`. A separately
  `put` FS with shuffled labels reports `same atoms: fail`. st510794 vs
  itself reports `valid: fail` with the `atom_overlap` fix text.

**Slice 3.**
- With the GPU runner stubbed (like `struct_relax.ML_RUNNER`): `mode='neb'`
  on a passing pair mints one `struct_neb` job; a second identical call
  mints none; a failing pair mints none and returns that gate's `next=`;
  the fifth mint for one quest in 24 h refuses and names the cap.
- The job writes one `struct_neb_runs` row, n+2 `struct_neb_images` rows,
  and a TS design linked `derived-from` the IS. A collapsed band stores
  NULL barriers and `status='failed'`. Nothing lands in `struct_runs`.
- On prod, one real edge (the slice-1 Cu(111)/O/CH₄ pair at `ml`)
  completes inside the 2 h wall. `view='neb'` shows E_a, ΔE, the TS handle
  and every trust row. The measured wall time is recorded.
- A doped slab (Pd with a surface Cu) is split with Cu on the slab side
  (unit test on the slab-split helper).

**Slice 4.**
- A succeeded run on a qu509651 candidate puts `barrier` on the candidate.
  The frontier table shows it confirmed if trusted, provisional otherwise.
  The logbook gets one system `result` entry citing `st<ts>`. Re-harvest
  is idempotent.
- A tick prompt for a quest without `reaction_config` contains the NEB
  block. One with `reaction_config` is unchanged (snapshot test).

**Slice 5.**
- A stubbed Hessian with two imaginary modes sets `trusted=false`, reason
  `n_imag=2`. With one mode, the run stays trusted.

## Explicitly NOT in scope

- gr506762 (numeric kinetics/thermochemistry lookup from papers) and
  gr515871 (inspectable trust-gate diagnostic for pathway seeds). Reto
  ruled them separate.
- DFT/GPAW NEB, a DFT single point on the TS, ZPE or entropy corrections.
  `neb-barriers-in-the-catpath-pipeline.md` owns the GPAW lane.
- Automatic FS enumeration, TS search without an FS (dimer, growing
  string), minima hopping for endpoints. The agent authors the FS.
- Changes to the `pathway` kind or the autocatpath engine. A methane
  network template belongs in the catpath repo.
- Facets other than fcc(111) from the `slab` op. The new ops work on any
  design, including CIF imports.
- Auto-chaining endpoint relaxes into the NEB call (see Open questions).
- Repairing st510794/st511725. The quest rebuilds endpoints with slice 1.

## Target + blast radius

`structure/ops.py` (catalogue rows, two ops); `handlers/structure.py`
(`put` args `derive_from`/`final`/`images`/`path_seeds`/`fmax`/`model`,
`mode='neb'`, views `pair`/`neb`, the hint rewrite); new
`workers/job_types/struct_neb.py` + its registry entry; migration 0248;
`quest/compute.py::harvest_measures` (one lane); `quest/tick.py` (generic
prompt block); `workers/job_types/structure_propose.py` (op vocabulary);
skill `precis-structure-help`. The relax path, `struct_runs` and the
pathway kind are untouched. Post-deploy look: one real `mode='neb'` run,
`view='neb'` on it, and qu509651's next tick.

## Open questions / decisions log

1. **Does a trusted MACE barrier count as confirmed on a quest frontier?**
   Proposed: yes, labelled `ml/mace_mp`. The autocatpath lane already
   ranks trusted MACE barriers as confirmed, so a different rule here
   would split one frontier. (Not a blocker for slices 1–2.)
2. **New tables or `struct_runs` + a `run_kind` column?** Proposed: new
   tables (§Data model). A column needs a filter in every relax reader,
   and a missed reader silently mixes E_TS into relax energies.
3. **Unrelaxed endpoints: refuse, or chain the two relaxes into the NEB
   call?** Proposed: refuse with the relax edits as `next=`. Chaining
   needs a job-after-job dependency the queue has no primitive for.
4. **Free slab: refuse or warn?** Proposed: refuse. st511725 shows what a
   free 4-layer slab does under a relax.
5. **Daily cap:** 4 NEB mints per quest per 24 h. Proposed default. Raise
   it once slice 3 has measured the wall time.
6. **Placement:** core job type with a lazy autocatpath import, or a
   `precis_pathway` plugin job? Proposed: core, because the run record,
   the handler and the calculator factory are all core. (Decision for the
   builder unless Reto objects.)
