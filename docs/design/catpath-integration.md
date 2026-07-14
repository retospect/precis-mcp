# Native catpath integration — reaction pathways as first-class structures

> **Status:** design-of-record, not yet sliced into code. Present-tense
> where it describes precis today; future-tense for the proposed build.
> Companion to `structure-*` (ADR 0043), the derived-compute lane
> (ADR 0044), and `sandbox-run.md`. Read those first.

## 0. Thesis

[catpath](https://github.com/retospect/catpath) is a computational-chemistry
tool: give it a metal surface, a substrate, and a target; it builds a
reaction network, relaxes every intermediate on an ML interatomic
potential, finds transition-state barriers with climbing-image NEB, and
reports energies with **honest, pooled uncertainty** (low-confidence
results flagged rather than reported as precise numbers). It is CLI-only,
YAML-configured, and emits `results.json` + three diagrams + a
provenance-linked `methods.md`.

That shape is not foreign to precis — it is the `structure` keystone kind
(ADR 0043) plus the derived-compute lane (ADR 0044), almost exactly.
catpath's intermediates **are** precis structures; its relaxations **are**
run-cube rows; its uncertainty flag **is** the mission's no-precise-number
pillar; its `methods.md` **is** an embeddable, citable chunk.

**"Native" means:** every catpath intermediate becomes a first-class
`structure` ref, every relaxation lands in the existing `struct_runs`
run-cube, and the reaction network sits on top as a thin new kind. The
diagrams and numbers are **derived** — the config is authoritative, and
editing it regenerates everything, reusing the content-addressed cache so
unchanged intermediates cost zero compute.

This doc maps the two worlds, states what we reuse vs. build, and — the
part that matters — enumerates the genuine messiness. §3 opens with four
tensions that shape the whole design. **Note (post-review):** precis and
catpath are *both* GPL-3.0-or-later under one owner, so tension #1
(licensing) is resolved in favor of **importing catpath in-process** — this
propagates through the doc; the arms-length wrapping described in some
passages is the rejected alternative, kept for contrast.

---

## 1. catpath as a pipeline (the kernel we rent)

catpath is a chain of content-addressable steps, which is what makes it
integrable:

```
config.yaml
  │  substrate, target, element, network strategy, slab, mlip backend, search/seeds
  ▼
build network            (network.py / explore.py — RDKit rule-based or curated template)
  │  N intermediate states (ASE Atoms = slab + adsorbate, FIXED atom order per edge)
  │  M elementary steps (reactant → product edges)
  ▼
relax each state         (relax.py — BFGS on an ASE Calculator; seeds × poses ensemble)
  │  per-state Estimate(mean, std, n, low_confidence)   [uncertainty.py]
  ▼
NEB each edge            (neb.py — climbing-image, N images, automatic retry)
  │  per-edge barrier + delta_e Estimate
  ▼
build graph              (graph.py — NetworkX DiGraph, node_link_data JSON)
  ▼
render + report          (render.py/viz.py — 3 PNGs; provenance.py — methods.md, config.snapshot.yaml)
```

Key internal facts (verified against `src/catpath/`):

- **Structures are raw ASE `Atoms`** (`structures.py`), built `slab +
  adsorbate` with a hard invariant: *two states connected by a reaction
  must have the same atoms in the same order* so NEB can interpolate.
  There is **no serialization of its own** — structures live as ASE
  objects, exportable via ASE to extxyz/POSCAR.
- **Backends are ASE `Calculator` factories** (`calculators.py`,
  `make_calculator`), lazy-imported, **one per installed environment**
  (EMT / MACE / CHGNet / FAIRChem / GRACE) — they *cannot share an env*.
- **Uncertainty is an `Estimate` dataclass** (`uncertainty.py`:
  `mean, std, n, values, low_confidence`) pooled across seeds × models;
  `low_confidence` trips when `std > spread_tol` or `n < 2`.
- **The graph is a NetworkX DiGraph** with `to_json` (node_link_data) /
  `to_csv`; nodes carry `energy/energy_std/rel_energy/low_confidence`,
  edges carry `barrier/barrier_std/delta_e/delta_e_std/low_confidence`.
- **Provenance is deterministic text, not a hash** (`provenance.py`):
  catpath guarantees "same config → same methods paragraph" but does
  **no content-addressing**. precis must supply the cache key.
- **License is GPL-3.0-or-later.** See §3.1 — this is load-bearing.

---

## 2. The native mapping

| catpath concept | precis substrate | Fit |
|---|---|---|
| `config.yaml` (substrate/target/slab/mlip/search) | body IR of a new **`pathway`** kind | authoritative intent, diffable, embeddable |
| intermediate state (ASE Atoms) | **`structure`** ref (`Scene`, `struct_atoms`/`struct_bonds`) | **native** — but needs the missing ASE→Scene ingest (§3.2) |
| a single relaxation | **`struct_runs`** row (fidelity=`ml`, `model`, `energy`, `final_geometry`, `cache_key`) | run-cube already exists; catpath fills it (§3, §5) |
| NEB band + climbing image | new **`struct_neb`** job + **`struct_frames`** (already has `positions` for "MD/NEB") | schema anticipated it; job type is new (§3.3) |
| `Estimate(mean±std, low_confidence)` | new pooled-uncertainty layer on the pathway edge/node | **no home today** (§3.4) |
| reaction network (DiGraph) | the **`pathway`** kind's graph (nodes→structure links, edges as rows) | new kind (§4) |
| ML backend (MACE/FAIRChem…) | `struct_runs.model` + per-backend container image | model already in the cache key; images are new ops (§3.5) |
| `config.snapshot.yaml` | **`provenance`** ref | direct |
| `methods.md` | embedded `pathway`-body chunk (searchable, citable) | direct, high value |
| 3 PNGs | derived **figures** — harvest now, native SVG re-render later | regenerable either way (§6, §3.6) |
| `low_confidence` flag | propagates to cite-time honest-uncertainty | mission pillar (§3.4) |
| the whole run | a **derived job** (ADR 0044) owned by the pathway ref | reuse compute lane wholesale |

---

## 3. The four tensions (read before designing anything)

### 3.1 License — RESOLVED: both are GPL-3.0-or-later, same owner

**There is no license tension.** precis's `pyproject.toml` declares
`license = "GPL-3.0-or-later"` — identical to catpath. And Reto owns
catpath outright with full relicensing control. So precis may
`import catpath` directly, in-process, today. The arms-length container
boundary this section originally demanded was purely a GPL firewall; there
is no firewall to work around.

This unlocks the **import-based architecture** (deeper and less total work
than the arms-length wrap):

> **precis imports catpath's pure core** (config parse, network builder,
> structure/slab builders, `graph`, `uncertainty.aggregate`, `provenance`)
> and runs it in-process in the worker. **The heavy relax/NEB delegates
> back through a `ComputeBackend` seam added inside catpath** (Reto owns
> it) to precis's existing `struct_relax` GPU run-cube. Only the ML
> backend calculators sit behind a container boundary — for an
> *operational* reason (§3.5), not a legal one.

