# precis-dispatch — a swappable compute-runner layer

Status: design intent, Phase-1 shipped. Consumers: precis-dft (first),
AlphaFold, CFD (designed-for).

Shipped portion: see the `precis.workers.executors` docstrings and
`workers/job_types/struct_relax.py`; full design sketches (Runner /
Stager / WorkloadSpec, capability vocabularies, cost/policy, staging
tree, image registry) in git history. Phase 1 — DFT end-to-end on
spark (stage to NFS → `ssh spark docker run --gpus all` → stream
`job_event` chunks → collect outputs → write the calculation record) —
is live via the `ssh_node` executor.

Decided up front (2026-06-21): the abstraction lives in precis-mcp;
container images distribute via a local registry on caspar backed by
`/opt/nfs/registry`; GPAW targets the GPU on spark (CPU-functional in
the same image as fallback); jobs run as one shared `precis-compute`
cluster user, not per-workload.

## Open scope — all trigger-gated ("when X is real, do Y")

Per the YAGNI review (2026-06-21): interfaces extracted from one
example are guesses; expect the eventual interfaces to differ from the
git-history sketches — that is the point of waiting.

- **When a 2nd backend (SLURM/AWS) or 2nd workload (AlphaFold/CFD)
  appears → extract the seam.** Only then pull `WorkloadSpec` /
  `Runner` / `Stager` out of the concrete relax code, shaped by *two*
  real cases. Do NOT write `SlurmRunner`/`AwsBatchRunner` stubs or
  contract tests before this.
- **When OOM/walltime bites → the right-sizing loop:** generous
  default + bump-on-failure via the existing `FailureMode` retry;
  analytic estimator / learned profiles only if that proves
  insufficient.
- **When AWS is actually wired → cost/policy/spot handling.**
- **When a micro-task-at-scale workload appears → shape/batching.**
- **When images must leave spark → the caspar registry**; until then
  `docker build` on spark and run locally is enough. Registry v1 is
  plain HTTP on the tailnet — revisit auth/TLS before anything leaves
  it.

## Risks / open questions

1. **GPAW GPU on Blackwell + CUDA 13 + aarch64** is bleeding edge —
   CuPy likely needs a source build. Mitigated by
   CPU-functional-in-same-image; its own spike.
2. **PAW dataset version pin** — freeze one version under
   `/opt/nfs/dft/potentials/`.
3. **Handle durability across worker restarts** — relies on persisting
   `meta.dispatch_handle`; confirm the coordinator's resume path
   round-trips arbitrary meta.
