# precis-dispatch — a swappable compute-runner layer

Status: **draft / for review**
Author: design session 2026-06-21
Scope: precis-mcp (`precis.dispatch` subpackage) + cluster Ansible
Consumers: precis-dft (first), AlphaFold, CFD (designed-for)

> First deliverable of the "run heavy science on the cluster" track.
> Decided up front (2026-06-21): the abstraction lives **in precis-mcp**
> as `precis.dispatch`; container images are distributed via a **local
> registry on caspar** backed by `/opt/nfs/registry`; GPAW targets the
> **GPU** on spark (CPU-functional in the same image as a fallback);
> jobs run as one shared **`precis-compute`** cluster user (not
> per-workload).

---

## Build now (Phase 1) — the whole of it

Deliberately tiny. The entire near-term deliverable:

1. **One job_type, `gpaw_relax`** (precis-dft), whose `run()`:
   stages an input dir (POSCAR + params) to NFS → `ssh spark docker
   run --gpus all -v <nfs>:/work <image> …` → streams logs to a
   `job_event` chunk → collects outputs from NFS → writes the
   `dft_calculation` record.
2. **One image** `precis-dft` (CUDA-13 base, GPAW GPU build with CPU
   fallback, ASE, precis-dft), built on spark.
3. **PAW datasets** mounted read-only from one NFS dir.
4. **Ansible:** the `precis-compute` user; mount NFS on spark;
   build/load the image.

That's it — ~1 job_type + 1 Dockerfile + 1 Ansible role. **No** Runner/
Stager ABCs, **no** `WorkloadSpec` dataclass (plain job-row params),
**no** SLURM/AWS/cost/shape/estimator/registry-service. Spark is
hard-wired as the target.

> **Everything below §1 is forward-looking design intent — NOT a build
> list.** It maps where this goes once a *second* backend (SLURM/AWS)
> or a *second* workload (AlphaFold/CFD) actually forces the
> abstraction. Per the YAGNI review (2026-06-21): interfaces extracted
> from one example are guesses. Build `gpaw_relax` concretely; extract
> `Runner`/`Stager`/`WorkloadSpec` on the **second real case** (rule of
> three). Expect the eventual interfaces to differ from the sketches
> here — that's the point of waiting.

---

## Build now — the LLM's view

The agent never sees `WorkloadSpec`, `Runner`, `Stager`, or anything
in §§3–11. Its entire surface is the existing precis seven-verb tool
set with `kind='job'`. Shipping a relaxation:

```
# 1. Register the structure (precis-dft StructureHandler, content-addressed)
put(kind='structure', poscar='<POSCAR text>')
  → "structure:9f2a… created"

# 2. Submit the relax job
put(kind='job', job_type='gpaw_relax', executor='ssh_node',
    params={
      'structure': 'structure:9f2a…',
      'functional': 'RPBE',
      'kpts': [4, 4, 1],
      'fmax': 0.02,
    })
  → "job:512 queued (executor=ssh_node)"

# 3. Watch it (coordinator runs it on spark, streams job_event chunks)
get(kind='job', id='job:512')
  → "STATUS:running — relax step 14, fmax=0.08"
  → later: "STATUS:succeeded → calc:job_512"

# 4. Read the result
get(kind='dft_calculation', id='calc:job_512', view='scalars')
  → "E_tot=-212.4 eV, converged=True, max_force=0.018"
```

That's the whole agent-facing contract. `params` is validated by the
job_type's `PARAMS_SCHEMA` at submit — a typo is an immediate
`BadInput`, not a queued zombie.

### The tool surface is free to change — no legacy

Every agent session starts fresh: **the LLM makes no legacy calls.**
The tool surface is therefore *not* an API we must keep
backward-compatible — each release ships the `params` shape that's best
*now*. Two consequences:

