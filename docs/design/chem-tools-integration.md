# Chemistry & protein tool-packs — integration design

> Design-of-record for folding external chemistry / protein compute
> tools (retrosynthesis planners, AlphaFold, sequence design, …) into
> precis. **Present-tense where built; explicit about what is deferred.**
> Companion to `catpath-integration.md` (the first tool-pack) and
> `sandbox-run.md` (the container-execution substrate). The decisions
> log at the bottom is authoritative.

## 0. The one-line thesis

**precis is already the facade.** The seven verbs (`get / search / put /
edit / delete / tag / link`) + the `kind=` discriminator *are* the stable
protocol boundary a chemistry orchestrator would otherwise need an MCP
broker for. So we do **not** build `chem-routes-mcp` / `chem-analysis-mcp`
/ per-engine MCP servers — that fragments the agent's tool surface, the
exact thing precis's narrow-verb design rejects. Each external tool
becomes:

1. a **kind** — the legible IR the LLM reads (a retrosynthesis `route`, a
   predicted `protein` structure, a `sequence` design spec), and
2. a **`job_type` executor** — the heavy engine, run off the request path
   on a compute node (ADR 0044 compute lane).

The engine is a swappable leaf behind the kind. Adding the 5th tool costs
one Dockerfile stage (or one ansible role) + one `job_type` + one
topology line — never a change to the agent-facing verb surface.

This mirrors the keystone-kind discipline already proven in `structure`
(GPAW/DFT relax on spark), `cad`, and `pcb` (Freerouting): *own a legible
IR, rent the heavy kernel only at job time; the LLM traverses a graph,
never pixels.*

## 1. Architecture at a glance

```
 agent ──put(kind='route', target=SMILES, engine=…)──▶ precis verb surface
                                          │
                        (content-addressed cache hit? ── return route)
                                          │ miss
                        mint kind='job' (job_type='retrosynth',
                        meta.executor, target_node) ── ADR 0044 compute lane
                                          │
   ┌──────────────────────────────────────┴───────────────────────────┐
   │ COMPUTE PLANE — Linux nodes only (spark + any added Linux box)     │
   │                                                                    │
   │  GPU-native, in-process          │  portable CPU, containerized    │
   │  (ansible role installs it):     │  (podman build on the node):    │
   │    • AlphaFold  (already on spark)│    • AiZynthFinder              │
   │    • DFT / MACE (structure kind)  │    • ASKCOS                     │
   │    • GPU seq-design (RFdiffusion) │    • LinChemIn (normalize)      │
   │                                   │    • ChemCrow (agentic)         │
   └──────────────────────────────────┴─────────────────────────────────┘
                                          │
                results (route graph JSON) written back onto the kind's
                chunks + meta; provenance stamped; requester unblocked via
                `requested`→job + `derived_job_succeeded` (ADR 0044).
```

**The Macs orchestrate; they do not run engines.** melchior/caspar/
balthazar are RAM-pressured (they jetsam-cull workers). A container VM on
macOS = Linux-VM overhead + a heavy RDKit/ASKCOS image + jetsam — the
worst place to run these. Engines run on Linux (native podman, no VM, GPU
where present). This already matches reality: AlphaFold and DFT live on
spark.

## 2. The canonical IR — one `route` kind

