# 0053 — Ingesting external DFT catalyst libraries into the `structure` kind

- **Status**: proposed (2026-07-09) · design conversation captured, not
  yet sliced. This ADR records the *decisions*; the source-survey +
  ETL-pattern exploration belongs in a
  [`docs/design/`](../design/) plan when the first slice is scoped.
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0043 — the `structure` kind](./0043-structure-kind-atomistic-ir.md)
    — the atomistic cell + bond-graph IR external data lands in, its
    `struct_runs` **run-cube** (§9/§23.16, content-addressed on
    `(structure_sha, fidelity, model, params, code_version)`), the
    `derive` lineage verb, and the `diff` probe. **The external corpus is
    imported *as* `structure` designs + pre-filled run rows — no new
    kind.** External-DB import is the deferred-vision bullet in
    `precis-structure-help` this ADR promotes to a decision.
  - [ADR 0044 — the derived-job lane](./0044-derived-job-lane.md) — a
    relax with no local backend dispatches to the GPU node parented on
    the structure itself. A derivative of an imported config re-uses this
    lane unchanged; the import path is its cache-fill twin.
  - [ADR 0007 — derived queue, no block jobs](./0007-derived-queue-no-block-jobs.md)
    — the idempotent, content-addressed derived-artifact philosophy. A
    bulk import is the same shape: a re-run skips, never duplicates.
  - **AGENTS.md ingest guarantees + `ref_identifiers` idempotency** — the
    "an external ID collapses to one ref" discipline the import path
    mirrors for `(dataset, config_id)`.
  - [ADR 0051 — turn-taking & blackboard convergence](./0051-turn-taking-persona-threads-and-blackboard-convergence.md)
    — the blackboard the §10 comparison board instantiates for the
    structure domain: N forked design threads converging on one shared
    observer watchlist.

## Context

The `structure` kind gives the LLM a legible way to *build* and *reason
about* atomistic designs (Pd slabs, adsorbates, clusters) and relax them
on a fidelity ladder (`clean` → `ml` → `dft`). What it lacks is **prior
art at scale**: every design starts from a blank cell, and the only
energies in the system are the ones we spend GPU-hours computing.

Meanwhile there are large, downloadable DFT catalyst libraries — millions
of relaxed configurations with energies + forces, much of it Pd-relevant:

- **AQCat25** (SandboxAQ) — ~11M DFT calcs over ~40k catalytic systems,
  500 eV cutoff, explicit spin; predefined splits + element/adsorbate
  filters, so a **Pd-sliced subset** is extractable without the full
  archive. CC BY-NC-SA.
- **Catalysis-Hub / CatApp** (SUNCAT) — REST API + web UI; query by
  element/reaction/facet/adsorbate and pull slabs + adsorption energies.
  A ready source of Pd(111)/(100)/alloy surfaces.
- **OC20 / OC22** — multi-GB HDF5/LMDB bulk archives; slice locally.
- **NCCR Catalysis index** (Zenodo / Materials Cloud / ioChem-BD) — >300
  curated datasets, several Pd single-atom / support-specific, one
  tarball per paper (CIF/XYZ + CSV/JSON).
- **ColabFit** — the exception that standardises to LMDB/Parquet/xyz.

Two facts shape the design: (1) **formats are heterogeneous** (JSON /
HDF5 / LMDB / Zarr / HuggingFace / CIF+CSV, each with its own metadata
schema) — an ETL layer is unavoidable; (2) **access splits two ways** —
true bulk dumps (tarball / HDF5 / HF dataset) vs query/API on-demand
(Catalysis-Hub REST, Materials Project, NOMAD). We have **TB of disk and
a fast link**, so storage economics are permissive but not infinite —
the question is *what* to persist, not *whether* we can.

The user's question, precisely: **how do we bring this in — on demand,
as a batch, or on demand plus derivatives?** The answer is *all three*,
as one substrate with three entry points.

## Decisions

### 1. No new kind. External configs are `structure` designs + run-cube rows.

An imported `(dataset, config_id)` becomes an ordinary `structure`
design (a cell + atoms; bonds inferred, §8.1) **plus one pre-filled
`struct_runs` row** carrying the source's energy + forces. It joins the
same TOC, probes, `search`-by-intent, `diff`, and `derive` as a
hand-authored design. The external DFT result is *just another rung on
the run-cube* — the ladder already models "an energy someone else
computed" the same way it models one we computed.