1. **No versioning / deprecation machinery.** Rename `kpts`→`kpoints`,
   restructure `params`, split or merge job_types freely; the next
   session just learns the current schema from `precis-job-help`.
   (Reinforces YAGNI — there's no compat layer to build or carry.)
2. **Dispatch internals are invisible to the agent.** It only ever
   issues `put(kind='job', …)`. `WorkloadSpec`/`Runner`/`Stager` can be
   introduced, reshaped, or discarded with *zero* agent-facing impact.
   The agent contract and the execution contract are fully decoupled.

This is the strongest reason the layer below stays deferred: it's
internal plumbing the LLM never touches, so building it early buys no
agent-visible capability — only the concrete `gpaw_relax` path does.

---

## 1. Motivation

precis-dft (and soon AlphaFold, fluid dynamics) need to run heavy,
long-lived compute that does **not** belong inside the precis worker
process. Today the only "where" we have is *local SSH to a node*; we
know we will want others:

- **Local SSH** — what we do now (Docker on spark).
- **SLURM** — the standard HPC scheduler. We don't run it yet, but we
  want a shim so adopting it later is "fill in one class," not a
  rewrite of every workload.
- **AWS** (Batch / spot) — burst to cloud at low-peak prices.

The hard-won lesson baked into this doc: **the runner is going to be
swapped, more than once.** So the workloads must not know how compute
is reached. They declare *what* to run; a dispatch layer owns *where
and how*.

This is not a new paradigm for precis-mcp. The executor layer already
works exactly this way — `src/precis/workers/executors/__init__.py`
says it out loud:

> "a job_type declares what *needs* to happen … an executor declares
> *how* it can happen (`claude_inproc` … future `claude_docker` …
> future `slurm`…)."

`precis-dispatch` is the shared library those anticipated executors
need. `ssh_node` / `slurm` / `aws_batch` become executor **names**;
all three are thin adapters over one dispatch core.

## 2. Goals / non-goals

**Goals**

- One backend-agnostic **WorkloadSpec** every heavy workload emits.
- A **Runner** interface with a stable lifecycle (submit → poll →
  logs → reap/cancel); swapping backends touches one class.
- The **OCI image** as the portability boundary — the one artifact
  that survives a runner swap (Docker today, Apptainer/SIF on SLURM,
  ECR on AWS).
- Clean reuse of the existing executor / capability-negotiation
  machinery for *target selection* (no bespoke scheduler in v1).
- AlphaFold and CFD slot in as additional images + specs, no dispatch
  changes.

**Non-goals (v1)**

- A real scheduler / bin-packer. Target selection is static capability
  matching (see §6). Smart scheduling is a later, additive concern.
- Running Linux/CUDA containers on the Macs (architecturally
  impossible — see §7, the "two lanes" note).
- Migrating AlphaFold in this pass. We design so it *can* move; we
  don't move it now (it isn't even installed on spark yet — only the
  NVIDIA AI Workbench tooling is present).

## 3. The four contracts

WorkloadSpec (what to run), **Runner** (compute transport, §5),
**Stager** (data transport, §8.2), and the **catalog** (what *can*
run — the existing job_type registry, enriched, §8.6).


```
   precis-dft        alphafold        cfd            ← workload producers
   (gpaw_relax,…)    (fold)           (solve)            emit WorkloadSpec
        \               |               /
         ▼              ▼              ▼
   ┌─────────────────────────────────────────┐
   │  WorkloadSpec  (backend-free)            │  §4
   │  image · command · resources · io · env  │
   └───────────────────┬─────────────────────┘
                       ▼
   ┌─────────────────────────────────────────┐
   │  precis.dispatch                         │  §5, §8
   │  Runner ABC: submit/poll/logs/cancel     │  (compute transport)
   │  Stager ABC: stage_in / collect_out      │  (data transport)
   └───┬───────────────┬───────────────┬──────┘
       ▼               ▼               ▼
  LocalSshRunner   SlurmRunner    AwsBatchRunner    ← swappable backends
  (Docker, now)    (sbatch +      (ECR + spot,        each pairs with a
   §5.1            Apptainer)§5.2  later)§5.3          Stager (§8.2)
```

### 3.1 Relationship to the existing executor layer

`precis.dispatch` is a **library**, not an executor. It is *used by*
executors:

| Executor name | PROVIDES (capabilities) | Backend (Runner) | Status |
|---|---|---|---|
| `ssh_node` | `docker`, `cuda`, `nfs`, `paw_datasets` (per host) | `LocalSshRunner` | build now |
| `slurm` | `apptainer`, `cuda`, `nfs`, … | `SlurmRunner` | stub + contract test |
| `aws_batch` | `ecr`, `cuda`, … | `AwsBatchRunner` | stub |

The dispatcher's existing rule — *reject the put if
`REQUIRES ⊄ PROVIDES` or executor ∉ `COMPATIBLE_EXECUTORS`* — gives us
**target selection for free**:

```python
# precis_dft/job_types/gpaw_relax.py (illustrative)
COMPATIBLE_EXECUTORS = frozenset({"ssh_node", "slurm", "aws_batch"})
REQUIRES = frozenset({"cuda", "paw_datasets", "nfs"})
```

Submit with `executor='ssh_node'` today; the same job_type runs under
`slurm` the day that executor's PROVIDES advertises `cuda`+`nfs`. No
change to `gpaw_relax`.