Consequences that cascade:
- The "build" list shrinks: precis reuses catpath's network/structure/
  graph/uncertainty/provenance logic rather than reimplementing it (§5).
- **One relaxer, one cache** — catpath's relax/NEB routes through precis's
  run-cube instead of running its own. This dissolves the "two relaxers"
  messiness that an arms-length wrap would have created (§7.1).
- Division of labor: **catpath owns the chemistry** (what the network is,
  how states are built, how uncertainty pools); **precis owns
  persistence + the compute lane + the web interface.**

The one design task this creates: co-design the `ComputeBackend` seam in
catpath — a protocol catpath's `relax`/`neb` call to obtain energies/
forces/relaxed-geometry, which precis implements by minting `struct_relax`
jobs. It mirrors the `ComputeBackend`/`Staging` seam already sketched in
`sandbox-run.md`. Since both repos are ours, we shape the exact seam we
want rather than scraping output files.

### 3.2 The ASE→Scene ingest direction does not exist

precis has `structure/export.py::_to_ase` (Scene → ASE Atoms) but **no
reverse**. To make catpath intermediates native structures we must build
`Scene.from_ase(atoms)`:

- fractional positions ← `atoms.get_scaled_positions()`; cell ← `atoms.cell`;
  pbc ← `atoms.pbc`;
- **fixed bitmask** ← ASE `FixAtoms` constraint (catpath freezes bottom
  slab layers via `fix_layers`) → precis `struct_atoms.fixed`;
- **bond inference** — precis marks this "future (CrystalNN)"; catpath
  ships bond-free ASE Atoms, so nodes ingest with `provenance='inferred'`
  bonds or none. Slabs are large periodic cells; full bond inference may
  be unnecessary for a first slice (structures are legible by
  coordination/neighborhood probes without an explicit bond graph).
