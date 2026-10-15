---
id: precis-job-compute-help
family: work
title: precis — the job types that run compute or code
summary: the compute-lane job types — struct_relax, pourbaix_bulk, surface_coverage_scan, sandbox_run, and the plugin fan-out lanes (autocatpath, pcb, retrosynth) — with executors, params and the named-list scan recipe pointer
answers:
  - which job types run compute or code, and on which executor?
  - how do I run a relax, a Pourbaix verdict or a coverage scan?
  - how do I run a sandbox build (sandbox_run)?
  - how does a quest get surface-coverage anchors?
applies-to: get/search/put (kind='job')
tags: workflow, verbs
kinds: job
status: active
---

# precis-job-compute-help — relax, Pourbaix, coverage scans, sandbox builds

The job types that run real compute or code — split out of
[[precis-job-help]] (submit/poll/cancel and the contract live there).
All of these submit the same way (`put(kind='job', parent_id=…,
job_type=…, params=…)`); what differs is the executor and the params.

## Which job types run compute or code?
## Run a relax, a Pourbaix verdict or a coverage scan

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `struct_relax`      | `ssh_node`      | DFT/ML relax of a `structure` on a GPU node   |
| `pourbaix_bulk`     | `claude_inproc` | Bulk Pourbaix verdict (`dissolved` / `leached` / `transformed` / `oxidised` / `unmatched` / `stable`) for a candidate structure's host phase at a U/pH point and over a window, from Materials Project entries; written to the job's `meta.verdict`. Params: `candidate_ref`, `point` (`{U_RHE, pH}`), optional `window` (`{U_RHE: [lo, hi], pH: [lo, hi]}`), `ion_conc_M` (1e-6), `stability_tol` (0.1 eV/atom), `grid` (5). Fails `config` without `PRECIS_MP_API_KEY`. A bulk verdict is necessary, not sufficient: the surface can still restructure under bias. |
| `surface_coverage_scan` | `ssh_node`  | Surface-Pourbaix anchors + inner CHE sweep (slice 1): one MLIP model's ab-initio-thermodynamics coverage scan of a clean slab (catpath `coverage`: γ(θ) per adsorbate, default H/O/OH at 0.25–1 ML, (111) only), stamped with its footing (`meta.anchor_key`, `model`, `engine_version`, `corrections`), then pooled with every succeeded scan on the same `anchor_key` (one per model) into `meta.che_sweep`: the resting termination along U (V vs RHE), every boundary with `band_propagated` (anchor bars through the CHE; "add an anchor here") and `band_model_form` (spread across models; needs ≥2 scans) kept apart, `needs_anchor` ranked by the first. A pooled model whose recorded correction set is a strict subset of the others' is dropped from the sweep first — a mixed corrected/uncorrected band prices the correction, not the model form; dropped models land in `che_sweep.dropped_uncorrected` and the scan warnings (gr486949). Params: `config` (a catpath config, same shape as the pathway lane; `coverage` block defaults filled), optional `model_index` (0), `force_backend`, `target_node` (the GPU node), `point_U_RHE`, `window_U_RHE` ([-1, 0.5]), `grid` (151), `pH` (7, labels the SHE view only), `sigma_e_eV` (default = `config.search.energy_thresh`), `slab_extxyz` (injected slab, scanned since autocatpath 0.25.0; digest folds into `anchor_key`), `structure_ref` (the slab's structure ref — resolved + exported with `FixAtoms` when `slab_extxyz` is absent; refused if not a (111) slab or nothing frozen). Refuses any facet other than (111). The stored sweep is read as a resting-state validity map by `view='pourbaix'` on `kind='structure'`/`'quest'` and the `/pourbaix/<handle>` page (slice 2). |

## Run a sandbox build — the `sandbox_run` job type

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `sandbox_run`       | `claude_docker` | Run an open-ended coding task (`mode:build`) or re-run a prior build's harvested tarball (`mode:run`, no claude/OAuth) in a throwaway, cgroup-capped container on an `agent_sandbox_host`. **Dark** — the `job_claude_docker` pass runs only where its `service_config` row (seeded from `agent_sandbox_hosts` group membership at deploy) has `prio>=1`; a human operator flips it live via the service-prio command. A put on a host without it queues a job nothing claims. Params: `mode` (`build` default \| `run`), `prompt` (`mode:build`, required), `artifact` (`mode:run` — a prior build's harvested `folder` ref id, required), `precis_access` (`none` default \| `read` — `mode:build` only, a per-run token'd read-only MCP callback, needs the `PRECIS_SANDBOX_READ_MCP` env flag on the daemon), `target_node` (a sandbox host, never melchior), `resources.wall_seconds`. `validate_submit` rejects an unsupported `mode`, `precis_access:read` without `PRECIS_SANDBOX_READ_MCP`, `secrets`, a non-sandbox target, `mode:build` with no `prompt`, `mode:run` with no `artifact`, and (`mode:build` only) a missing `CLAUDE_CODE_OAUTH_TOKEN`. Recurring `mode:run` (e.g. a dated pipeline) just wraps the same params in a `meta.schedule` todo — no special-casing, see `precis-recurring-help`. `image` (`code-task:<git-sha>`, default `code-task:latest` — the ansible play's movable tag; per-job override via `params.image`) is pinned in the launch argv and recorded as provenance in three places: the job's `meta.image`, the terminal `job_summary` text, and the harvest folder's `meta.image`. |

## Compute lane + plugin job types

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `pcb_place` / `pcb_route` | `job_inproc` | The enqueued halves of `put(kind='pcb', args={'op':'place'|'route'})` — placement-only anneal, resp. joint place+sketch anneal + realize checkpoint (see `precis-pcb-route-help`) |
| `retrosynth`      | `ssh_node`      | Plan a retrosynthesis route to a target molecule on the compute lane (see `precis-rxn-help`) |
| `autocatpath_seed` | `ssh_node`     | One `(model, seed)` unit of an autocatpath pathway exploration; stashes the JSON partial for the sibling aggregate (see `precis-pathway-help`) |
| `autocatpath_aggregate` | `ssh_node` | Fold the seed fan-out's partials into the pathway's graph + pooled-uncertainty result on the pathway ref |
| `autocatpath_explore` | `ssh_node`   | Legacy single-shot autocatpath run — still registered so pre-existing queued rows don't error-loop; nothing new mints it (superseded by the seed/aggregate fan-out) |

(More land as new modules under `precis/workers/job_types/`. See
the per-type recipe skills for invocation details.)

## How does a quest get surface-coverage anchors?

Flag the quest `meta.surface_anchors: true`. Each tick then mints one
`surface_coverage_scan` per live candidate that reached the `neb` rung
of the fidelity ladder or sits on the merged Pareto front
(`precis.quest.surface_anchors`), its exported slab injected as
`slab_extxyz`; the harvest stamps `anchors_job_id`, `anchor_key`,
`anchors_footing`, `resting_termination_at_point`, `needs_anchor_top`
and a deferred `theta0_check` onto the candidate meta. `U_RHE`/`pH`/
`window.U_RHE` come from `meta.operating_conditions` when set; a
named-list recipe lives in `precis-pathway-help`.

## See also

- [[precis-job-help]] — submit, poll, cancel; the parent-todo contract
- [[precis-pathway-help]] — the coverage-scan recipe and pathway lane
- [[precis-pcb-route-help]] — the pcb place/route lane
- [[precis-rxn-help]] — the retrosynthesis lane
