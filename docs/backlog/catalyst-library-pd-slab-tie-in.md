---
status: draft
title: Catalyst-library records tie to our Pd slab models through a typed reference quantity, a geometric match key and a bare-host calibration panel
pillar: quests
---

# Catalyst-library tie-in for the Pd slab models

Reto, 2026-10-05: "a spec on how to tie in catalyst libraries for our Pd
slab models and related things". `chem-database-tie-ins.md` holds the
source choices and the four rulings of 2026-10-02 (Catalysis-Hub first,
look up on demand, show and flag only, no calibration offset). This item
is the mechanism those rulings need: which number of ours is compared with
which number of theirs, and when the two describe the same surface.

## Motivation / why

Our Pd model is one slab recipe: fcc(111), 3×3×4, 10 Å vacuum, two fixed
layers, MACE medium (`quest/catalyst_seed.py::REACTION_CONFIG`). Most of
qu164903's candidates are that slab plus a dopant or adatom. A library
record is a different cell, layer count, lattice constant and functional,
and it reports a reaction energy against its own gas references. Three
things in the code stop a comparison today:

1. **One field holds two quantities.**
   `structure/importers/catalysis_hub.py::adapter` writes `reactionEnergy`
   into `ExternalRun.energy` when the record has one and the DFT total
   energy otherwise. Nothing records which it is, nor the reaction it
   belongs to. `cathub_db.read_cathub_db` imports only the product
   adsorbate system, so the clean slab and gas systems that define the
   reaction energy are not kept. `_flatten` fetches `activationEnergy` and
   the adapter drops it.
2. **The method key contains the record.**
   `handlers/structure.py::_method_key` hashes the whole `method` dict of
   an external run. That dict also carries `facet`,
   `surface_composition`, `reactants` and `products`, so two records from
   the same paper and functional never count as the same method.
3. **Computed against external is refused outright.**
   `guard_energy_comparable` raises on any computed/external pair. That is
   correct for a ΔE. The show-and-flag hook needs a second, labelled path
   that puts the two numbers side by side without subtracting total
   energies.

There is also no rule for what "the same surface" means. Without one the
hook either never fires (exact structural identity is never met) or fires
on a 2×2 RPBE record against a 3×3 doped MACE slab.

Why the functional matters: Catalysis-Hub tracks the DFT code and
functional per dataset so that like settings can be combined (pa2604), and
measured against the 39 experimental reaction energies of the CE39 set,
PW91 and PBE overestimate chemisorption for strongly bound adsorbates
(pa688). MACE-MP-0 is trained on PBE data. A gap between our number and an
RPBE or BEEF-vdW record therefore mixes the functional difference with any
MLIP error.

## In scope

### 1. A typed reference on every imported record

`ExternalRun` gains a `reference` payload beside the total energy:

```
quantity   adsorption_energy | reaction_energy | activation_energy
value_eV   float
equation   reactants → products, each with its state (gas | star) and
           stoichiometry, as the source wrote it
members    the config_ids of the systems the value was computed from
           (clean slab, adsorbed state, gas molecules, transition state)
```

- `ExternalRun.energy` is always the DFT total energy of that system. A
  record with no total energy is skipped and counted in `ImportSummary`.
- `cathub_db.read_cathub_db` imports the clean slab and gas systems of a
  reaction as well, so `members` resolves inside our store.
- An `activationEnergy` becomes its own reference with
  `quantity = activation_energy`.
- `method` keeps only method fields: `functional`, `code`, `cutoff_eV`,
  `kmesh`, `spin`, `dataset_doi`. The descriptors (`facet`,
  `surface_composition`, `reactants`, `products`) move to a `descriptor`
  payload. `_method_key` then compares methods.
- The reference is stored as a `measures` row on the imported structure
  ref (Reto, 2026-10-05), so slice A's storage waits on
  `measures-substrate.md`.
- When a source gives no cutoff or k-mesh, the method key is (functional,
  code, dataset_doi), so two papers never merge by accident (Reto,
  2026-10-05).

### 2. One match key, derived from geometry for both sides

One function, `structure/importers/match.py::slab_key(scene, adsorbate)`,
runs on our scenes and on imported ones:

| Field | Derived from |
|---|---|
| host | element counts of the slab atoms, per layer for the top two layers |
| facet | the source label for an import, the `slab` op for ours; a record whose surface-atom coordination contradicts its label is rejected |
| adsorbate | formula of the non-slab atoms (the `equation` names it for an import) |
| site | coordination of the binding atom to surface atoms: top, bridge, fcc, hcp |
| coverage | adsorbates per surface atom |

The key decides which of our models a reference is *relevant to*. It does
not make two numbers comparable; §3 does that. Grades:

- **exact:** host, facet, adsorbate, site and coverage equal.
- **near:** same host, facet and adsorbate; site or coverage differs.
- **none:** anything else.

A near match is shown with the differing field named.

The slab/adsorbate split needs `n_slab` on our scenes
(`slab-modelling-knobs.md` § provenance). Until that lands the split falls
back to the adsorbate formula, which is enough for the bare-host panel.

