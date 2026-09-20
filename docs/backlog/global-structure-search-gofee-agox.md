---
status: ready
title: global structure search as a quest proposal engine — slice 1: AGOX/GOFEE surrogate search as a struct_search job on the in-process MLIP rung
prio: normal
model: opus
---

# global structure search as a quest proposal engine — slice 1: AGOX/GOFEE surrogate search as a `struct_search` job on the in-process MLIP rung

Source: Binuja's presentation (Reto, 2026-09-18). Papers requested the same
day, all stubs pinned to the fetch queue:

- Bisbo & Hammer 2020, PRL 124 086102 — GOFEE: global optimisation with a
  Gaussian-process surrogate of energies + forces learnt on the fly,
  two-length-scale kernel, lower-confidence-bound acquisition (`pa346935`).
- Bisbo & Hammer 2022, PRB 105 245404 — GOFEE refinements: population from
  clustering of DFT-evaluated low-energy structures; final exploitation as
  continued first-principles relaxation paths (`pa346936`).
- Christiansen, Rønne & Hammer 2022, JCP 157 054701 — AGOX: the modular
  Python framework (random search, basin hopping, GOFEE-style surrogate
  search) built on ASE (`pa346938`).
- Pickard 2019, PRB 99 054102 — hyperspatial optimisation: extra spatial
  dimensions created computationally so 3D traps become saddles
  (`pa346950`).
- Hammer group 2025, npj Comput. Mater. — extra degrees of freedom (chemical
  identity, degree of existence, 4–6D positions) inside the ML fingerprint;
  the GOFEE + hyperspatial merge (`pa346948`).

Reviewed 2026-09-18 (`ready`, Fable): the first draft rested on a relax
ladder that does not exist (`dft-fast`/`dft-tight`), on the wrong extra
(`[dft-ml]` is dormant; the GPU node installs `[catalyst-gpu]`), and on a
"same container as `ml`" premise (`ml` runs **in-process** on the node;
only `gpaw` goes to the `precis-dft` container). This version is corrected
to the code and split: slice 1 is this item; surrogate persistence +
variable composition, and hyperspatial generators, are follow-up items
`blocked-by` this one.

## Motivation / why

Today a quest's candidate `structure`s come from **LLM proposals** one at a
time (`quest/tick.py::max_proposals_per_tick` = 1 by operator decision
2026-08-08), each relaxed at one rung: `emt` (ours, in-process anywhere),
`ml` (MACE, in-process on the GPU node via
`workers/job_types/struct_relax.py::_default_ml_runner` →
`structure/relax.py::_ml_calculator`), or `gpaw` (the `precis-dft`
container). That is a human-shaped search: it finds what the proposer can
name (a dopant here, an adatom there) and is blind to reconstructions
nobody thought of. qu164903 (Pd(111) NO→NH3) and the nanobud junction work
are the regimes GOFEE was built for: supported clusters, surface
reconstructions, adsorbate overlayers, carbon clusters on Ir(111) are its
published demos.

GOFEE's economics match what we have. The surrogate (GPR on a fingerprint)
does the exploration; the oracle is called only on acquisition winners. Our
oracle is the same MACE calculator the `ml` rung already runs in-process on
the node, so a search is one long in-process job on the node, not 200
relax jobs. AGOX is ASE-native and ASE is a core dependency.

## In scope