- **label assignment** — the sharp edge. precis assigns stable
  `aPd123`-style labels via `Scene.next_label`; catpath enforces *same
  atoms, same order* across a reaction pair. If precis re-labels or
  re-orders on ingest, **NEB interpolation between two ingested endpoints
  breaks.** The adapter must preserve catpath's atom order verbatim and
  assign labels positionally, and the NEB job (§3.3) must key on ordered
  geometry, not precis's deliberately order-*independent* `structure_sha`
  (§6). Round-trip fidelity tests are mandatory.

### 3.3 NEB is a band, not a point — but we needn't model the band

`struct_relax` (`workers/job_types/struct_relax.py`) relaxes **one**
structure and writes **one** `struct_runs` row. NEB operates on a **pair**
of relaxed endpoints, interpolates N images, and runs a climbing image.

**Decision: for slice 1 we do NOT model the elastic band natively.**
catpath computes the barrier (its `barriers` step); precis stores the
**number**. We model:

- the **endpoints** as `structure` refs (already the plan),
- the **barrier + Δe (with spread)** as edge attributes on the pathway,
- optionally the **saddle / transition-state geometry** as one extra
  `structure` ref — that's the physically interesting object worth keeping,
  far more than the interpolated band images.

The full band → `struct_frames` (which already carries `positions` per step
"for MD/NEB") is a **slice-2+ visualization nicety** (drawing the minimum-
energy path), not a requirement for honest barriers.

A native `struct_neb` job type is only needed if we want precis to *drive*
NEB through its own run-cube (order-sensitive cache key over both ordered
endpoints). That falls out naturally once the `ComputeBackend` seam (§3.1)
exists, and is deferred until then.

### 3.4 Ensembles and pooled uncertainty have no home

precis's run-cube is **one deterministic run per cache key**. catpath runs
`seeds: [0,1,2]` × multiple poses and **pools** into `Estimate(mean±std,
low_confidence)`. Two options:

- **(a) Ingest catpath's pooled numbers as-is** (slice 1). The pathway
  node/edge carries `energy_mean/std/low_confidence`; the run-cube stores
  whatever single geometry catpath returns as representative. Simple, but
  precis can't *add* seeds later without re-running catpath.
- **(b) Drive seeds from precis** (later). Each `(structure, model, seed)`
  is a distinct cache key → N run-cube rows → precis pools natively into a
  new `Estimate` at the pathway layer. Honest, cache-composable, but needs
  a pooling concept precis lacks today.

Either way, **`low_confidence` must propagate to cite time**: a `draft`
citing a barrier from a pathway must inherit the flag, so the "no claim
without honest uncertainty" pillar holds end-to-end.

**Where it lands (the honest-number loop):** the `citation` kind already
carries a `verifier_confidence` field — that is the natural home to *also*
carry the numeric `Estimate` (mean, spread) + `low_confidence`, so a draft
citing *"barrier 0.8 ± 0.3 eV (low confidence)"* inherits the flag and the
reader badges it. This closes the pillar end-to-end (pathway edge →
citation → reader badge) reusing an existing field rather than new plumbing.

**Reuse over reimplementation:** because catpath is imported (§3.1), even
"precis-driven pooling" (option b) is not a rewrite — precis calls
catpath's `uncertainty.aggregate()` over run-cube rows. Slice-1 ingest and
slice-4 native pooling share the same code path.

