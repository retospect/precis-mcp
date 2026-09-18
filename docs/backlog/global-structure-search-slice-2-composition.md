---
status: draft
title: global structure search — slice 2: variable composition (`add` as ranges) + surrogate warm-start from a prior search's AGOX database
prio: normal
blocked-by: global-structure-search-gofee-agox
---

# global structure search — slice 2: variable composition (`add` as ranges) + surrogate warm-start from a prior search's AGOX database

Building on slice 1's fixed-stoichiometry search, this slice extends the `struct_search` job type to accept compositional ranges and to reuse a prior search's trained surrogate as a warm-start initialization.

## Motivation / why

Slice 1 searches a fixed stoichiometry (e.g., `{"Pd": 2, "N": 1, "O": 1}`). Many real exploration scenarios need flexibility: is there a sweet spot at Pd₂NO₃ or Pd₃NO₂? Variable composition (per-element min/max ranges) lets the search explore the stoichiometry landscape, discovering stable phases nobody hypothesized.

A surrogate (GPR fingerprint) trained on one problem's AGOX database can accelerate a similar search: warm-starting from a prior search's population, rather than starting from scratch, cuts acquisition steps and wall time.

## In scope

1. **Variable composition**: `add` param expands from a fixed dict to ranges, e.g. `{"Pd": [1, 3], "N": [0, 2], "O": [0, 2]}`. AGOX's `RandomGenerator` or `SamplingGenerator` constraint accepts symbolic ranges; we wire them in.
2. **Warm-start surrogate**: new job param `warm_start` = handle/job_id of a prior `struct_search` job whose AGOX `Database` artefact (saved in slice 1) seeds the surrogate initialization. AGOX's `Database.load` method is called on the prior job's artefact; the loaded population primes the GPR.
3. **Write-back**: results carry `meta.search.warm_start_job` (the prior handle) and `warm_start_population_size` (rows seeded from the database).
4. **Acceptance criteria** for the warm-start path: a test runs two sequential searches on the same seed/box; the second names the first as `warm_start`; both complete within budget; the second's initial population includes at least one candidate from the first.

## Explicitly NOT in scope

- Constraint solving beyond AGOX's built-in `RandomGenerator`/`SamplingGenerator`; we don't build a custom generator.
- Hyper-parameter tuning for the surrogate model.
- Hyperspatial generators or degree-of-existence degrees of freedom (slice 3).
- Evolutionary / genetic algorithm variants; AGOX remains the oracle source.

## Acceptance criteria

- A test builds a seed slab, runs a `struct_search` with `add={"Pd": [1, 3], "N": [0, 1]}` and `budget=50`, captures the job handle.
- A second search on the same seed uses `warm_start=<prior_handle>`, `add` with different ranges (e.g., `{"Pd": [2, 4], "O": [0, 1]}`), and `budget=100`; it completes and carries `meta.search.warm_start_job` + `warm_start_population_size`.
- Without a prior job, omitting `warm_start` behaves as slice 1 (no regression).
- `uv lock` resolves without moving any pinned dependency.

## Target + blast radius

- `src/precis/workers/job_types/struct_search.py` (extend from slice 1): handle `add` ranges, wire AGOX generator constraint.
- `src/precis/structure/search.py` (extend): add `warm_start` param, call `Database.load(artefact_path)` if present, seed surrogate.
- Tests: `tests/test_struct_search.py` (add two-job warm-start scenario).

## Open questions / decisions log

- OPEN (non-blocking): AGOX `Database` save/load as warm-start — verify the API before speccing. (See slice 1's open questions.)