1. **New job type `struct_search`** (`workers/job_types/struct_search.py`,
   registered like `struct_relax` in `workers/job_types/__init__.py` and
   `workers/registry.py`; pinned to the GPU node the same way). Params:
   `seed` (a `structure` handle: slab + cell, `fixed` atoms honoured as
   AGOX constraints), `box` (explicit confinement bounds, fractional
   coordinates in the seed's cell, `[[fx0,fx1],[fy0,fy1],[fz0,fz1]]`),
   `add` (composition to place inside the box, fixed stoichiometry, e.g.
   `{"Pd": 2, "N": 1, "O": 1}`), `model` (MACE model, default via
   `relax.route_ml_model`), `budget` (oracle single-point evaluations,
   default 200, hard cap 1000), `algo` (`gofee` default; `basin_hopping`
   and `random` as AGOX's cheaper baselines), `timeout_s` (own cap,
   default 2 h, under `struct_relax._RELAX_TIMEOUT_S_DEFAULT`).
2. **In-process runner** mirroring `_default_ml_runner`: seed POSCAR →
   ASE Atoms → AGOX environment (template = seed atoms with `fixed` mask,
   confinement = `box`, symbols = `add`) → AGOX search with the MACE
   calculator from `relax._ml_calculator(model)` as the oracle → AGOX's
   population (its `Database`) → the top-K distinct structures (K default
   10, `geom_hash_c`-distinct after `normalize_scene`). Swappable
   `SEARCH_RUNNER` hook for tests, like `ML_RUNNER`.
3. **Write-back**: each returned candidate becomes a `structure` row via
   the existing store put path (`normalize=True`), `derived-from` the
   seed, `meta.search = {algo, model, budget_used, iteration,
   oracle_energy_eV, surrogate_energy_eV, rank}`, tagged `search:agox`.
   The job row records the population summary (count, best energy,
   budget used, wall time) and AGOX's own database file as a job artefact
   under the job's NFS out-dir (so a later item can warm-start from it).
4. **Quest slot**: `quest/tick.py` gains a proposal source `search` that,
   when the quest's rubric/meta opts in (`meta.search = {box, add, ...}` on
   the quest), spends the one `max_proposals_per_tick` slot on a
   `struct_search` job instead of an LLM-authored structure;
   `struct_search` is added to `workers/job_types/quest_tick.py::_SIM_JOB_TYPES`
   so per-quest backpressure sees it. Returned candidates enter the
   frontier like any relaxed candidate (they carry an `ml` energy already).
   The proposer LLM sees them on the next tick as candidates to name and
   argue; it never sees the search internals.
5. **Extra + deploy**: new extra `struct-search = ["agox>=3.11,<4"]` in
   `pyproject.toml` (pinned without AGOX's optional extras; it pulls ray,
   dscribe, ase-ga, scikit-learn, h5py, matplotlib), appended to
   `deploy/roles/autocatpath/defaults/main.yml::autocatpath_extras` so the
   GPU node's worker venv gets it. Missing backend → `RelaxUnsupported`
   with an "install precis-mcp[struct-search]" message, classed infra by
   the job.
6. **Skills**: `precis-structure-help` gains a "search, don't guess"
   section (params, what comes back, the `search:agox` tag);
   `precis-quest-help` documents the opt-in and the slot rule.

## Explicitly NOT in scope

- Replacing the LLM proposer. The search proposes; the LLM still rules
  (names the candidate, argues it into the dossier). One slot, not the
  loop.
- Writing our own GPR/kernel/fingerprint code. AGOX owns the surrogate.
- A DFT oracle. There is no in-process DFT; `gpaw` is container-only and a
  GOFEE oracle call is a single-point evaluation, not a `gpaw-relax`.
  Re-ranking the returned population at `gpaw` is the existing
  per-candidate `struct_relax` path, invoked by the quest as today.
- Relax-cache participation. `structure/cache.py` keys a *relax run*
  (structure sha + fidelity + model + params); the handler and
  `struct_relax`'s write-back own lookup/write. AGOX's single-point
  oracle calls do not go through it; AGOX's own database is the memo.
- Variable composition (`add` as ranges), surrogate warm-start across
  searches, and the hyperspatial / degree-of-existence generators —
  follow-up items `global-structure-search-slice-2-composition.md` and
  `global-structure-search-slice-3-hyperspatial.md`, both blocked-by this.
- Free (non-periodic) clusters: `canonical.inplane_symmetry_ops` is
  identity-only when the cell is not periodic in both in-plane axes, so
  free-cluster twins would not dedup. Slab/overlayer seeds only.
- A region/box Measure kind on the seed structure. Bounds ride on the job
  (`box`), decided here; a persisted region kind is a later item if the
  LLM turns out to need to read it.
- Bulk crystal structure prediction (cell-variable AIRSS-style search).

## Acceptance criteria

- A test builds a fresh Pd(111) 3×3×4 slab via the `slab` op (bottom two
  layers `fixed`), stubs `SEARCH_RUNNER` with a canned AGOX-shaped
  population of 12 geometries (two of them translation twins), and asserts:
  10 `structure` rows land (twins collapsed by `geom_hash_c`), each passes
  `validate`, each carries `derived-from` the seed, `meta.search` with the
  fields above, and the `search:agox` tag; the job row carries the
  population summary.
