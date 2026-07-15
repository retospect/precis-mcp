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
   contract before adding a second engine.
3. **ASKCOS** behind the *same* `route` kind + `job_type` (likely the
   heavier container path) — proves "two engines, one IR."
4. **AlphaFold** as a `protein` kind, in-process on spark (GPU-native),
   reusing the job substrate; converges with `structure` via `Scene`.
5. **Sequence design** (`sequence` kind; ProteinMPNN/RFdiffusion) — another
   `job_type`, GPU on spark.
6. **ChemCrow / agentic** last — in precis this is not a tool but a planner
   coroutine / dream that calls the narrow verbs. Augmentation, not
   foundation.

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

> Status: **specced, not built.** Slice 1b's `parse_aizynth_trees` is a bespoke
> walk of AiZynth's `ReactionTree` dict; this slice replaces per-engine parsers
> with one normalizer so the second engine (ASKCOS, slice 3) reuses it unchanged.

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

**Open questions — resolve at execution (verify from linchemin source, same
discipline that pinned `ReactionTree.to_dict` for 1b):**
- the exact **SynGraph JSON schema** (node/edge shape) `parse_syngraph` walks;
- the exact **facade call signature** + whether translate and descriptors are one
  call or two;
- the **linchemin version** to pin in the image;
- whether descriptors need the **stock/config** (buyable set) — if so, reuse the
  mounted `/models` config.

**Test plan (mirror 1b — fixture-based, gate-green without a cluster):**
- `parse_syngraph` against a captured SynGraph JSON fixture → `RouteGraph` with
  steps + metrics;
- `_run_container` round-trip with a stubbed `RUNNER` that writes a `route.json`
  fixture → dispatch parses via `parse_syngraph` → route written back with
  metrics;
- a `view='metrics'` render test;
- **capture a real SynGraph fixture from the deployed aizynth container** (once
  live) to ground the schema before finalizing `parse_syngraph`.

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