**Possible generalization (flag, don't build):** an `Estimate` — a measured
quantity with pooled uncertainty + a low-confidence flag — is on-mission
well beyond pathways (relax energies have model spread; `calc`/`math` carry
uncertainty). A precis-wide honest-number primitive is worth considering
later; scoping it now is creep. Keep it pathway-local for slices 1–3.

---

### 3.5 One backend per environment → N container images, not one

`struct_relax` today assumes **one** image (`precis-dft:cpu`) gated by
**one** capability (`REQUIRES = {"has_gpaw"}`). catpath backends *cannot
co-install* — MACE, CHGNet, FAIRChem, GRACE each need their own env. So:

- one container image **per backend** (`precis-catpath-mace`,
  `precis-catpath-fairchem`, …), each large (FAIRChem/MACE weights);
- the job's image + `REQUIRES` become **parameterized by model**
  (`has_catpath_mace`, `has_catpath_fairchem`), and GPU nodes advertise
  which they hold;
- EMT is dependency-free and CPU-only — it can run in a tiny image (or
  even in-process, mirroring how the `ml` rung loads MACE locally today),
  so **execution is mixed**: cheap backends inline, heavy backends as
  containerized `ssh_node` jobs.
- This is exactly `sandbox-run.md`'s per-image model; `struct_relax`'s
  single-image assumption must generalize to a `model → image/capability`
  table. This is real ops work (build + publish N images to the GB10 GPU
  node) and a real cluster-role change.

### 3.6 Diagrams: harvest bitmaps vs. re-render native

catpath renders 3 PNGs (energy profile w/ structure thumbnails, DAG
network, heatmap) with matplotlib + ASE. Two paths, both "regenerable"
because both are keyed to the pathway's content address:

- **harvest** the PNGs as figures/attachments (slice 1) — cheap, faithful,
  but bitmaps: no interactivity, and they duplicate weights of catpath's
  viz;
- **re-render natively** in `precis_web` from the graph + run data (energy
  profile as SVG, DAG like `/structure`, heatmap) — regenerable,
  interactive, and unlocks the `/structure`-style **under-pointer
  structure popovers** (hover a node → see the relaxed geometry). This is
  the real interface win catpath lacks, but it re-implements catpath's viz
  and is a later slice.

Recommendation: harvest in slice 1 (the PNG is derived and re-runs when the
IR changes — the regen guarantee holds), plan native SVG re-render once the
data layer is proven.

---

## 3.8 Grounding against the real catpath code (supersedes speculation above)

Reading the actual source (`src/catpath/`, ~3.5k LoC) sharpens the seam and
makes it *lighter* than §3.1's `ComputeBackend`-protocol framing implied:

- **catpath is already built for an external orchestrator.**
  `pipeline.run_one_seed(cfg, seed)` is documented as *"deliberately
  standalone and JSON-serialisable so an orchestrator (Snakemake) can fan
  out seeds across jobs and call `aggregate_partials`."* **precis replaces
  Snakemake.** This is the integration, in one sentence.
- **`pipeline.run()` is just a serial in-process loop** over
  `cfg.mlip.specs()` (the `(backend, model)` list) × `cfg.search.seeds`,
  calling `run_one_seed`, then `aggregate_partials(cfg, partials)`.
  `aggregate_partials` is **pure numpy** (`uncertainty.aggregate`, no ML
  deps) — it runs in-process in the precis worker.
- **The compute unit precis dispatches is `run_one_seed`, not a relax.**
  The relax/NEB leaf functions (`relax(atoms, calc, …)`,
  `neb_barrier(r, p, make_calc, …)`) already take the calculator *by
  parameter*, but each `run_one_seed` internally calls
  `make_calculator(cfg.mlip)` and runs the full BFGS/NEB loop — so it must
  execute **where the backend is installed** (the per-backend container /
  GPU node). One `run_one_seed` = one compute-lane job. No per-step force
  round-tripping.
- **Structures come back via a side channel.** `run_one_seed(collect=…)`
  stashes the lowest-energy relaxed `Atoms` per state in a dict (kept out
  of the JSON partial because `Atoms` aren't serialisable). To ingest them
  as `structure` refs, the job must **serialise those Atoms** (extxyz) into
  its out-dir. This is the one small catpath-side addition.

### The revised, code-grounded architecture

**precis is "just another orchestrator" for catpath's existing fan-out
unit.** The bridge:

1. builds `cfg` from the `pathway` body YAML and the network in-process
   (`config`, `network.build_network` — pure, no ML deps);
2. **fans out** one `catpath_explore` compute-lane job per `(model, seed)`,
   each running `run_one_seed` in the backend container on a capable node,
   **content-addressed + cached** on `sha(cfg-for-this-spec, seed,
   catpath_version)` — precis's parallelism + cache is the win over
   catpath's serial `run()`;
3. harvests each job's `partial.json` + serialised state structures;
4. calls `aggregate_partials(cfg, partials)` in-process → node/edge
   `Estimate`s;
5. ingests each relaxed state as a `structure` ref (`Scene.from_ase`, §3.2)
   and writes the `pathway` ref (edges, provenance, methods).

**Minimal catpath-side change** (you own it): a thin per-seed entry —
`catpath _seed <cfg> <seed> --out <dir>` (or an equivalent function) that
runs `run_one_seed` and writes `partial.json` + `states/<name>.extxyz`.
That's the container contract. **No `ComputeBackend` protocol needed for
slices 0–2.**

**Where the deeper `ComputeBackend` seam (§3.1) still earns its keep:** only
if we later want catpath's *individual relaxes* to route through precis's
`struct_runs` run-cube (one relaxer, per-state cache instead of per-seed
cache). That is a slice-4+ optimisation — the relax/neb functions being
already calc-parameterised means it's a clean refactor when we want it, but
we don't need it to ship.

## 3.7 Packaging — an out-of-tree plugin, not in-tree, not a CLI shell

precis has a first-class **entry-points plugin system** (four groups), and
**precis-dft is the live precedent** — a separate GPL sibling package that
plugs into this same structure/GPU world and is installed only where the
heavy deps live. catpath integrates the identical way: a **`precis-catpath`
bridge package** (own repo, GPL, depends on *both* `precis` and `catpath`)
advertising:

| entry-point group | contributes | catpath |
|---|---|---|
| `precis.handlers` | a kind (Handler + KindSpec) | the `pathway` kind |
| `precis.migrations` | schema | the `pathway_edges` table |
| `precis.job_types` | a compute-lane job (`JobTypeSpec`) | `catpath_explore` |
| `precis.ref_passes` | a worker pass (returns `None` off-GPU) | heavy-backend dispatch |

Why the bridge, not the two alternatives:
- **vs. in the precis tree:** the heavy ML stack (MACE/FAIRChem/torch) stays
  out of precis-core's dependency graph — core stays slim, the plugin is
  installed only on GPU/compute nodes (exactly how precis-dft deploys).
  precis's loader also *catches and logs* a broken plugin rather than
  bricking the MCP server (built-ins are trusted, plugins are not; a plugin
  claiming a built-in kind loses to the built-in).
- **vs. a CLI "calling mode":** the bridge gets a real kind, real tables, a
  real run-cube — not file-scraping a subprocess.

The bridge keeps both sides clean: **catpath stays domain-pure** (no precis
concepts), **precis-core stays chemistry-free** (no catpath dep). All glue —
including precis's `ComputeBackend` implementation wiring catpath's injected
relaxer to `struct_relax` — lives in the bridge.

**Where "calling" survives:** only the innermost per-backend ML calculator
(the §3.5 env-conflict layer), and even there it is precis's *own*
compute-lane job dispatch, not a catpath CLI shell — EMT/cheap-ML in-process
(env coexists), MACE/FAIRChem as a per-backend container `struct_relax` job
(env conflicts). The one catpath-side task: **expose the injection point**
so `relax`/`neb` accept a supplied energy/force/relax provider instead of
always building their own calculator.

## 4. The `pathway` kind

A new kind (handle `pw<id>`), body = the catpath config YAML (the
authoritative intent), reusing the `structure`-adjacent machinery:

- **body/IR:** the config YAML as an embedded chunk (searchable) + the
  harvested `methods.md` as a second chunk (citable). `meta` carries the
  resolved graph: node list (→ structure links), edge list (barriers,
  deltas, low_confidence), backend, catpath version, provenance ref.
- **nodes:** each intermediate is a `structure` ref, linked
  `pathway-node`→structure (reserved relation, ADR 0027 style). Nodes
  cross-link, embed, and are browsable in `/structure`.
- **edges:** elementary steps as rows (new `pathway_edges` table or
  `meta`) carrying `barrier/std`, `delta_e/std`, `low_confidence`,
  `neb_run_id` → the `struct_frames` band.
- **compute:** a derived job (`catpath_explore`) owned by the pathway ref
  (ADR 0044 compute lane). An intent todo that wants the barriers
  `requested`→links it; `derived_job_succeeded` closes on success, the
  `child-failed` bubble follows on failure. No rotation (artifact-owned).
- **provenance:** `config.snapshot.yaml` → a `provenance` ref, linked.
- **views:** `view='network'` (DAG), `view='profile'` (energy diagram),
  `view='runs'` (the run-cube rows behind each node), `view='compare'`
  (per-model box plots — a query over run-cube grouped by `model`).

**Alternative considered — no new kind:** model the network with
structures-as-nodes + `link` edges + a `folder` grouping + `struct_measures`
for barriers. Rejected for slice 1: edges carry rich estimate data + band
frames that `links` don't hold, and the config IR + methods + renders need
an owning ref. Revisit if kind-proliferation becomes a concern.

---

## 5. Reuse vs. build

**Reuse (seams already exist):**
- ADR 0044 derived-compute lane: `JOB_PARENT_KINDS`, `requested` link,
  `derived_job_succeeded` evaluator, `child-failed` bubble.
- `struct_runs` run-cube + `cache.run_cache_key` / `structure_sha` content
  addressing + append-only never-invalidated cache-hit logic.
- `struct_frames` (`positions` per step — already spec'd "for MD/NEB").
- `ssh_node` executor + `target_node` GPU pin + lease/heartbeat (already
  covers long GPU jobs).
- `/structure` viewer scaffolding for node inspection; `provenance` kind;
  the citation/draft path for `methods.md`.

**Reuse from catpath (imported, not rebuilt — §3.1):** config schema,
network/intermediate builder, structure/slab builders, `graph`,
`uncertainty.aggregate`, provenance text, and (optionally) `render`. precis
does not reimplement the chemistry.

**Build (precis-side):**
- `Scene.from_ase` ingest adapter (§3.2) — the critical, order-preserving,
  fixed-layer-aware, label-deterministic one; bond-free for slabs. +
  round-trip tests. (precis-side regardless of license — catpath has no
  `Scene`.)
- The **`ComputeBackend` seam inside catpath** (§3.1) + precis's
  implementation of it that mints `struct_relax` jobs — so catpath's relax
  routes through precis's one run-cube.
- `pathway` kind: handler, migration (kind + `pathway_edges`), views, skill.
- `catpath_explore` orchestration: run catpath's pure pipeline in-process,
  dispatch heavy relax via the seam, harvest structures + run-cube rows +
  edges + methods + provenance into the `pathway` ref.
- `model → image/capability` generalization of `struct_relax`'s single-
  image assumption (§3.5) + N cluster backend images + node capability
  advertisement. **(Operational, not legal.)**
- Pooled-uncertainty carrier on pathway node/edge + cite-time propagation
  via `citation.verifier_confidence` (§3.4).
- Cache key that folds **catpath version + backend model-checkpoint
  identity** (§7 trap).
- (later) native SVG re-render of the 3 diagrams (§3.6); native
  `struct_neb` band (§3.3).

---

## 6. The regen model

The config is authoritative; everything downstream is derived and content-
addressed:

- **structure_sha** (`cache.py`) is deliberately *label-free and order-
  independent* — good for reusing a relaxed geometry regardless of how it
  was built. But NEB needs *ordered* endpoints, so the `struct_neb` /
  edge cache key is a **separate, order-sensitive** hash (§3.3). Do not
  conflate them.
- **Edit the config → re-mint `catpath_explore` → regenerate.** Unchanged
  intermediates hit the run-cube by `structure_sha` (zero compute, the way
  the `struct_relax` zero-compute cache hit was proven on prod
  2026-07-01). Only changed/new nodes and their incident edges recompute.
- **Graph restructuring is the messy case.** A config change (different
  surface, `network: auto` re-detecting intermediates) can *add or drop
  nodes*, not just perturb geometries. Regen must diff the new graph
  against the old by `structure_sha`: reuse cached runs for surviving
  nodes, recompute new ones, and decide the fate of orphaned nodes
  (structures dropped from the network — soft-retire the `pathway-node`
  link, keep the structure ref, since it's still a valid structure).

---

## 7. Messiness & open issues

Beyond the four tensions in §3:

1. **Two relaxers — RESOLVED by the seam (§3.1), not a residual.** With the
   `ComputeBackend` seam, catpath's relax/NEB routes through precis's
   `struct_relax` run-cube — one relaxer, one cache, one BFGS driver. This
   messiness only exists in the rejected arms-length model. (If slice 0
   temporarily shells catpath before the seam lands, tag those run-cube
   rows `produced_by='catpath'` so they never collide with precis-driven
   rows; drop the distinction once the seam is in.)
2. **Silent model-update cache trap.** catpath does no hashing; precis
   owns the cache key. It **must** fold catpath version *and* the backend
   **checkpoint identity** (e.g. MACE-MP-0 revision), or a silent weights
   update yields stale cache hits reported as fresh numbers. Backends don't
   always expose checkpoint identity cleanly — this needs per-backend
   probing.
3. **"auto" network detection is heuristic.** RDKit rule-based intermediate
   detection is deterministic only given pinned catpath + RDKit versions.
   Reproducibility of the *graph shape* (not just energies) depends on
   pinning those in the cache key and the container image.
4. **Cost / wall-time.** FAIRChem NEB is minutes–hours per edge; a full
   network is many edges × seeds × models. Leases/heartbeats cover long
   jobs already, but scheduling many heavy jobs needs the load-ceiling +
   `target_node` pinning, and probably a per-pathway concurrency cap.
5. **Egress.** catpath compute needs **no network** (pure numerics on local
   weights) — tighten the container to no-egress, simpler and safer than
   `sandbox_run`'s open-egress agentic containers.
6. **Uncertainty semantics at the boundary.** catpath's `low_confidence`
   is `std > spread_tol OR n < 2`. precis must not silently re-threshold;
   ingest the flag *and* the raw spread so the reader can show both.
7. **Diagram thumbnails need geometry.** catpath's energy-profile PNG
   embeds ASE structure thumbnails. Native re-render (§3.6) reproduces
   those from the ingested `structure` refs — a nice forcing function that
   validates the ingest fidelity (if the thumbnail looks wrong, the
   ingest is wrong).
8. **MCP/agent surface.** How does the LLM drive it? Sketch:
   `put(kind='pathway', config=<yaml>)` auto-dispatches the explore job;
   `get(kind='pathway', view='profile'|'network'|'runs'|'compare')`;
   `edit` the config → regen. Needs a `precis-pathway-help` skill and a
   `precis-toolpath` entry. Cross-model comparison (`catpath compare`) is a
   natural fan-out (one job per backend) + a synthesis render.
9. **Where the interface lives.** catpath is CLI-only; precis supplies the
   interface it lacks — a `/pathway` web reader (energy profile + DAG +
   under-pointer structure popovers) with edit-by-prompt + a regen button,
   reusing the `structure_propose`/`cad_propose` derive pattern. This is
   arguably the single biggest user-facing win and a strong argument for
   the native (data-layer) integration over a black-box wrap.

---

## 8. Slicing

- **Slice 0 — plugin skeleton + in-process EMT. ✅ BUILT + verified.**
  The `precis-catpath` bridge lives in the **catpath repo** as the
  `catpath.precis` subpackage (option A — `pip install catpath[precis]`),
  advertising `precis.handlers` (`pathway = catpath.precis:PathwayHandler`)
  and `precis.migrations` (`catpath = catpath.precis.migrations`). It splits
  into a **precis-free `runner.py`** (imports only catpath: runs `run()` +
  assembles a JSON artifact — graph, `results.json`, `methods.md`,
  per-state extxyz geometries — mirroring `write_outputs` minus matplotlib)
  and a **`handler.py`** (the `pathway` kind: `put` runs catpath on EMT
  in-process and persists a slug-addressed ref + `pathway_body` methods
  chunk + graph/results/provenance in `meta`; `get` renders
  `profile`/`network`/`methods`/`config`; content-addressed regen is a
  cache-hit; `PRECIS_CATPATH_ENABLED` dark-gate → `InitError`). Migration
  `0001_pathway_kind.sql` seeds the `kinds` + `chunk_kinds` rows.
  **Verified** against the precis test DB: put→get→regen-cache-hit→delete
  round-trip + the gated-off path (5 tests green; EMT smoke run ~0.4s).
- **Slice 1a — routing to the pinned node. ✅ BUILT + verified.** The
  `catpath_explore` job type (`catpath.precis.job`, `precis.job_types` entry
  point): `meta.executor='ssh_node'`, `REQUIRES=∅`, `target_node=<node>`;
  its `dispatch` runs catpath **in-process on that node** (catpath[precis]+
  backend are in the node's worker venv) and writes the artifact back onto
  the pathway ref (shared `persist.py`). The handler routes when
  `PRECIS_CATPATH_ROUTE_NODE` is set (mints the job, ref → `status:computing`)
  and runs in-process otherwise. **Precis-core enabler:** `pathway` owns its
  compute job via the new `KindSpec.can_own_jobs` flag (§8b) — no per-`(model,
  seed)` fan-out yet (whole `run()` in one job). **Verified**: dispatch
  write-back + a spark-pinned job minted end-to-end against the test DB.
- **Slice 1b — fan-out + native structures.** (pending) Split the one job
  into per-`(model, seed)` `catpath_explore` jobs (the `catpath _seed` entry,
  §3.8) with per-partial caching + `aggregate_partials`. Build `Scene.from_ase`
  (§3.2) and ingest each relaxed state as a `structure` ref linked
  `pathway-node` (needs the `Relation` core add, §8b). Nodes browsable in
  `/structure`, cross-linkable, citable.
- **Slice 2 — heavy backends.** Per-backend images (MACE, FAIRChem) on the
  GPU node + capability advertisement (§3.5); cross-model `view='compare'`
  (a query over the per-`(model,seed)` partials). Optional saddle/TS
  structure per edge (§3.3).
- **Slice 3 — native re-render + interface.** SVG energy profile / DAG /
  heatmap with under-pointer structure popovers; `/pathway` web reader +
  edit-by-prompt + regen button. Cite-time `low_confidence` badge via
  `citation.verifier_confidence` (§3.4).
- **Slice 4 — unify relaxers (optional).** The deeper `ComputeBackend` seam
  (§3.1/§3.8): route catpath's individual relaxes through precis's
  `struct_runs` run-cube for per-state (not per-seed) caching + one relaxer.
  NEB band → `struct_frames` (§3.3). Only if the per-state cache economics
  justify it.

---

## 8b. Build findings (slice 0)

Discovered while building — feed into slice 1:

- **Closed core registries limit "pure out-of-tree."** precis's plugin
  surface covers kinds/handlers/migrations/job_types/ref_passes, but three
  cross-cutting registries are closed sets a plugin can't extend from its own
  package. Two matter here:
  - **`JOB_PARENT_KINDS`** (compute-lane ownership) — *RESOLVED cleanly.*
    Rather than hardcode `pathway` into core, added a **`KindSpec.can_own_jobs`
    opt-in** (protocol.py) that `JobHandler.put` unions into the allowed
    parent set (via `hub`-registered specs; `_numeric_ref.__init__` now stashes
    `self.hub` so an ad-hoc `JobHandler(hub=…)` can read it). ~10 lines,
    additive, reusable by *any* plugin compute kind. `pathway` sets
    `can_own_jobs=True`. The failure-bubble only special-cases `todo`, so a
    plugin owner flows through the existing compute-lane path unchanged.
  - **`Relation = Literal[...]`** (link vocabulary) — *still open.* Slice 1b's
    `pathway-node` link between the pathway and its `structure` nodes needs
    either a matching core add (Literal member + relations seed) or reuse of
    an existing relation (`derived-from` is the least-bad). The same
    `can_own_jobs`-style extensibility could be applied here later.
  - (`EXECUTOR_PROVIDES` — *not needed*: `catpath_explore` REQUIRES nothing
    and rides `ssh_node`; the `target_node` pin does the routing.)
- **`precis-mcp` on PyPI lags** (8.4.3) behind the deployed source (8.21).
  The `catpath[precis]` extra pins `precis-mcp>=8.21,<9`, so a *fresh* PyPI
  resolve fails. In practice the bridge installs into an env that already
  has precis (it's a precis plugin), where the pin is just a compat
  assertion and resolves fine. If standalone `pip install catpath[precis]`
  is ever wanted, point the extra at a git ref instead.
- **`chunk_kind` is a hard FK to `chunk_kinds.slug`** — the body chunk kind
  (`pathway_body`) must be seeded in the plugin migration, else the body
  insert FK-violates. Done.
- **The test DB is cloned per-session** by precis's conftest, so seeding via
  a *separate* connection to `precis_test` is invisible to the store — apply
  plugin vocab through the store's own connection.

## 9. Decisions needed

1. **License — RESOLVED.** Both GPL-3.0-or-later, same owner; precis imports
   catpath in-process. No relicensing, no arms-length rule. (§3.1)
2. **catpath-side change is tiny (§3.8).** Not the full `ComputeBackend`
   protocol — just a per-seed entry (`catpath _seed <cfg> <seed> --out
   <dir>` writing `partial.json` + `states/*.extxyz`) so precis can fan out
   `run_one_seed` and ingest structures. catpath is *already* orchestrator-
   ready (`run_one_seed` serialisable + `aggregate_partials` pure). The full
   relax-injection seam is deferred to slice 4. Confirm this is the scope.
3. **`Scene.from_ase` — CONFIRMED build.** precis-side, bond-free for slabs,
   order/label-deterministic for NEB endpoints. (§3.2)
4. **NEB — CONFIRMED: no native band for slice 1.** Store barrier/Δe as edge
   numbers + optional saddle structure; band deferred to slice 2+. (§3.3)
5. **New `pathway` kind vs. structures+links+folder** (§4). Recommend new
   kind — it's a migration + handler + skill commitment. Confirm.
6. **Uncertainty carrier:** pathway-local `Estimate` now, propagated to
   cite-time via `citation.verifier_confidence`; a precis-wide `Estimate`
   primitive flagged but deferred. Confirm the scope line. (§3.4)
7. **GPU-node images** (§3.5): which backends on the cluster? MACE-MP-0 is
   the pragmatic default; FAIRChem for adsorbate accuracy. One large image
   each — the remaining *operational* cost.
8. **Diagrams** (§3.6): harvest PNGs first, native interactive SVG later.
   Confirm the ordering.