No `dft_calculation` kind, no `dataset` kind. A dataset is an **open tag
+ provenance**, not a top-level kind (AGENTS.md: no new kind without a
distinct corpus role / namespace / citation semantics).

### 2. An adapter registry is the ETL seam — one normaliser per source.

Format heterogeneity is absorbed by a small **adapter registry**
(`structure/importers/`), one adapter per source, each a pure function:

```
adapter(raw_record) -> (Scene, ExternalRun, ExternalId)
```

- **`Scene`** — the ADR 0043 cell + atoms, built via ASE where the
  source is ASE-readable (CIF/extXYZ/POSCAR/LMDB-atoms), else hand-mapped.
  ASE is already the `[dft]`/`[dft-ml]` extra; importers are gated behind
  a new **`[import]`** extra (ASE + `datasets`/`h5py`/`lmdb` as the
  source needs), never a top-level dep.
- **`ExternalRun`** — the run-cube payload: `energy`, `max_force`, the
  relaxed `final_geometry`, and a **`method` fingerprint** (§4).
- **`ExternalId`** — `(dataset, config_id)` for idempotent collapse (§3).

Adapters land incrementally, cheapest-first: **Catalysis-Hub** (REST,
already-curated, small) and an **AQCat25 Pd split** first; OC20 LMDB and
NCCR/Zenodo tarballs follow. Each adapter is unit-tested against a tiny
checked-in fixture record, so the normaliser is exercised without the
multi-GB source.

### 3. Three ingest modes over one idempotent write path.

All three resolve to the same `store.structure_import(scene, run,
external_id)` — idempotent on `external_id` (a re-import updates mutable
fields, never duplicates, exactly like `ref_identifiers`):

- **On-demand (query → hydrate one).** `search`/`get` against a remote
  catalog (Catalysis-Hub REST, Materials Project) resolves a specific
  config; **first touch hydrates it** into a `structure` design + run
  row and caches it forever. This is the paper/web cache-on-first-touch
  pattern retargeted to atoms — the LLM asks "give me a relaxed Pd(111)
  OH config" and gets a real, cited one, materialised lazily.
- **Batch (bulk mirror).** `precis import <source> --filter element=Pd`
  is a CLI job that bulk-downloads a **Pd-sliced** split to NFS, streams
  it through the adapter, and inserts N designs + run rows. Bounded by
  the same thresholds discipline (a cap on N per invocation; resumable on
  the external-ID cursor). The raw split stays on NFS; the DB holds only
  the normalised Scene + run row + a source pointer.
- **On-demand + derivative.** An imported design is a **read-only
  reference anchor** (tagged `provenance:external`, §5). The LLM
  `derive`s a variant off it — substitute the metal, add/rotate an
  adsorbate, strain the cell, pull an atom — producing a new
  `derived-from` design. It pre-relaxes on the cheap **`ml` rung**
  (which the imported corpus itself helps train, §7), then dispatches
  DFT via the ADR 0044 lane. The imported DFT energy is the **ground-truth
  baseline** the derivative's `diff` + energy is read against.

The cheapest *physical* step in that loop should not require torch — see §8.

### 4. A `method` fingerprint gates cache-sharing and forbids naive comparison.