### 3. Normalise by running our model on the library's own structures

Coverage, cell size, layer count and site all change a binding energy for
physical reasons. Correcting for each separately would need a model per
effect. The general normalisation (Reto, 2026-10-05) is to remove them by
construction: for each reference, our (backend, model) re-relaxes the
reference's own member systems, and the `equation` is evaluated on those.

- Each member (clean slab, adsorbed state, gas molecule, transition state
  endpoint) is copied by `derive` (§6), its in-plane lattice rescaled to
  the constant our model relaxes the host bulk to, the source's fixed
  atoms kept fixed, then relaxed with the ordinary `struct_relax` job.
- Gas molecules are relaxed once per (backend, model) and shared.
- Our value and the reference value then share host, facet, site, cell,
  coverage and layer count. What remains is the method difference, which
  is the quantity the panel exists to measure.
- `slab_key` is evaluated before and after our relax. If the adsorbate
  left the reference's site, the row is marked `site_moved` and reported
  as a finding; its energy gap is not used.
- Our value is reported twice, raw and with the gas and H* corrections of
  ruling `catalysis-selectivity-19`, each labelled.

The gap measured on the library's cell is then read as the model's error
for that adsorbate on that host and applied to our own slab recipe. That
step assumes the model's error does not depend on coverage. Slice A tests
it once: bare Pd(111) + NO relaxed at two cell sizes that both have
references.

Each comparison row carries: our value (raw, corrected), the reference
value, the reference's functional and DOI, the match grade, and a
functional class:

- **same family:** the reference functional is the one the MLIP was
  trained on (PBE for MACE-MP-0, RPBE for UMA's oc20 head).
- **cross-functional:** any other. Shown, never flags.

The row is produced by a new function beside `guard_energy_comparable`,
which keeps refusing raw ΔE across methods.

### 4. Bare-host calibration panel first, candidates second

Few library records match a doped candidate exactly, so the per-candidate
hook would stay silent on most of the frontier. The trust question for a
candidate is mostly a question about its host and its (backend, model).
So the first consumer is a panel:

- The bare host slab is its own `structure` ref, one per slab recipe
  (structures are content-addressed, so the recipe yields one ref). The
  panel's rows are `measures` on that ref. Every candidate carries
  `meta.host_ref` pointing at it. β-PdH is a second host ref.
- A reference that matches a candidate's own modified site attaches to
  the candidate's ref and replaces the host row for that adsorbate.
- Key of a panel row: (host ref, adsorbate, backend, model, engine
  version).
- Rows: every adsorbate of the quest's network that has a reference on
  the bare host (host, facet and adsorbate equal; any cell). For qu164903
  that is the NO→NH₃ network's intermediates plus H and the CO poison on
  Pd(111).
- Our side is the §3 re-relaxation of each reference's members; no new
  job type and no pathway run.
- A candidate links to its host's panel. Its leaderboard row shows the
  panel's worst same-family gap and how many of its own network's
  adsorbates the panel covers.
- The panel is the precis half of catpath's calibration-panel ask
  (`catpath/HANDOFF-post-trust-deploy.md` item 4) and the measured input
  for the trust-demo paper (td459589).

The per-candidate comparison (ruling chemistry-5) uses the same row type
when a library record has the candidate's own host (alloy and hydride
surfaces the libraries do contain).

### 5. The distrust flag

No fixed threshold: catpath's handoff names about 0.2 eV, which has no
derivation. The flag uses two measured quantities instead (Reto,
2026-10-05):

- **Reference noise floor.** The spread among independent same-family
  references for one match key. A gap below the disagreement between two
  published calculations says nothing about our model.
- **Decision relevance (margin-flip size).** A selectivity margin is, at
  the candidate's worst branch point, the energy by which the step toward
  the target beats the best competing step. An error δ in adsorbate X's
  binding energy moves the margin by c·δ, where c is X's net coefficient
  across the two steps (+1 or −1 if X is on one side only, 0 if it is on
  neither or cancels). The flip size is |margin| / |c|: the smallest
  error in X that reverses the verdict. With c = 0 the adsorbate cannot
  flip that margin and never flags the candidate.

A same-family gap that exceeds the noise floor and is large enough to
flip a margin sets `reference_gap` in the flags of every candidate on
that host, read through the same path as `barrier_wrong_site`
(`quest/frontier.py`). Rank and Pareto membership do not change. Slice A
measures both quantities and sets no flag. Where a key has a single
reference, the noise floor is the median over the panel's other keys and
the row says so.

### 6. Seeds from a library slab

An external structure stays read-only. `derive` copies it into a new
editable structure: geometry copied, in-plane lattice rescaled to the
constant our MLIP relaxes the host to, bottom layers fixed per the quest's
slab recipe, `meta.seeded_from` set to the source ref, then relaxed before
any use. Limited to fcc(111) hosts until the engine takes other facets
(`catalyst-discovery-quest.md` Slice 4b).

### 7. Related Pd models