**Decision: a single canonical `route` kind; engines normalize to it.**
(Not per-engine kinds; not overloading catpath's `pathway`.) AiZynth,
ASKCOS, and any future planner map their raw output into one route-graph
IR: `target`, ordered `steps` (each with reaction SMARTS / template id,
precursors, conditions, references), per-node stock status, confidence,
and provenance. LinChemIn is the **normalizer** — it runs at *route
ingest* (the Marker-analog: raw engine output → normalized chunks), so
"swap the engine, keep the schema" is enforced in one place rather than
hoped for.

The LLM reads the route graph; it never runs a planner in the request
path. Scoring is a `view=` / measure over the stored graph, not a
synchronous engine call.

`protein` (AlphaFold: sequence → predicted structure) and `sequence`
(inverse folding / design: spec → candidate sequences) are **sibling
kinds** on the same substrate. `protein` output is a structure — it can
feed the existing `structure` kind's viewer/IR via the `Scene.from_ase`
path (also catpath slice-1b's next step; nice convergence).

## 3. Plugin, not core — the tool-pack model

**Decision: chemistry/protein ship as plugin tool-packs, not core
kinds.** Precis core stays lean; each domain (retrosynth, protein,
sequence-design) snaps in via entry points, ships dark behind a flag
(e.g. `PRECIS_CHEM_ENABLED`), exactly like catpath's `pathway`. This is
the "kitchen sink you add as the call comes in" model — the alternative
(chem kinds in precis core, like structure/cad) works but means every new
tool-pack edits core.

A plugin tool-pack needs two core seams. **Both are now landed:**

* **`KindSpec.can_own_jobs`** (shipped — catpath spine): lets a plugin
  kind own a derived compute-lane job without a core edit to
  `JOB_PARENT_KINDS`. `JobHandler.put` unions the opt-in kinds.
* **Open relation vocabulary** (shipped — this design's first slice): a
  plugin kind seeds its own link relations (e.g. `consumes` / `produces`
  for route steps, `predicts` for AlphaFold) in its migration. The
  handler-layer `validate_relation` now reads the live `relations` table
  (via `Store.valid_relations()`, cached, refresh-on-miss) instead of
  only the static `Relation` literal — so plugin relations are accepted
  without a core edit. The DB FK stays the durable guard; the literal
  stays the built-in typo-safety hint. See §7.

After these two seams, a tool-pack is pure plugin work: handler +
migration + `job_type` + Dockerfile/role, zero core churn.

## 4. Two engine styles — the dividing line

AlphaFold is already GPU-installed on spark, so we **use it in-process**,
not in a container (containerizing it would need nvidia-container-runtime
for no benefit). That gives a crisp, stable rule:

| Property | In-process (ansible role) | Container (podman) |
|---|---|---|
| Examples | AlphaFold, DFT, MACE, GPU seq-design | AiZynthFinder, ASKCOS, LinChemIn, ChemCrow |
| Install | pip into a node venv (`roles/dft`, `roles/catpath`) | `podman build` the wrapper image on the node |
| Best for | GPU-native, already-on-spark | portable CPU, upstream-maintained env |
| Provenance | resolved venv versions | image digest (cleaner) |
| GPU | native (no plumbing) | needs nvidia-container-runtime (deferred) |

The container style is the **portable default** for new CPU engines; the
in-process style is reserved for GPU-native tools that already live on
spark.

## 5. Container packaging — build-on-demand, no artifact store

**Decision: build the wrapper image on the compute node, install into its
local store, reuse. No tarballs, no registry.**

* Wrapper Dockerfiles live **in precis-mcp** (`docker/`), next to the
  existing `code-task` stage — the precedent for shipping a tool image
  in-repo, built in-place, tagged by git sha, no registry. Each is a thin
  `FROM upstream:tag@sha256:<pinned-digest>` + a small job-runner shim, so
  we inherit the upstream-maintained environment and pin the digest for
  reproducible provenance. Where upstream ships no image (LinChemIn is a
  pip lib) we build a small one ourselves.
* **Each compute node builds the images for its declared capabilities**
  (`podman build`) at **deploy time** — a thin bootstrap step, so no job
  eats a cold multi-minute build; lazy-build stays a fallback. Images live
  in the node's local image store.
* Cost accepted: every node that runs engine X builds X once (duplicated
  build effort across nodes). For a small Linux compute fleet this is
  nothing. A registry earns its keep only when the fleet grows enough that
  whole-image rebuilds hurt — deferred until then.
* **Model weights are not in the image.** AlphaFold params (tens of GB),
  ASKCOS data, etc. are **mounted from the NAS**, content-addressed — the
  corpus-PDF `storage_path` / `pdf_locations` pattern. Image = code+env
  (rebuildable, small-ish); weights = data (mounted, versioned apart).

**Runtime: podman on the Linux nodes.** Rootless, daemonless, no license,
Linux-native, and it matches the `sandbox_run` security posture
(container-user ≠ executor-user). If spark already runs docker, use it —
the executor is OCI-runtime-agnostic (`docker` or `podman`). The
container-runtime install is a **shared prereq with `sandbox_run`** (which
also needs a rootless runtime wired to a locked-down executor user); the
two tracks pay it once. (On an interactive dev Mac, OrbStack is the nicer
DX; podman-machine is the license-free parity option — but engines don't
run on Macs, so this only matters for local iteration.)

## 6. Speed — why the interactive surface stays fast

Two tiers (ADR 0007 / 0044 no-block compute lane):

* **Request path** (agent `put`/`get`): sub-second. It mints a job and
  returns a handle, or returns a **content-addressed cache hit** — input
  hash = target SMILES + engine version (image digest) + model version +
  stock snapshot. Same target twice = zero compute (the structure/DFT
  zero-compute cache hit, already proven on prod).
* **Compute**: offline, async. A CASP search is minutes; AlphaFold longer.
  A caller that wants to block links `requested`→job;
  `derived_job_succeeded` closes it on success, the failure-bubble follows
  the link on failure. First-call latency for an uncached target is real
  (minutes+) — the UX answer is the job handle + auto_check, not a
  synchronous wait, exactly like `plan_tick` / `structure`.

## 7. Repo split — shareable vs fleet-private

* **precis-mcp (shareable, topology-free):** the plugin code (kinds +
  `job_type`s), the wrapper Dockerfiles, the design docs, and a generic
  `docker-compose.yml` that stands up the engines with no reference to
  our hostnames/secrets. Because engines are containers, this compose file
  *is* the shareable install recipe — someone cloning precis can run the
  chem tools without our fleet. This satisfies "so others can use it."
* **`~/work/cluster` (private, never pushes):** inventory, `topology.yml`
  (the single capability→node map — which of *our* nodes runs what),
  secrets, and the playbooks that wire *our* launchd/systemd units +
  install podman + install the GPU-native in-process stack (AlphaFold/DFT
  roles). This *references* the precis-side artifacts but supplies the
  private topology.

This is exactly how catpath split: `catpath.precis` plugin (shareable) vs
`roles/catpath` + `topology.yml` + `44-catpath.yml` (cluster-private). The
refinement here: containerization moves "how to run an engine" *out* of
the private repo into precis, shrinking the private repo to genuinely
machine-specific glue.

## 8. Build order (slices)

0. **Seams (done in this design's first ship):** `can_own_jobs` (catpath
   spine) + open relation vocabulary. Both dark; no consumer yet.
1. **`route` kind + AiZynthFinder**, `job_type='retrosynth'`, containerized
   (wrapper `FROM upstream@digest`) on a Linux node. Prove the compute-lane
   round-trip + content-addressed cache — the structure/DFT loop, for chem.
   **Slice 1a — BUILT** (the `precis_chem` plugin: `route` kind + `retrosynth`
   job + the route-graph IR + a deterministic in-process `stub` engine + the
   content-addressed cache + the requester-blocking wire, all dark behind
   `PRECIS_CHEM_ENABLED`, gate-green without a cluster). **Slice 1b — BUILT
   (precis side; live-run needs a node).** The AiZynth container path:
   `precis_chem.aizynth` (`parse_aizynth_trees` — the `ReactionTree.to_dict`
   mol/reaction walk → `RouteGraph`; `build_aizynth_argv` — the `podman run`
   command line), the `retrosynth` dispatch's `_run_container` branch (stage
   target → `RUNNER`/`STAGER` hooks → parse `trees.json`, the `struct_relax`
   seam, gate-tested with a stubbed runner), and the wrapper `docker/aizynth/`
   (`Dockerfile` `FROM python:3.11-slim` + `pip aizynthfinder`, the
   `precis-aizynth-run` shim → `aizynthcli --config --smiles` → `trees.json`).
   Image = code; the policy/stock **models mount from the NAS at `/models`**,
   not baked. **Remaining (cluster / `~/work/cluster`):** per-node `podman build
   docker/aizynth`, a `config.yml` + model files on the NAS,
   `PRECIS_CHEM_ROUTE_NODE` (+ `PRECIS_CHEM_MODELS_DIR`) on a Linux node, and
   flipping `PRECIS_CHEM_ENABLED`. Until then the stub inline path is the only
   live engine.
2. **LinChemIn normalization at route-ingest** — enforce the one-IR
   contract before adding a second engine. **Slice 2 — BUILT (precis side +
   container; live-run needs the image rebuilt).** LinChemIn runs **inside** the
   aizynth container (its own isolated venv, so its rdkit pin can't fight
   aizynth's): the shim's `az_to_route.py` translates `trees.json`
   (`az_retro`) → SynGraph, extracts engine-agnostic steps + `routes_descriptors`
   metrics, enriches per-step from the raw tree, and emits a precis-canonical
   `route.json`. Precis-side `precis_chem.normalize.parse_syngraph` reads that
   clean JSON (dependency-free); `_run_container` **prefers `route.json`, falls
   back to `trees.json`**. `RouteGraph.metrics` + `get(view='metrics')` surface
   the descriptors — the scoring substrate. Gate-green (real-LinChemIn fixture).
   **Remaining:** rebuild the image on the node (`aizynth_build_image=true`).
3. **ASKCOS** behind the *same* `route` kind + `job_type` — proves "two
   engines, one IR." **Slice 3 — BUILT (precis side; live-run needs an ASKCOS
   deployment).** ASKCOS v2 is a multi-service platform with a Tree-Builder REST
   API (not a CLI), so the engine seam grew a **third transport** (`engine.py`:
   `inprocess`/`container`/`service`, `is_container` back-compat). The dispatch
   branches on it: `_run_service` (`jobs.py`) POSTs the target to the deployment
   (`SERVICE_CALLER` hook, endpoint `PRECIS_ASKCOS_URL` — trusted operator infra,
   SSRF-exempt), extracts the `paths` (`askcos.py`, defensive), and normalizes
   them with a **standalone LinChemIn container** (`docker/normalizer`,
   `--input-format askcosv2`; a service engine has no image to bundle the
   normalizer in) → the *same* `parse_syngraph`. `AiZynthEngine.run_argv`/
   `native_parser` generalized `_run_container` off aizynth specifics.
   **Verified:** the generic normalizer round-trips real LinChemIn output
   (`az_retro`); service dispatch is gate-tested with stubbed `SERVICE_CALLER`/
   `NORMALIZER`. **Remaining (live):** stand up ASKCOS v2, set `PRECIS_ASKCOS_URL`,
   build the normalizer image, and **verify the Tree-Builder request/response
   schema against the instance's `/docs`** (flagged in `askcos.py`).
4. **AlphaFold** as a `protein` kind on spark, reusing the job substrate;
   converges with `structure` via `Scene`.
   **Slice 4a — BUILT (precis side, gate-green without a GPU).** The
   `precis_bio` plugin (sibling of `precis_chem`): the `protein` kind
   (`handlers`→`ProteinHandler`, slug-addressed, `meta.fold` = mmCIF + mean
   pLDDT + pTM/ipTM + sequence, `card_combined` embeds the sequence) + the
   `fold` job_type (`can_own_jobs` compute lane) + a `FoldEngine` port with a
   deterministic in-process `StubFoldEngine` and the `AlphaFold3Engine`
   (de-novo). Dark behind `PRECIS_BIO_ENABLED`; routed by `PRECIS_FOLD_NODE`.
   **Refinement over the original "in-process on spark" plan:** AF3 ships as a
   **container** image (`alphafold3:ready`), not a Python-importable stack, so
   `fold` reuses slice 3's *container* transport (`RUNNER`/`STAGER` hooks,
   `docker run` argv, output parser) rather than an in-process call — the
   always-on workers carry no jax/CUDA. Grounded on the real AF3 v3.0.1 install
   on spark (input JSON dialect / de-novo invocation / mmCIF+summary output
   captured from the working `run_alphafold3.sh`). `mean pLDDT` is read from
   the CIF Cα B-factors by a dependency-free `_atom_site` scan; ptm/iptm from
   `summary_confidences.json`. Verified: stub inline fold + cache hit; the AF3
   container path (input staging → argv → parse → write-back) round-trips with
   a stubbed `RUNNER`; the missing-node / missing-models / no-model failures
   bubble cleanly. **Slice 4b — remaining (live):** `roles/alphafold` asserts
   the `alphafold3:ready` image + models present on spark, wires the fold
   worker env (`PRECIS_FOLD_NODE=spark`, `PRECIS_FOLD_MODELS_DIR`,
   `PRECIS_FOLD_IMAGE`, an XLA cache mount), and un-darks the kind. **Verify at
   the first live run** (flagged in `alphafold.py`, best-effort so it degrades
   rather than crashes): the exact output subdir naming/lowercasing, the
   `summary_confidences.json` key names, and de-novo accuracy vs MSA. **Slice
   4c — later:** `structure` convergence (`cif → ASE → Scene.from_ase`,
   ADR 0043, for a 3D viewer / graph probes) + a ColabFold MSA-mode engine for
   real accuracy.
5. **Sequence design** (`sequence` kind; ProteinMPNN/RFdiffusion) — another
   `job_type`, GPU on spark.
6. **ChemCrow / agentic** last — in precis this is not a tool but a planner
   coroutine / dream that calls the narrow verbs. Augmentation, not
   foundation.
   **Slice 6 — BUILT** (`precis-lab-help` skill): the composition layer, not a
   new framework. The "tools" already exist as kinds (`route`/`protein`/
   `structure`/`paper`) driven by the seven verbs; the skill is the canonical
   **recipes** that chain them into a research loop (plan a synthesis; fold +
   inspect a target; the design loop), for an interactive agent *or* an
   autonomous `plan_tick` working an `LLM:*` todo — the compute lands off the
   tick via the compute lane, so a tick mints then a later tick reads. Indexed
   in `precis-toolpath-help` (Chemistry/biology section) + `precis-overview`.
   The heavier follow-on — a *dedicated* chem/bio `plan_tick` executor that
   auto-drives the loop end-to-end — is deferred (couples to the planner; the
   skill already lets the generic planner do it).

## 9. Known limitations / follow-ups

* **Read-time inverse rewrite for plugin relations.** `Store.links_for`'s
  `relation='cited-by'`→`cites` rewrite is driven by the Python
  `_INVERSE_RELATIONS` dict, which does not know plugin relations. So an
  *asymmetric* plugin relation won't auto-mirror on the read filter. For
  slice 1, plugin relations should be **symmetric**, or the plugin queries
  the stored direction explicitly (or omits the relation filter, which
  returns all edges regardless). Follow-up: source the read-time inverse
  map from the DB `relations.inverse_slug` column so plugin inverse pairs
  work end-to-end. This ship deliberately opens only the *write/validation*
  path — the actual catpath-1b / route-step blocker.
* **nvidia-container-runtime** is not wired; GPU tools stay in-process
  until it is. Wire it once if you want uniform containerization.
* **Registry vs build-on-demand** — revisit if the compute fleet grows.

## 10. Slice 2 — LinChemIn normalization at route-ingest (full spec)

> Status: **BUILT** (precis side + container; live-run needs the image rebuilt).
> Slice 1b's `parse_aizynth_trees` stays as the fallback; the normalized
> `route.json` path is now primary, so the second engine (ASKCOS, slice 3) reuses
> `parse_syngraph` unchanged. See **"As built"** below for the resolved specifics.

**Why.** "One canonical `route` kind" only holds if every engine's output maps to
the *same* IR. A bespoke parser per engine (AiZynth's `ReactionTree`, ASKCOS's
JSON, IBM RXN, …) quietly breaks that. **LinChemIn is the normalizer** — the
route analogue of Marker at paper-ingest: raw engine output → one data model
(SynGraph) → our `RouteGraph`. Enforced in **one place**, so "swap the engine,
keep the schema" is a fact, not a hope.

**What LinChemIn is.** Open-source Python toolkit (`linchemin`, the SynGraph data
model; docs `linchemin.readthedocs.io`). Its **facade** exposes high-level ops:
`translate` (engine format → SynGraph → other formats) and route **descriptors**
(`compute_descriptors`: `nr_steps`, `nr_branches`, branchingness, convergence,
longest-sequence, …). Input formats cover `az` (AiZynthFinder), `askcos`,
`ibmrxn`, `mit`. SynGraph is the working/serializable format.

**Decision — normalize INSIDE the engine container, emit SynGraph.** The engine
image already carries rdkit; LinChemIn needs rdkit too. So add `linchemin` to
each wrapper image and normalize there — the container emits an **already-
normalized** SynGraph JSON (+ descriptors), never raw engine JSON. This keeps
rdkit/linchemin off the always-on precis workers, and collapses the precis-side
parser to **one** engine-agnostic function. (Rejected alternative: normalize
precis-side behind `[chem]` — drags rdkit onto the worker; the container is the
natural home, exactly as Marker runs inside the paper-ingest sandbox.)

**Concrete changes.**
1. `docker/aizynth/Dockerfile` — `pip install linchemin` alongside aizynthfinder.
2. `docker/aizynth/precis-aizynth-run` — after `aizynthcli … → trees.json`, run a
   normalize step (LinChemIn `facade('translate', input_format='az',
   output_format='syngraph')` + descriptors) and write `route.json` (SynGraph +
   metrics) into `/work/out`. Keep `trees.json` too (raw provenance).
3. **New `src/precis_chem/normalize.py`** — `parse_syngraph(content) → RouteGraph`,
   the single normalizer: walk the SynGraph reaction graph into `RouteStep`s and
   fold descriptors into `RouteGraph` (see 5). Supersedes
   `aizynth.parse_aizynth_trees` (keep the latter as a fallback for a raw
   `trees.json` when `route.json` is absent — belt-and-suspenders during rollout).
4. `jobs._run_container` — read `route.json` via `parse_syngraph` (add
   `NORMALIZE_FILE = "route.json"`); fall back to `trees.json` +
   `parse_aizynth_trees` when it's missing.
5. `ir.RouteGraph` — add optional `metrics: dict` (the descriptors). `render()`
   surfaces them; **`get(kind='route', id=…, view='metrics')`** exposes them.
   This is the user's "own scoring" hook: route scoring becomes a **view over
   stored descriptors**, never a synchronous engine call.

**As built (open questions resolved against LinChemIn 3.2.0 by introspection).**
- **Facade signature:** `facade(functionality: str, routes: list, **kwargs) ->
  (result, meta)` — `routes` is positional. Translate + descriptors are **two
  calls**: `facade("translate", trees, input_format="az_retro",
  output_format="syngraph")` → `([BipartiteSynGraph], meta)` (the format string is
  **`az_retro`**, not `az`); `facade("routes_descriptors", [syngraph])` →
  `(DataFrame, meta)`, one row per route with columns `nr_steps`, `nr_branches`,
  `branchedness`, `longest_seq`, `convergence`, `cdscore`,
  `simplified_atom_effectiveness` (+ `branching_factor`).
- **Config gotcha:** linchemin's dynaconf **refuses to import `facade`** until
  `$HOME/linchemin/settings.yaml` exists — the Dockerfile runs `linchemin_configure`
  at build (HOME=/root, matching the container's runtime user). `packaging` is a
  missing transitive dep; pinned explicitly. **linchemin==3.2.0.**
- **rdkit conflict:** aizynthfinder and linchemin both pin rdkit, so linchemin
  lives in an **isolated venv** (`/opt/linchemin-venv`); the shim calls that
  python. No stock/config needed for descriptors (they're graph-structural).
- **route.json is precis-canonical, not raw SynGraph.** The shim owns the
  linchemin-specific code and emits a clean `{schema_version, target, engine,
  engine_version, solved, steps[], metrics{}, score, provenance}` — so
  `parse_syngraph` stays dependency-free and the *same* JSON serves every engine.
  Steps come from an **engine-agnostic** SynGraph walk (CE nodes →
  `get_products()`/`get_reactants()`, target-first BFS from `get_roots()`);
  per-step AiZynth-only fields (confidence, buyable `in_stock`, reaction class,
  template) are enriched from the raw `trees.json` in the shim. Slice-3 ASKCOS
  swaps `input_format="askcos"` + its own enrichment; the extractor + precis-side
  reader are untouched.

**Tests (built, gate-green without a cluster; `tests/test_route_plugin.py`):**
`parse_syngraph` against a **real captured LinChemIn `route.json` fixture**
(`tests/fixtures/chem/aspirin_route.json`) + a hand-built case (field mapping,
`reaction_smiles`→IR); the `metrics` JSON round-trip; `get(view='metrics')` (and
a stub route reporting no descriptors); and three `_run_container` paths — prefers
`route.json`, falls back to `trees.json`, and falls back on a **garbled**
`route.json`.

**Sequencing.** Slice 2 lands **before** ASKCOS (slice 3): once `parse_syngraph`
exists, ASKCOS reuses it unchanged — its container's normalize step just passes
`input_format='askcos'`. No new precis-side parser, ever again.

## Decisions log

* **precis is the facade; no broker/per-engine MCP servers.** Each tool =
  a kind + a `job_type`, behind the seven verbs.
* **One canonical `route` kind**, engines normalize to it via LinChemIn at
  ingest. Not per-engine kinds; not folded into `pathway`.
* **Plugin tool-packs, not core kinds.** Ship dark behind a flag, via entry
  points, like catpath.
* **Two engine styles**, split by GPU-native-in-process vs
  portable-CPU-container. AlphaFold stays in-process on spark.
* **Build-on-demand containers**, no tarball store, no registry (yet).
  Wrapper Dockerfiles in precis; `podman build` per node at deploy. Weights
  mounted from NAS.
* **podman on Linux compute nodes; Macs orchestrate only.** Runtime install
  is a shared prereq with `sandbox_run`.
* **Repo split:** shareable (plugin + Dockerfiles + compose) in precis-mcp;
  fleet-private (inventory + topology + secrets + roles) in `~/work/cluster`.
* **Two core seams landed** to enable all of the above: `can_own_jobs` and
  the open relation vocabulary (`Store.valid_relations()` +
  `validate_relation(store=…)`).