These new executors are **siblings of `coordinator`** in
`src/precis/workers/executors/`, and register in `EXECUTOR_PROVIDES`.
PROVIDES is **per-host** (spark advertises `cuda`; a CPU node does
not), so the table becomes host-aware — see §6.

## 4. WorkloadSpec

A frozen dataclass + JSON Schema (validated at submit, like every
job_type's `PARAMS_SCHEMA`). Backend-free by construction.

```python
@dataclass(frozen=True)
class ResourceRequest:
    """What the workload NEEDS — a small, portable, backend-neutral
    vocabulary. NOT the union of every scheduler's knobs (see §6.2)."""
    gpus: int = 0
    gpu_min_vram_gb: float = 0.0   # 0 = any; else a quantitative floor
    gpu_arch: str | None = None    # optional, e.g. "blackwell" / "hopper"
    cpus: int = 1
    mem_gb: float = 4.0
    walltime_s: int = 3600

@dataclass(frozen=True)
class WorkloadShape:
    """The TEMPORAL shape — orthogonal to footprint (§6.6). Drives the
    dispatch *strategy* (pack vs single, spot vs on-demand)."""
    est_duration_s: float = 60.0   # per-task runtime (refined by estimate_resources)
    cardinality: int = 1           # how many of these tasks (1 … millions)
    resumable: bool = False        # can checkpoint/restart — distinct from interruptible

@dataclass(frozen=True)
class DispatchPolicy:
    """HOW to place — distinct from how big (§6.5).

    Note: `interruptible` (spot eligibility) is normally *derived* from
    shape — spot-unsafe iff (long AND not resumable), see §6.6 — not
    set by hand. Override only for exceptions."""
    interruptible: bool | None = None  # None ⇒ derive from WorkloadShape
    max_cost: float | None = None  # ceiling; defaults from campaign budget
    deadline_s: int | None = None  # spill to paid capacity to meet it
    prefer_local: bool = True      # exhaust free cluster capacity first

@dataclass(frozen=True)
class WorkloadSpec:
    workload: str              # catalog key (= job_type name), §8.6
    image: str                 # registry ref, e.g. "caspar:5000/precis-dft@sha256:…"
    command: list[str]         # argv inside the container
    resources: ResourceRequest = ResourceRequest()
    # opaque per-backend passthrough — used ONLY if that backend is
    # chosen, ignored otherwise. The escape hatch that keeps the core
    # portable without losing backend power (§6.2).
    backend_hints: dict[str, dict] = {}   # {"slurm": {...}, "aws": {...}}
    # placement policy — HOW to place, not how big (§6.5). Cost ceiling
    # defaults from the campaign's existing `budget`.
    policy: DispatchPolicy = DispatchPolicy()   # interruptible, max_cost, deadline
    # data plane — LOGICAL refs, NOT host paths. The Stager (§8.2)
    # materializes them for the chosen backend (mount / archive / object).
    inputs: list[InputRef] = ()     # content-addressed blob OR artifact://job/N/name
    datasets: list[DatasetRef] = () # named+versioned shared assets (PAW, AF-DB), §8.1
    outputs: list[OutputDecl] = ()  # {name, container_path} — collected back as artifacts
    env: dict[str, str] = {}
    # identity / provenance
    run_as: str = "precis-compute"  # shared cluster compute user (§10)
    labels: dict[str, str] = {}  # job_id, campaign_id, workload kind
```

Design points:

- **No host, no node, no "how."** Selection is the dispatcher's job.
- **Requirements, not the union of scheduler knobs.** `ResourceRequest`
  is the small portable set; backend-specific richness goes through
  `backend_hints` (§6.2). The Runner translates portable → native.
- **Data moves via NFS, not the image.** Inputs (POSCAR, PAW datasets)
  and outputs (gpw, logs) are mounts under the shared tree (§8).
  Images stay small and immutable; reproducibility comes from
  `image` digest + `command` + input digests.
- **`labels` thread provenance** so logs/outputs tie back to the
  precis `job` row and the dft campaign that spawned it.

> **PAW datasets are inputs, not capabilities.** Projector
> Augmented-Wave setup files (GPAW's per-element "pseudopotentials"),
> versioned, read-only, ~hundreds of MB — mounted from NFS (§8), pinned
> by version. The boolean `paw_datasets` capability (§6.1) just means
> "this target has the dataset tree mounted"; *which* version is an
> input path, not a capability.

## 5. Runner interface

_⚠ Later — not Phase 1. Build `gpaw_relax`'s ssh/docker path inline
first; extract this interface only when a second backend exists._

```python
class Runner(Protocol):
    def submit(self, spec: WorkloadSpec) -> Handle: ...
    def submit_batch(self, specs: list[WorkloadSpec]) -> Handle: ...  # pack/array, §6.6
    def poll(self, handle: Handle) -> RunState: ...   # PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED
    def logs(self, handle: Handle, *, follow=False) -> Iterator[str]: ...
    def cancel(self, handle: Handle) -> None: ...
```

- `Handle` is an opaque, **serializable** token (so a run survives a
  worker restart — it persists in the job row's
  `meta.dispatch_handle`). For SSH it's `{node, container_id}`; for
  SLURM `{job_id}`; for AWS `{job_arn}`.
- The executor wrapping the runner does the **yield/resume** dance
  that `coordinator` already implements: `submit` → `Yield` with
  `WakeWhen(handle_terminal)`; `wake_runner` re-queues on completion;
  the executor `poll`s, streams `logs` into `job_event` chunks, writes
  the verdict. precis-dft's campaign state machine is *already* built
  around this (`Yield`/`WakeWhen` in `precis_dft.campaign`).

### 5.1 LocalSshRunner (now)

`ssh <node> docker run --gpus all --rm -v <nfs>:<...> <image> <cmd>`.

- Runtime on spark is **Docker 29** (not podman — host_vars is stale)
  with **nvidia-container-toolkit 1.19** → `--gpus all` / CDI works.
- `submit` launches detached (`-d`), returns `{node, container_id}`.
- `poll` = `docker inspect`; `logs` = `docker logs`; `cancel` =
  `docker rm -f`.
- Concurrency/affinity: v1 pins CUDA workloads to spark statically.

### 5.2 SlurmRunner (stub now, real later)

- Generates an `sbatch` script wrapping `srun apptainer run <sif> <cmd>`.
  HPC sites forbid the Docker daemon, so the image is converted
  OCI→SIF (`apptainer pull docker://caspar:5000/...`). Same artifact,
  different runtime — this is the payoff of the image boundary.
- Ships as an interface-conforming `NotImplementedError` + a contract
  test that asserts the WorkloadSpec→sbatch translation shape, so the
  seam is real and exercised before SLURM exists.

### 5.3 AwsBatchRunner (stub)

- Image mirrored to ECR; submit to a Batch queue backed by a spot
  compute environment; `Handle = {job_arn}`. Outputs to S3 or a
  mounted FSx, reconciled back to NFS. Sketched only.

## 6. Capabilities, requirements & right-sizing

Two questions, two mechanisms — don't conflate them.

### 6.1 Two vocabularies: boolean gate vs quantitative fit

- **Boolean capabilities** — *does the target have the thing?*
  `cuda`, `nfs`, `paw_datasets`, `docker`. This is the existing
  `REQUIRES ⊆ PROVIDES` gate. `EXECUTOR_PROVIDES` becomes
  **host-aware**: `ssh_node@spark` provides `{docker, cuda, nfs,
  paw_datasets}`; `ssh_node@caspar` provides `{docker, nfs}` (CPU
  only). Resolved from inventory/host_vars at worker boot, not a
  static dict. A `gpaw_relax` with `REQUIRES={cuda,…}` can only be
  placed where `cuda` is provided → spark.

- **Quantitative resources** — *does the target have enough?* Compare
  the workload's `ResourceRequest` (§4) against each target's
  **`Resources`** profile (VRAM, RAM, cores, max walltime). This is a
  new ≥-comparison layered on top of the boolean gate.

So placement = `caps_satisfied(REQUIRES, PROVIDES) AND
resources_fit(request, node.Resources)`.

### 6.2 We wrap a *small* vocabulary + an escape hatch

We do **not** wrap the union of SLURM and AWS knobs — that's a leaky
abstraction that only grows. Instead:

- Portable `ResourceRequest`: `gpus`, `gpu_min_vram_gb`, `gpu_arch?`,
  `cpus`, `mem_gb`, `walltime_s`.
- Opaque `backend_hints` for the tail.

Each Runner **translates** portable → native and merges its hint blob:

| ResourceRequest | SLURM | AWS Batch | Docker (ssh) |
|---|---|---|---|
| `gpus=N` | `--gres=gpu:N` | `resourceRequirements GPU=N` | `--gpus N` |
| `gpu_min_vram_gb` / `gpu_arch` | `--constraint=<feature>` | instance-type filter | (node fixed) |
| `mem_gb` | `--mem` | `MEMORY` | `--memory` |
| `cpus` | `--cpus-per-task` | `VCPU` | `--cpus` |
| `walltime_s` | `--time` | attempt timeout | client-side kill |
| `backend_hints["slurm"]` | merged raw (`--partition`, …) | — | — |

### 6.3 Target capabilities: known, not guessed

We never guess what a *target* offers — it's declared or introspected:

- **Our boxes:** already in `host_vars` (spark = GB10, 124 GB unified,
  20 cores). Authoritative; the dispatcher reads it.
- **SLURM:** `sinfo`/`scontrol show node` → gres + features + memory.
- **AWS:** instance-type capabilities are a published catalog.

A target's `Resources` profile is registered at boot (and re-probed
periodically). Matching is deterministic.

### 6.4 Workload requirements: estimate → calibrate → learn

_⚠ Later. Phase 1 = generous default + bump-on-OOM. The estimator and
learned profiles earn their keep only if that proves too coarse._

The genuinely-unknown side is *how big a given job is*. Blind LLM
guessing is the wrong tool; the system should right-size itself, in
layers:

1. **Analytic estimate first.** DFT scaling is semi-predictable —
   GPAW memory/time scale with atoms × bands × grid-points ×
   k-points. precis-dft computes `estimate_resources(structure,
   params) → ResourceRequest` from the cell/electron count. Covers the
   common case without a probe.
2. **Calibrate the tail with a mini-experiment.** When the estimate is
   shaky, submit a **probe**: short-walltime / few-SCF-step run that
   measures actual peak VRAM + time-per-step, then extrapolate and
   right-size the real submission. A probe is just another child job —
   the campaign coordinator already spawns/waits on children.
3. **Persist → a learned model.** Record measured `(peak_mem,
   walltime)` keyed by `(workload kind, size bucket, settings)`.
   Similar future jobs skip the probe and read the profile. Fits the
   precis "remember everything" grain (a `calc`/`finding`-style row).
4. **Failures are measurements.** OOM / walltime-kill bumps the
   estimate and retries. `precis_dft.retry` **already has a
   `FailureMode` enum** with OOM + walltime entries — so resource
   right-sizing and the retry ladder are *one loop*: classify failure
   → adjust `ResourceRequest` → resubmit. Not two systems.

The LLM sets the goal and may override any number; it does not invent
"32 GB, 12 h" from nothing.

### 6.5 Cost & policy — fit isn't enough for elastic backends

_⚠ Later — pure AWS-future; nothing to honor until AWS is wired._

On our fixed boxes marginal cost ≈ 0, so "fit" is the whole story. On
AWS it isn't: among instance types that satisfy the `ResourceRequest`,
you want the cheapest one *available now* at spot/market prices, which
move constantly.

- **Delegate market optimization to AWS Batch.** Hand a compute
  environment the *set* of satisfying instance types, run it `spot`
  with `SPOT_PRICE_CAPACITY_OPTIMIZED` and a max-%-of-on-demand cap;
  AWS picks the cheapest available pool. We don't hand-pick a type
  per job — we constrain the set and the ceiling.
- **A policy axis we own** (distinct from resources — it's *how to
  place*, not *how big*): `interruptible` (tolerates spot reclamation?)
  + a cost ceiling. GPAW relaxations checkpoint → `interruptible=True`;
  jobs that don't set it `False` and never land on spot.
- **The ceiling already exists:** `dft_campaign` params carry a
  `budget` (`gpu_hours`, `wall_seconds`). That budget *is* the dispatch
  cost ceiling — one concept, not two.

Default policy: **prefer free local capacity; spill to paid spot only
when local is saturated or a deadline demands it, and only for
interruptible jobs.**

### 6.6 Workload shape — granularity drives strategy

_⚠ Later — no micro-task-at-scale workload exists yet; DFT is
macro/few. Add when one appears._

Footprint (how big, §4/§6.1) and **shape** (how long × how many ×
resumable, `WorkloadShape`) are orthogonal, and shape usually dictates
the dispatch *strategy* more than footprint does. The hinge is
**per-dispatch overhead** (container / `sbatch` / Batch launch ≈
seconds–minutes): noise for a week-long job, fatal for a 5 s one.

| Shape | Strategy |
|---|---|
| micro (≤ s) × huge (10⁶) | **never one-dispatch-per-task** — pack into array jobs / a persistent worker-pool draining a queue; one dispatch covers thousands |
| medium (min–hr) × moderate | one dispatch per task; spot if resumable |
| macro (hr–days) × few, resumable | one dispatch; spot OK (loss ≤ checkpoint interval); reserved capacity nice |
| macro × few, **non-resumable** | **on-demand / reliable node only — never spot, never a rebootable box** |

**Spot eligibility is computed, not configured.** Expected loss on
reclaim ≈ time-since-checkpoint, so the only spot-unsafe quadrant is
*long AND non-resumable*:

```python
spot_ok = not (shape.est_duration_s > LONG_S and not shape.resumable)
# DispatchPolicy.interruptible overrides only as an explicit exception
```

**API consequence:** dispatch needs a **batch-submit** path
(`submit_batch(specs)`), not just `submit(one)`. A producer emitting a
million micro-tasks hands dispatch the batch; dispatch owns packing /
array / pool. Single-submit is batch-of-one. The `Runner` interface
(§5) grows `submit_batch`; `LocalSshRunner` packs into a bounded
worker pool, `SlurmRunner` into an array job, `AwsBatchRunner` into a
Batch array.

`WorkloadShape` is declared on the catalog `WorkloadProfile` (§8.6) —
it's usually a property of the *workload type* — with `est_duration_s`
refined per-instance by `estimate_resources` (§6.4).

### 6.7 Placement brain (later)

v1 placement is static capability+fit matching. **balthazar** is
already `node_role: scheduler` — the natural home for a real
queue/bin-packer when contention warrants it. Additive; producers
don't change.

## 7. Cluster topology — and the two-lane reality

Four boxes:

| Host | OS / arch | Role | Compute lane |
|---|---|---|---|
| spark | Ubuntu 24.04 / **aarch64** | inference | **Docker + CUDA** (GB10) |
| caspar | macOS | data / NFS server | CPU (Docker?) |
| balthazar | macOS | scheduler | CPU |
| melchior | macOS / M2 Ultra | gateway | **MPS via alchemi (native)** |

**Critical constraint:** the Macs cannot run Linux/CUDA containers.
So there are *two* execution lanes, and dispatch must not pretend
they're one pool:

1. **Container lane** (Linux): WorkloadSpec + Docker/Apptainer/ECR.
   GPU work pins to spark.
2. **Native-Mac lane** (MPS): melchior's `alchemi` venv, run natively.
   This is a *different* runner (`MacNativeRunner`, future) or simply
   stays the existing alchemi path. ML-potential scoring that wants
   Apple-silicon throughput lives here; GPAW does not (no Mac GPAW).

v1 implements only the container lane. The spec is lane-agnostic so a
Mac-native runner can be added without touching producers.

## 8. Data plane: datasets, inputs, outputs, staging

The core must not know what "PAW" is. It knows three logical data
shapes, and a **Stager** moves them for whatever backend won.

### 8.1 Datasets — generalize PAW

A *dataset* is a named, versioned, content-addressed, **read-only**
asset shared across jobs. PAW is just one:

```python
DatasetRef("gpaw-paw", "0.9.20000")       # hundreds of MB
DatasetRef("alphafold-db", "2.3-reduced") # MULTI-TERABYTE
DatasetRef("openfoam-meshes", "…")        # CFD
```

The **scale spread is the design driver**: kilobyte POSCARs ↔
terabyte AlphaFold DBs. Datasets are *mounted/pre-staged once* and
never packed per-job; only small per-job inputs ever travel as
archives. A `datasets[]` entry resolves to a read-only mount
(shared-FS) or a pre-pulled cache (remote/cloud); the boolean
`paw_datasets`-style capability (§6.1) just asserts "this target has
that dataset tree available."

### 8.2 The Stager — data transport, parallel to the Runner

_⚠ Later. Phase 1 is one bind-mount of NFS (spark shares our FS).
Archive / object modes arrive with the first non-shared-FS backend._

`WorkloadSpec` names *logical* inputs/outputs, never host paths. A
Stager materializes them, picked by backend:

```python
class Stager(Protocol):
    def stage_in(self, spec: WorkloadSpec) -> Mounts: ...   # → concrete paths/binds
    def collect_out(self, spec, handle) -> list[Artifact]: ...
```

| Mode | When | Mechanism |
|---|---|---|
| **shared-FS mount** | target shares our NFS (ssh on our cluster) | bind-mount, zero-copy — big datasets + our boxes |
| **archive** | no shared FS (remote SLURM, AWS) | `tar.zst` the *small* per-job inputs → push (scp/S3) → unpack in scratch → run → pack outputs → pull back |
| **object store** | cloud big/shared blobs | S3/registry, CSI mount or pre-pull |

So: **archives (zip/tar.zst) for the small per-job payload when there
is no shared filesystem; mounts for the big datasets, always.**
Producers don't change when transport does — same principle as the
Runner.

### 8.3 Tidy inputs — a typed contract

Every input is one of three structured things, validated at submit
(extends each job_type's existing `PARAMS_SCHEMA`):

- **params** — scalars/JSON, jsonschema-validated (exists today).
- **inputs** — content-addressed blobs (POSCAR…) **or** an
  `artifact://job/<N>/<name>` ref to a prior job's output (this is how
  a **DAG** forms: B consumes A's relaxed structure).
- **datasets** — the named/versioned refs of §8.1.

Content-addressing buys dedup + reproducibility + clean staging keys
in one move — precis-dft already does this with `structure:<sha>`;
we generalize it. "Tidy" = no input is an ad-hoc host path; it's a
hash or a typed artifact ref.

### 8.4 Outputs — the to-and-fro loop closes

A workload declares **named outputs** (`OutputDecl(name, container_path)`).
The Stager collects them, content-addresses them, registers each as an
**artifact** linked to the job row, and they become consumable inputs
downstream. Same artifact registry both directions.

### 8.5 The shared-FS Stager's tree (our cluster)

`/opt/nfs` on caspar is the export root (`nfs_export_path`),
caspar-local disk, exported to Linux clients:

```
/opt/nfs/
  registry/                       # registry:2 storage backend (§9)
  datasets/<name>/<version>/      # read-only shared assets (gpaw-paw/, alphafold-db/, …)
  artifacts/<sha256>/             # content-addressed inputs + collected outputs
  scratch/<job_id>/               # per-job working dir (ephemeral)
```

Note this is **workload-agnostic** now — no `dft/` branch; GPAW's PAW
lives at `datasets/gpaw-paw/0.9.20000/` like any other dataset.

**Open infra item:** on spark, `/shared` exists but is **not currently
NFS-mounted** (only `/nas` → finnmaccool is live). Before Phase 1 the
`nfs_client` role must actually mount caspar's export on spark.
Recommend fixing `/shared` (the documented Linux `shared_mount`,
backed up with `/opt/nfs`) rather than overloading `/nas`.

### 8.6 Workload catalog — "what can run"

Already solved infra, not new: the **`precis.job_types` entry-point
registry** (`JobTypeSpec`, surfaced via `precis-job-help`). precis-dft
/ AlphaFold / CFD register their job_types as plugins; discovery is
`entry_points(group='precis.job_types')`.

What's added is **dispatch metadata** per entry — a `WorkloadProfile`:

```python
@dataclass(frozen=True)
class WorkloadProfile:
    image: str                          # which container
    datasets: list[DatasetRef]          # what shared assets it needs
    shape: WorkloadShape                # temporal shape → strategy (§6.6)
    estimate_resources: Callable        # structure/params → ResourceRequest (§6.4)
```

A *workload* is then exactly "a job_type that emits a WorkloadSpec."
One introspectable, plugin-extensible list — no second catalog to keep
in sync.

## 9. Image build + registry

- **Registry:** `registry:2` on **caspar**, storage at
  `/opt/nfs/registry`. The registry process runs *on* caspar, so that
  path is local disk for it (no NFS-over-network/locking penalty);
  other nodes pull over plain HTTP; it lands inside the tree the
  `backups` role already covers. (Caveat sidestepped: never point a
  registry storage driver at an NFS *client* mount — only at the
  server's own disk, which is what this is.)
- **Build host:** spark — the image must be **aarch64**, and spark is
  the only Linux/ARM box. Build there, `docker push caspar:5000/...`.
- **Tagging:** `precis-dft:cu13-<gitsha>` plus a moving `:latest`.
  WorkloadSpec pins the **digest** for reproducibility.
- **Image contents (`precis-dft`):** CUDA 13 base (aarch64) → apt
  `libxc-dev libopenblas-dev libopenmpi-dev` → build GPAW with GPU
  (CuPy) **and** verify it imports CPU-only, so a GPU-build failure
  still yields a working pipeline → `pip install ase precis-dft`.
  PAW datasets are **not** baked in — mounted from NFS (§8) so the
  image stays small and dataset version is swappable.

## 10. Identity

One shared **cluster compute user**, not per-workload (these are all
first-party, trusted workloads on a private tailnet — per-workload
users proliferate for no benefit):

- A single **`precis-compute`** system user (fixed UID, e.g. 810,
  `/usr/bin/false`, `home=/dev/null`) in `system_users`
  (`group_vars/all/main.yml`), matching litellm/ollama/etc. **All**
  workloads — GPAW, AlphaFold, CFD — run as this user. Not `deploy`
  (don't run heavy compute as the admin/deploy account).
- A matching **`precis-compute`** group owns NFS `artifacts/` +
  `scratch/`; the precis worker (`deploy`) joins it to reap outputs.
- **In-container identity:** run as the mapped `precis-compute` uid,
  not root.
- **If real isolation is ever needed** (untrusted code, per-workload
  quotas), split into per-workload users *then*. YAGNI now.

## 11. Provisioning (cluster repo, `~/work/cluster`)

Ansible work, separate from this doc's code but pinned here so it's
not forgotten:

- `roles/users` — add the shared `precis-compute` user + group.
- `roles/registry` (new) — `registry:2` on caspar at `/opt/nfs/registry`.
- `roles/dft` (new) + `playbooks/41-dft.yml` — on spark: ensure Docker
  + nvidia CDI, fetch/pin the `gpaw-paw` dataset to NFS
  `datasets/`, build/pull the image, drop a thin run-wrapper.
  Targets `inference` group.
- `roles/nfs_client` — fix the `/shared` mount on spark (§8).

(CLAUDE.md in precis-dft points at `../precis-mcp/cluster/` for this —
**that path is wrong**; the Ansible lives at `~/work/cluster`. Worth a
one-line fix in precis-dft's CLAUDE.md.)

## 12. Phased plan

- **Phase 1 — DFT end to end on spark (the "Build now" box).** A
  concrete `gpaw_relax` job_type: stage to NFS → `ssh spark docker
  run` → collect → write `dft_calculation`. The `precis-dft` image
  (GPU+CPU). Spark hard-wired. **No** dispatch abstraction — just the
  one working path. This is the only committed scope.

Everything below is **"when X is real, do Y"** — not scheduled, not
to-be-stubbed in advance:

- **When a 2nd backend or workload appears → extract the seam.** Only
  then do `WorkloadSpec` / `Runner` / `Stager` get pulled out of the
  concrete `gpaw_relax` code, shaped by *two* real cases. (Don't write
  `SlurmRunner`/`AwsBatchRunner` stubs or contract tests before this —
  the contract from one example is a guess.)
- **When OOM/walltime bites → the right-sizing loop** (§6.4): start
  with a generous default + bump-on-failure via the existing
  `FailureMode` retry; add the analytic estimator / learned profiles
  only if the simple loop proves insufficient.
- **When AWS is actually wired → cost/policy/spot** (§6.5).
- **When a micro-task-at-scale workload appears → shape/batching**
  (§6.6).
- **When images must leave spark → the caspar registry** (§9); until
  then `docker build` on spark and run locally is enough.

## 13. Risks / open questions

1. **GPAW GPU on Blackwell + CUDA 13 + aarch64** is bleeding edge —
   CuPy almost certainly needs a source build and may not converge in
   one pass. Mitigated by CPU-functional-in-same-image; tracked as its
   own spike. (Accepted at decision time: target GPU now.)
2. **PAW dataset version pin** — choose a version (e.g. `0.9.20000`)
   and freeze it under `/opt/nfs/dft/potentials/`.
3. **`/shared` not mounted on spark** — must land before Phase 1 (§8).
4. **Registry auth/TLS** — v1 is plain HTTP on the tailnet
   (`insecure_registries`). Fine inside the tailnet; revisit before
   anything leaves it.
5. **Handle durability across worker restarts** — relies on
   persisting `meta.dispatch_handle`; confirm `coordinator`'s
   resume path already round-trips arbitrary meta (it should).

## Appendix A — example: gpaw_relax emits a WorkloadSpec

```python
WorkloadSpec(
    image="caspar:5000/precis-dft@sha256:…",
    command=["precis-dft-run", "gpaw-relax", "--in", "/work/in", "--out", "/work/out"],
    resources=ResourceRequest(            # from estimate_resources(structure, params), §6.4
        gpus=1, gpu_min_vram_gb=40, cpus=8, mem_gb=32, walltime_s=43200,
    ),
    backend_hints={"slurm": {"partition": "gpu"}},   # ignored under ssh_node
    inputs=[
        Mount("/opt/nfs/dft/inputs/job_512", "/work/in", ro=True),
        Mount("/opt/nfs/dft/potentials/0.9.20000", "/opt/gpaw-setups", ro=True),
    ],
    outputs=[Mount("/opt/nfs/dft/outputs/job_512", "/work/out", ro=False)],
    env={"GPAW_SETUP_PATH": "/opt/gpaw-setups"},
    run_as="precis-compute",
    labels={"job_id": "512", "campaign_id": "cmp_7", "kind": "gpaw_relax"},
)
```

Submitted with `executor='ssh_node'` → `LocalSshRunner` →
`ssh spark docker run --gpus all …`. The same spec, unchanged, runs
under `slurm` once that executor advertises `cuda`.
