---
status: draft
prio: normal
---

# Global structure search slices

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## global structure search — slice 2: variable composition (`add` as ranges) + surrogate warm-start from a prior search's AGOX database

_Grouped 2026-09-26; was `global-structure-search-slice-2-composition`, status draft, prio normal._

Building on slice 1's fixed-stoichiometry search, this slice extends the `struct_search` job type to accept compositional ranges and to reuse a prior search's trained surrogate as a warm-start initialization.

### Motivation / why

Slice 1 (shipped 2026-09-20, `structure/search.py`) searches a fixed stoichiometry (e.g., `{"Pd": 2, "N": 1, "O": 1}`). Many real exploration scenarios need flexibility: is there a sweet spot at Pd₂NO₃ or Pd₃NO₂? Variable composition (per-element min/max ranges) lets the search explore the stoichiometry landscape, discovering stable phases nobody hypothesized.

A surrogate (GPR fingerprint) trained on one problem's AGOX database can accelerate a similar search: warm-starting from a prior search's population, rather than starting from scratch, cuts acquisition steps and wall time.

### In scope

1. **Variable composition**: `add` param expands from a fixed dict to ranges, e.g. `{"Pd": [1, 3], "N": [0, 2], "O": [0, 2]}`. AGOX's `RandomGenerator` or `SamplingGenerator` constraint accepts symbolic ranges; we wire them in.
2. **Warm-start surrogate**: new job param `warm_start` = handle/job_id of a prior `struct_search` job whose AGOX `Database` artefact (saved in slice 1) seeds the surrogate initialization. AGOX's `Database.load` method is called on the prior job's artefact; the loaded population primes the GPR.
3. **Write-back**: results carry `meta.search.warm_start_job` (the prior handle) and `warm_start_population_size` (rows seeded from the database).
4. **Acceptance criteria** for the warm-start path: a test runs two sequential searches on the same seed/box; the second names the first as `warm_start`; both complete within budget; the second's initial population includes at least one candidate from the first.

### Explicitly NOT in scope

- Constraint solving beyond AGOX's built-in `RandomGenerator`/`SamplingGenerator`; we don't build a custom generator.
- Hyper-parameter tuning for the surrogate model.
- Hyperspatial generators or degree-of-existence degrees of freedom (slice 3).
- Evolutionary / genetic algorithm variants; AGOX remains the oracle source.

### Acceptance criteria

- A test builds a seed slab, runs a `struct_search` with `add={"Pd": [1, 3], "N": [0, 1]}` and `budget=50`, captures the job handle.
- A second search on the same seed uses `warm_start=<prior_handle>`, `add` with different ranges (e.g., `{"Pd": [2, 4], "O": [0, 1]}`), and `budget=100`; it completes and carries `meta.search.warm_start_job` + `warm_start_population_size`.
- Without a prior job, omitting `warm_start` behaves as slice 1 (no regression).
- `uv lock` resolves without moving any pinned dependency.

### Target + blast radius

- `src/precis/workers/job_types/struct_search.py` (extend from slice 1): handle `add` ranges, wire AGOX generator constraint.
- `src/precis/structure/search.py` (extend): add `warm_start` param, call `Database.load(artefact_path)` if present, seed surrogate.
- Tests: `tests/test_struct_search.py` (add two-job warm-start scenario).

### Open questions / decisions log

- OPEN (non-blocking): AGOX `Database` save/load as warm-start — verify the API before speccing. (Slice 1's decisions log is in git: `docs/backlog/global-structure-search-gofee-agox.md` at 98fcdfa8.)

## global structure search — slice 3: hyperspatial / degree-of-existence generators (Pickard 2019, Hammer 2025)

_Grouped 2026-09-26; was `global-structure-search-slice-3-hyperspatial`, status draft, prio normal._

Plug AGOX generators that operate in extra spatial dimensions and extended chemical identity spaces into the slice-1 `algo` param framework, enabling topological rearrangements and rare phases inaccessible via direct 3D search.

### Motivation / why

The Pd(111) NO→NH₃ quest and the nanobud junction work sit in configuration spaces where 3D Cartesian search gets trapped in false minima. Pickard 2019 showed that embedding the structure in an extra spatial dimension turns these traps into saddle points the search can escape. Hammer 2025 extends this: instead of just spatial coordinates, include chemical-identity and degree-of-existence degrees of freedom inside the ML fingerprint, letting the surrogate explore phases that differ not just in position but in stoichiometry / bonding character without fixing composition upfront.

These are AGOX generator plugins, not a new algorithm. Slice 1's framework already allows swapping `algo` (GOFEE / basin_hopping / random); these fit the same slot.

### In scope

1. **Hyperspatial generators**: AGOX wrapper generators that embed the search in extra spatial dimensions (e.g., the method from Pickard 2019, PRB 99 054102 — `pa346950`). Dimensions are added computationally; at the end, they are projected back to 3D.
2. **Degree-of-existence generators**: AGOX generators plugged into the fingerprint (Hammer et al. 2025, npj Comput. Mater. — `pa346948`) that explore chemical identity and "existence" degrees of freedom (4–6D positions, off-site occupancy variants, valence-state mixing) inside the learned representation, not in direct 3D. The fingerprint maps back to valid 3D structures.
3. **Job type integration**: `struct_search` gains `algo='hyperspatial'` and `algo='degree_of_existence'` options (or a composite; TBD at ready time — see slice 1's decisions log). The param is validated against AGOX's available generators at runtime.
4. **Artefacts + metadata**: results carry `meta.search.generator_type` (e.g., `hyperspatial`, `degree_of_existence`) so subsequent processing can route the candidates appropriately (e.g., higher scrutiny at DFT, since the generator space is less direct).

### Explicitly NOT in scope

- Custom fingerprint design or retraining the ML model. The fingerprint is inherited from the MACE calculator + AGOX's standard dscribe/ASE setup.
- Combining hyperspatial + degree-of-existence in a single search in this slice. (Multi-generator compositions are a later item, if needed.)
- Validation of the Pickard or Hammer papers' claims; we implement and deploy their methods as-is.
- Swapping the oracle from MACE to DFT or any other model.

### Acceptance criteria

- Tests stub AGOX generators for both hyperspatial and degree-of-existence; a search with `algo='hyperspatial'` dispatches the hyperspatial generator, and a search with `algo='degree_of_existence'` dispatches the DOE generator. Results carry the correct `meta.search.generator_type`.
- A real run (manual, logged) on the GPU node with a known-hard problem (e.g., Pd(111) + small adsorbate overlayer) using hyperspatial search finds ≥ 5 distinct candidates within the budget that are geometrically distinct from the LLM proposer's guesses.
- Without AGOX's generator modules installed, dispatching a search with `algo='hyperspatial'` or `algo='degree_of_existence'` fails fast with an infra-classed event naming the missing component; `scripts/test` runs green via the stub.
- `uv lock` resolves without moving any pinned dependency.

### Target + blast radius

- `src/precis/workers/job_types/struct_search.py` (extend): validate `algo` param against available AGOX generators; wire hyperspatial + DOE generators.
- `src/precis/structure/search.py` (extend): instantiate the generator class based on `algo`; pass to AGOX environment.
- Tests: `tests/test_struct_search.py` (add hyperspatial and DOE stubs + integration tests).
- Skills: optional update to `precis-structure-help.md` to document the new generator types (TBD at ready time).

### Open questions / decisions log

- TBD at ready time — see slice 1's decisions log for the overall design approach.