- The real runner, exercised once on the GPU node (manual, logged in the
  item's decisions log with the job handle), returns ≥ 10 distinct
  candidates for `add={"Pd":2,"N":1,"O":1}` on that seed within
  `budget=200`, at least one with `oracle_energy_eV` below the seed's own
  relaxed `ml` energy plus the isolated-adsorbate reference (i.e. the
  search found a binding configuration), inside `timeout_s`.
- `struct_search` appears in `_SIM_JOB_TYPES`; a quest with
  `meta.search` set dispatches exactly one `struct_search` job per tick
  under `max_proposals_per_tick`, and a quest without it dispatches none
  (existing tick tests unchanged).
- Without `[struct-search]` installed, dispatching the job fails fast with
  an infra-classed event naming the extra; `scripts/test` (which lacks
  AGOX) runs the whole suite green via the stub.
- `uv lock` resolves the new extra without moving `mace-torch`/`torch`
  pins; `[all]` does not include it.

## Target + blast radius

- `src/precis/workers/job_types/struct_search.py` (new) +
  `workers/job_types/__init__.py` loader + `workers/registry.py` spec row
  + `workers/job_types/quest_tick.py::_SIM_JOB_TYPES`.
- `src/precis/structure/search.py` (new, pure): seed Atoms + box + add →
  AGOX environment/generators/config; AGOX population → scenes (uses
  `precis_pathway.ingest.scene_from_ase`'s logic — copy the small
  ASE→Scene helper into `structure/` rather than importing the
  `[catalyst]` package). `relax.py` untouched (`_ml_calculator` is the
  factory).
- `src/precis/quest/tick.py` proposal source; `quest/frontier.py` reads
  `meta.search` for the origin column.
- `pyproject.toml` extra; `deploy/roles/autocatpath/defaults/main.yml`
  extras list.
- Skills `precis-structure-help.md`, `precis-quest-help.md`.
- Tests: `tests/test_struct_search.py` (runner stub + write-back + quest
  slot), extend `tests/test_job_types_registry*.py` for the new row.

## Open questions / decisions log

- DECIDED: oracle = `ml` only, in-process, `_ml_calculator`; no DFT oracle.
- DECIDED: extra = new `[struct-search]`, deployed via `autocatpath_extras`,
  not `[dft-ml]` and not folded into `[catalyst-gpu]` (keeps ray/dscribe
  out of the pathway compute stack's own resolve).
- DECIDED: box = explicit fractional bounds on the job; no region kind.
- DECIDED: job type, not a `structure` op (`ops.py` is one-Scene-in
  one-Scene-out; a population fits the job/write-back shape).
- DECIDED: licence — repo is `GPL-3.0-or-later`, AGOX is `GPL-3.0-only`;
  a dependency, compatible.
- DECIDED (build, 2026-09-18): the whole seed slab is frozen during the
  search (AGOX `fix_template=True`); only the `add` atoms move. Surface
  relaxation is the per-candidate `struct_relax` that follows, not the
  search's job.
- DECIDED (build): AGOX driven via the 3.11 config API (`ProblemConfig` +
  `RunConfig` + `<Algorithm>.create(...)`), not the hand-wired module script;
  a deadline `Observer` sets the run's convergence flag to stop on
  `timeout_s` without raising. `agox.algorithms.api.create_search(kind=…)`
  calls a `create_by_kind` that does not exist in 3.11.1 — avoided.
- OPEN (non-blocking, slice 2): AGOX `Database` save/load as warm-start —
  verify the API before speccing surrogate persistence.
- DECIDED (2026-09-19, first cluster run): `ray` is NOT idle — GOFEE's
  default collector/relaxer are ray-pool actors that import agox in fresh
  worker processes. AGOX 3.11.1 is broken against the fleet's pins (ase
  3.29 hid `IndexedConstraint`/`slice2enlist`; numpy 2.5 rejects
  `float()` of the `(1,)` log-marginal-likelihood), so `search.py` starts
  ray itself with `worker_process_setup_hook=_agox_compat_shims` before
  `create()`. Version bounds on the extra cannot fix it: the GPU venv
  installs against the uv.lock-derived constraints file. The first
  acceptance job (356752) died on this at import; an EMT smoke of the
  shimmed module on pollux then ran GOFEE end-to-end (12 candidates,
  16.8 s wall, budget 6).
- OPEN (non-blocking): `budget_used` was 12 for `budget=6` on that smoke —
  GOFEE spends two oracle evaluations per iteration, so the cap is 2× the
  request. Decide whether `budget` should count iterations or evaluations.
- OPEN (non-blocking): wall-clock — a 200-call GOFEE search with MACE on
  a 3×3×4 slab + 4 adatoms is expected in minutes, not hours; the 2 h cap
  is a guard, not a budget. Measure on the first real run and record here.