The run-cube's identity today is geometry + `(fidelity, model, params,
code_version)`. External DFT adds a **method axis** — functional, plane-
wave cutoff, k-mesh, spin treatment, pseudopotentials, dataset DOI. Two
consequences:

- **Cache identity widens** to include the method fingerprint, so an
  AQCat25 config (PBE, 500 eV, spin-polarised) and *our* GPAW relax of
  the same geometry are **distinct rows**, not a false cache hit.
- **Energies are only comparable within one method.** A `diff`/energy
  read across two different functionals is a **category error**; the
  handler surfaces the method fingerprint and refuses (or loudly flags) a
  cross-method ΔE. An adsorption/reaction energy is only trustworthy when
  slab, adsorbate, and reference share a fingerprint — the discipline
  every one of these datasets documents and we must not silently violate.

### 5. Provenance is first-class; external runs never masquerade as ours.

Every imported design + run carries structured provenance: the
`dataset`, `config_id`, DOI/URL, licence (**AQCat25 is CC BY-NC-SA —
non-commercial**, recorded so downstream use respects it), and the §4
method fingerprint. Two guards:

- `struct_runs.provenance='external'` (vs `'computed'`) — `view='runs'`
  labels imported rows, and the cache-fill path for *our* compute never
  overwrites an external row.
- An imported design is **not editable in place** — an `edit` op on a
  `provenance:external` design errors with "derive a variant instead,"
  so the reference corpus stays a faithful mirror and all human/LLM work
  branches cleanly off it (the ADR 0043 `derive` lineage).

### 6. Persist the normalised config; do not mirror raw payloads by default.

TB of disk does not mean mirror everything. A relaxed config (geometry +
energy + forces + method) is **kilobytes**; the DB holds that. What we do
**not** ingest by default: full relaxation *trajectories*, wavefunctions,
charge densities, and the raw multi-GB archives — those stay on NFS
behind the source pointer and are pulled only when a specific derivative
demands them (an intermediate frame, a restart density). This keeps the
DB the system-of-record for *legible, comparable* results (ADR 0043 §12)
and NFS the bulk store for *raw* payloads — the same split as the
`struct_relax` job's staging.

### 7. The imported corpus is training data for the local MLIP rung.

The `ml` fidelity rung (MACE-MP-0 / CHGNet) is what makes the
derive→relax loop cheap enough to explore variants. A large Pd-focused
imported corpus of DFT energies + forces is **exactly an MLIP training /
fine-tuning set**. So imports are not only prior-art lookups — they
close a loop: import DFT → fine-tune the local potential → cheaper,
more accurate `ml` pre-relaxes → better derivative proposals → fewer
wasted DFT dispatches. This is a downstream workstream (its own plan),
but the import schema (§2 `ExternalRun` carries forces, not just energy)
is chosen now so the training set is available without a re-ingest.

### 8. A pure-Python `emt` rung — a torch-free physical pre-relax (extends ADR 0043 §9).

The 0043 fidelity ladder jumps from `clean` (rung 0: pure geometry
repair, no energy, no deps) straight to `ml` (ASE + a **torch** MLIP,
the heavy `[dft-ml]` extra). For deriving and iterating on Pd variants
that gap is expensive: the LLM wants a *quick, physical, dependency-light*
minimisation to sanity-check a derivative before spending an MLIP forward
pass, let alone DFT.

ASE ships exactly that — the built-in **EMT** (Effective Medium Theory)
calculator (`ase.calculators.emt`) with ASE's pure-Python **FIRE/BFGS**
optimizers. It is **numpy + ASE only, no torch**, and its default
parameters cover precisely the fcc catalytic metals — **Al, Ni, Cu, Pd,
Ag, Pt, Au** — plus H/C/N/O adsorbates. That is an almost-perfect match
for a **Pd-first** catalyst focus.

So the ladder gains a new rung **between `clean` and `ml`**:

- **`emt`** — real (if *approximate*) energy + forces via ASE EMT +
  FIRE/BFGS. Gated behind the existing **`[dft]`** extra (ASE, already
  pulled for CIF export) — **not** `[dft-ml]`, so it runs on a torch-free
  host and **never dispatches to the GPU node**. Honours the `fixed`
  bitmask and returns the same convergence envelope as `ml` (§9/§22-D).
- **Element guard, not a crash.** EMT's coverage is a *closed set*. A
  structure with an out-of-set element raises `RelaxUnsupported` with a
  clear "EMT covers {Al,Ni,Cu,Pd,Ag,Pt,Au,H,C,N,O}; use fidelity='ml'"
  hint — legible, never a silent wrong answer.
- **Honestly labelled as qualitative.** EMT is a fast semi-empirical
  potential, not DFT-accurate; its run-cube rows carry `model='emt'` and
  are never method-comparable to an imported PBE energy (the §4
  fingerprint keeps them distinct). It is a *geometry/relative-stability*
  pre-filter, not a source of publishable energies.

This makes the derive→relax loop cheap and torch-free for the common
case (a Pd variant), reserving the MLIP `ml` rung and the GPU DFT
dispatch for when the extra fidelity actually earns its cost. It is a
pure extension of the 0043 ladder — `relax(fidelity='emt')` slots in
alongside `clean`/`ml` with no schema change.

### 9. Cadence: validate always (auto), relax on request; the gate blocks compute, not construction (refines ADR 0043 §5c/§6.4).

*How often does any of this run?* Split by cost — the answer is different
for the microsecond check and the second-to-hour minimisation.

- **Validate — precis decides, automatically, on every `put`/`edit`.**
  `validate(scene)` (overlap + over-valence) is pure and microseconds, so
  it runs on *every mutation* and its findings are **echoed in the
  response**. A physically-impossible proposal — the "this carbon has 12
  H attached" (`over_valence`) case, or a sub-covalent overlap — surfaces
  **the instant it is created**, without the LLM remembering to ask. This
  is net-new: today `validate` is opt-in (`view='validate'`); this makes
  it an always-on echo on the write path.
- **Relax — the LLM decides, explicitly, never automatically.** *Not*
  per atom-add. Two reasons: (a) construction is **incremental** — you
  add a C, then its four H over several ops, and mid-sequence the graph
  transiently looks wrong, so auto-relaxing every op would fight the
  builder; (b) even the cheap rungs *change state* (`clean`/`emt` move
  atoms; energy rungs cost real time), and precis must never mutate or
  spend compute unbidden. `relax` is the semantic "I'm done editing this
  region — fix the geometry / get me an energy" act.
- **The gate blocks *compute*, not *construction*.** Validation is
  **advisory by default** (warn + echo, never reject an edit — a
  half-built molecule must be allowed to exist between ops). The block
  bites only at the **energy-rung boundary**: `emt`/`ml`/`dft` **refuse
  to run on a hard validator finding** (no DFT dispatch wasted on a 12-H
  carbon), returning the findings + their `suggested_fix`. `clean` is
  **exempt** — it *is* the remedy for a bad geometry. This is the "cheap
  rules before any compute" the 0043 validator docstring already
  promises, finally enforced.
- **Write-time checks are monotonic-only; completeness is deferred.**
  This is what makes auto-validate-on-write safe rather than a nag. A
  write-time rule may flag only a **construction-invariant** violation —
  one that *adding more atoms/bonds cannot fix*: **over-valence**
  (monotonic-worsening) and **overlap**. The dual — **under-saturation**
  (a C with one bond, three still to come) — is **transient**: it
  resolves as you keep building, so it is **never an edit-time failure**.
  Today it isn't even a rule (it's the `view='find', undercoordinated`
  *query*), and it must stay that way. So the C-with-one-bond builds
  silently; the 12-H carbon flags immediately; the incomplete methyl only
  registers if you try to spend energy on it.
- **The completeness check *is the relax* — do not hand-write a
  saturation validator (§8).** The deferred "write a bunch, *then*
  validate" step is not a bespoke valence-counting heuristic; it is the
  **physical rung itself**. An undersaturated, strained, or unstable
  structure shows up as **non-convergence / high residual force /
  runaway energy** in the `emt` (or `ml`) run — a *more truthful* verdict
  than any rule table, catching what the two microsecond heuristics never
  can (strained rings, wrong coordination geometry, unstable adsorption),
  with nothing to maintain. So the two tiers are **complementary, not
  redundant**: the cheap validator's job is not physical truth but
  (a) instant **triage** so the fancy tool is never spent on obvious
  garbage, and (b) an **actionable op-vocabulary fix** *before any relax
  has run* ("remove a bond") — a raw force magnitude can't tell the LLM
  which op to emit; the rule can. The `emt`/`ml` rung is the authoritative
  physical judge; the write-time rules are the free pre-flight.

|                       | cadence                    | decided by                          |
|-----------------------|----------------------------|-------------------------------------|
| `validate`            | every `put`/`edit` (auto)  | **precis**                          |
| `clean`               | on request                 | **LLM**                             |
| `emt` / `ml` / `dft`  | on request, **gated**      | LLM asks; **precis refuses if invalid** |

In the derive loop (§3/§8) this is exactly the desired failure mode: a
derived variant auto-validates on creation and echoes any finding, an
`emt` pre-relax is a deliberate LLM step, and a broken proposal fails
**cheaply and legibly at edit time** — never as a wasted GPU job. A
`strict` mode that hard-rejects an invalid *edit* (not just an invalid
*relax*) is available as an opt-in, off by default.

### 10. The comparison board — N designs held together, keyed by shared observers (the ADR 0051 blackboard).

The derive loop (§3) and imports produce *many* candidates at once — a Pd
variant sweep, an imported config plus its derivatives. The LLM needs to
hold a small working set of **2..N designs side by side** and read the
**"eyed things"** — the **eyes** (the ADR 0043 §6.6/§6.8 persisted
observers with a point of view) and §7 measures — across all of them in
one glance. That shared
watchlist across concurrent design threads **is the ADR 0051 blackboard**,
instantiated for the structure domain: forked threads exploring one
variant each converge on one board.

- **Shape: a pivot table.** Rows = **observers**, columns = the **N
  designs** in the working set, cells = the **current value + verdict**.
  The user's sketch, made concrete:

  |  observer               | baseline | design 1 | design 2 |
  |-------------------------|----------|----------|----------|
  | bond order (active C–C) | 1.0      | 1.4      | 0.4      |
  | bond type  (active C–C) | single   | single   | h-bond   |
  | adsorbate–defect (Å)    | 3.1      | 2.2      | —        |
  | energy (eV)             | −12.9    | −12.3    | −11.8    |

- **Keyed by *portable* observers, never by raw atom label.** design 2's
  `aC1` is not design 1's `aC1`, so a row cannot key on a label — it keys
  on a **role / site / pattern** (`@active_site`, "the fcc-hollow adsorbate
  bond") that **resolves per design**, so one row means the same *thing* in
  every column. A **blank cell** (observer doesn't resolve in that design —
  no such bond) is itself a finding, not an error. This is exactly the
  0043 §12-B note that cross-experiment cursors live on the **exploration
  scope's `meta`** (above any one structure), reusing the `embodiment`
  shape — promoted here from *vision* to a concrete view.
- **Method-fingerprint-aware per row (§4).** An energy row must not place
  a PBE-imported baseline and our GPAW derivative in comparable cells
  without flagging the functional mismatch — the same guard as the pairwise
  `diff`, applied column-wise. Geometry/graph rows (bond order, distance)
  are method-agnostic and compare freely.
- **Backing: the ensemble cube (0043 §12-D), sliced.** The board is a
  *view* — (observer × design) over the in-memory working set, write-through
  to the `runs × structures` cube. **N in memory is a bounded working set**
  (the blackboard's active set: pinned first, else ordered by a chosen
  measure, else recency); beyond N spills to PG and is queried via the
  cube's OLAP slice, never all loaded. This is the §10 memory-vs-PG
  boundary ("one structure → memory; across structures → PG") widened to a
  small *set* of live structures.

## Consequences

- **The `structure` kind gains a corpus of prior art** — `search(kind=
  'structure', q='OH on Pd(111)')` can return real, cited, DFT-relaxed
  configs, not only what we hand-built.
- **One idempotent write path, three entry points** — on-demand hydrate,
  batch mirror, and derivative-off-a-reference all funnel through
  `structure_import`; re-runs skip on `external_id`.
- **A new `[import]` extra** (ASE + per-source readers) — gated, never a
  top-level dep (AGENTS.md); a missing extra returns Unsupported with an
  install hint, exactly like `[dft]` CIF export.
- **The run-cube grows a method axis + provenance** — a small
  forward-only migration on `struct_runs` (method fingerprint columns +
  `provenance`); external and computed rows for one geometry coexist.
- **Cross-method energy comparison is guarded**, not left to vigilance —
  the handler refuses/flags a ΔE across fingerprints (§4).
- **Licence provenance is carried** (AQCat25 non-commercial), so a later
  export/publication step can honour it.
- **A batch import is a bounded, resumable CLI job** subject to the
  thresholds discipline — no unbounded "download 11M rows" footgun.
- **A torch-free `emt` rung fills the ladder gap** (§8) — Pd-family
  derivatives get a real physical pre-relax on any host under the `[dft]`
  extra alone, so the common case never touches torch or the GPU node.
- **Validation becomes an always-on echo** (§9) — invalid chemistry
  (over-valence / overlap) surfaces on every `put`/`edit`, and no energy
  rung ever spends compute on a hard failure; the write path grows an
  auto-`validate` echo (net-new — it was opt-in `view='validate'`).
- **A comparison board promotes the cross-experiment tier** (§10) — the
  0043 §6.8 cross-experiment cursor tier (was *vision*) becomes a concrete
  (observer × design) pivot over a bounded in-memory working set, backed
  by the §12-D cube; it is the 0051 blackboard for structure design.

## Sequencing

0. **The `emt` rung** (§8) — independent of the import path and shippable
   first: a `_relax_emt` in `structure/relax.py` (ASE EMT + FIRE, `[dft]`
   extra, element guard), `fidelity='emt'` in the ladder + the skill
   table. Immediately upgrades the derive loop.
1. **Schema + write path**: `struct_runs` method-fingerprint + provenance
   columns; `structure_import` idempotent on `(dataset, config_id)`; the
   `provenance:external` read-only guard on `edit`.
2. **First adapter — Catalysis-Hub REST** (small, curated, on-demand
   hydrate). Proves the adapter seam + the on-demand mode end to end.
3. **Batch mirror — an AQCat25 Pd split** through the same adapter seam;
   `precis import` CLI with `--filter` + a resumable cursor.
4. **Derivative loop** — wire `derive` off a `provenance:external` anchor
   to the existing ADR 0044 `ml`/`dft` dispatch; `diff`-vs-baseline.
5. **MLIP fine-tuning** (§7) — a separate plan; the import schema already
   captures forces for it.

## Out of scope (v1)

- **OC20/OC22 LMDB + NCCR/Zenodo tarball adapters** — land after the
  Catalysis-Hub + AQCat25 pair proves the seam; each is "just another
  adapter."
- **Mirroring trajectories / wavefunctions / densities** (§6) — pulled
  on demand per-derivative, not bulk-ingested.
- **The MLIP fine-tuning pipeline itself** (§7) — the schema is chosen to
  enable it; the training job is its own workstream.
- **Cross-method energy *reconciliation*** (e.g. a correction scheme to
  compare PBE vs our GPAW) — v1 *refuses/flags* the comparison (§4);
  reconciling is deliberately not attempted.
- **A bespoke valence-saturation / completeness validator** (§9) — not
  built: the `emt`/`ml` relax's convergence + residual-force envelope is
  the more truthful completeness signal, already computed. Write-time
  rules stay monotonic-only (over-valence + overlap).
- **Molecular Pd complexes / ligand cross-coupling** as a distinct track
  — the heterogeneous surfaces/adsorbates path ships first; molecular
  (`pbc=[F,F,F]`) configs fall out of the same IR when a source is added.

## Rejected

- **A `dft_calculation` / `dataset` top-level kind** — external configs
  have no corpus role, namespace, or citation semantics distinct from a
  `structure` design; they *are* structure designs with a pre-filled run
  (§1). (`precis-dft`'s `dft_calculation` stays that project's concern;
  the kind-merge is a separate ADR 0043 §23.12 note, not this one.)
- **One universal on-the-fly loader over every source** — the formats are
  too divergent (§ Context); a thin per-source adapter to one canonical
  Scene + run is simpler than a mega-schema, and each adapter is
  independently testable.
- **Bulk-mirroring full archives into Postgres** — the DB holds the
  normalised, comparable result (kilobytes); raw multi-GB payloads stay
  on NFS behind a pointer (§6).
- **Editing imported designs in place** — would corrupt the reference
  mirror and confuse provenance; all work `derive`s a fresh, linked
  variant (§5).
- **Treating external energies as directly comparable to ours** — a
  category error across functionals; the method fingerprint makes the
  incompatibility explicit and machine-checkable (§4).
- **Making the cheap physical pre-relax depend on torch** — the `ml` rung
  needs the heavy `[dft-ml]`/GPU path; the derive loop's common case
  (a Pd-family variant) gets a torch-free ASE-EMT rung instead (§8), and
  only escalates to `ml`/DFT when the fidelity is worth it.