- **β-PdH (`pd-hydride-substrate.md`).** The hydride slab is its own host
  in the match key (the H sublattice is part of `host`), so it gets its
  own panel. Library H absorption and subsurface-H records are references
  for that item's Stage 0 numbers.
- **Materials Project.** Bulk PdHx formation energies and lattice
  constants are `reaction_energy` references on a bulk host. The adapter
  shares the `PRECIS_MP_API_KEY` path that `pourbaix_bulk` already uses.
- **Barriers.** An `activation_energy` reference is compared with a
  trusted NEB barrier on the same elementary step only
  (`meta.barrier_trusted`).

## Build order

- **A.** Typed reference + method/descriptor split + clean-slab and gas
  import (§1), `slab_key` (§2), `derive` (§6), the re-relaxation and
  comparison row (§3), the panel for bare Pd(111) fed from the local
  cathub `.db` files with its noise floor and the two-cell check (§4).
  No credentials needed.
- **B.** Panel shown on the leaderboard; the `reference_gap` flag (§5).
- **C.** Per-candidate comparison; the Catalysis-Hub credential path when
  the SUNCAT key arrives (`chem-database-tie-ins.md` build step 3).
- **D.** Library slabs as quest seeds (§6); hydride panel and Materials
  Project references (§7).

## Explicitly NOT in scope

- Source choice, storage policy and MOFs: decided in
  `chem-database-tie-ins.md`.
- A calibration offset or any change to ranking (ruling chemistry-5).
- MLIP fine-tuning on imported data.
- Non-fcc(111) slab building, solvation, potential-dependent references
  (BEAST DB's grand-canonical numbers need the U/pH window objective
  first).
- The DFT verify tier of catpath's item 4.
- Experimental references read from the corpus (CE39, pa688). They belong
  to `measures-substrate.md`; the comparison row accepts them later as a
  third functional class.

## Acceptance criteria

- Importing a recorded Catalysis-Hub reaction (no network) yields the
  adsorbed, clean-slab and gas systems, each with a total energy, and one
  reference whose `members` resolve to those refs.
- Two records of one dataset and functional with different reactions have
  equal `_method_key`; two functionals do not.
- `slab_key` returns the same key for our 3×3×4 Pd(111)+NO scene and for a
  recorded 3×3 Pd(111)+NO import, grades a 2×2 record `near` and a Pd₃Cu
  record `none`.
- For one recorded reference, the re-relaxed members have the source's
  cell shape, atom count and fixed atoms, and the row's two values come
  from the same equation.
- A re-relaxation in which the adsorbate changes site yields a
  `site_moved` row with no gap.
- `get` on the Pd(111) panel lists, per adsorbate, our raw and corrected
  value, the reference, its functional and DOI, the functional class and
  the noise floor. A cross-functional row shows no flag at any gap.
- The panel reports the two-cell check for NO: the gap at each cell and
  their difference.
- `guard_energy_comparable` still raises on a computed/external pair.
- A candidate whose host panel has a same-family gap above the noise
  floor and above its margin-flip size shows `reference_gap` in its flags
  and keeps its rank.
- A derived seed is editable, carries `meta.seeded_from`, and its source
  still refuses edit.

## Target + blast radius

- `src/precis/structure/importers/` (`__init__.py`, `catalysis_hub.py`,
  `cathub_db.py`, new `match.py`).
- `src/precis/store/_structure_ops.py::structure_import`.
- `src/precis/handlers/structure.py` (`_method_key`, the comparison view,
  `derive`).
- `src/precis/quest/frontier.py`, `quest/compute.py` (panel link, flag).
- `src/precis_pathway/` only as a reader of an existing result.
- Already-imported external rows change shape on re-import; the import is
  idempotent on `(dataset, config_id)`.

## Open questions / decisions log

Decided, Reto 2026-10-05:

- References are stored as `measures` rows (§1).
- Method key without cutoff and k-mesh is (functional, code,
  dataset_doi) (§1).
- Coverage and the other cell effects are normalised by re-relaxing the
  library's own structures with our model (§3); equal coverage is
  required for an `exact` grade (§2).
- No fixed 0.2 eV threshold; the flag uses the reference noise floor and
  decision relevance (§5).

- A library slab is rescaled to our model's lattice constant before the
  re-relaxation (§3). The bulk-relax cache this needs is the third
  section of `slab-modelling-knobs.md` and is built with slice A.

Open:

1. **Error transfer to doped candidates.** The host panel's gap is
   applied to every candidate on that host unless the candidate has its
   own reference (§4). How far a dopant changes the model's error at an
   adjacent site is not measured by this item.
2. **Does a pathway result name the worst branch point's states?** The
   margin-flip size (§5) needs the two competing steps and their
   intermediates, not only the scalar margin. To be verified against the
   catpath result before slice B; owner is the catalysis-selectivity
   thread.
3. **Thread.** `threads/chemistry.md` Do-next 5 owns
   `chem-database-tie-ins.md`; this item is listed beside it. The quest
   flag half touches `roadmap-quest`'s frontier code.
