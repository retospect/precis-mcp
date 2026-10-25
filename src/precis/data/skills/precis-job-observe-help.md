---
id: precis-job-observe-help
family: work
title: precis — compute job types and the fleet /-views
summary: the compute-lane job types (struct_relax, pourbaix_bulk, surface_coverage_scan, sandbox_run, pathway/pcb/retrosynth lanes) plus the read-only /… views (/logs, /limits, /delta, /builds, /throughput, /cadence) on kind='job'
answers:
  - which job types run compute or code, and on which executor?
  - how do I run a relax, a Pourbaix verdict or a coverage scan?
  - how do I run a sandbox build (sandbox_run)?
  - how does a quest get surface-coverage anchors?
  - how do I read worker_logs / what is the cluster doing right now?
  - is the external rate limiter pacing a provider?
  - what changed since the last doctor tick?
  - has a fix reached the fleet yet / which build is each process on?
  - is the fleet working or idle?
  - when does a cadence fire next?
applies-to: get/search/put (kind='job')
tags: workflow, verbs, troubleshooting
kinds: job
status: active
---
# precis-job-observe-help — compute lanes and the read-only `/…` views

The satellite reference for `kind='job'`: the job types that run real
compute or code, and the `/…` introspection views answering 'what is
the fleet doing'. Submit/poll/cancel and the parent-todo contract
live in [[precis-job-help]] — every job type below submits the same
way (`put(kind='job', parent_id=…, job_type=…, params=…)`).

## Which job types run compute or code?
## Run a relax, a Pourbaix verdict or a coverage scan

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `struct_relax`      | `ssh_node`      | DFT/ML relax of a `structure` on a GPU node   |
| `pourbaix_bulk`     | `claude_inproc` | Bulk Pourbaix verdict (`dissolved` / `leached` / `transformed` / `oxidised` / `unmatched` / `stable`) for a candidate structure's host phase at a U/pH point and over a window, from Materials Project entries; written to the job's `meta.verdict`. Params: `candidate_ref`, `point` (`{U_RHE, pH}`), optional `window` (`{U_RHE: [lo, hi], pH: [lo, hi]}`), `ion_conc_M` (1e-6), `stability_tol` (0.1 eV/atom), `grid` (5). Fails `config` without `PRECIS_MP_API_KEY`. A bulk verdict is necessary, not sufficient: the surface can still restructure under bias. |
| `surface_coverage_scan` | `ssh_node`  | Surface-Pourbaix anchors + inner CHE sweep (slice 1): one MLIP model's ab-initio-thermodynamics coverage scan of a clean slab (catpath `coverage`: γ(θ) per adsorbate, default H/O/OH at 0.25–1 ML, (111) only), stamped with its footing (`meta.anchor_key`, `model`, `engine_version`, `corrections`), then pooled with every succeeded scan on the same `anchor_key` (one per model) into `meta.che_sweep`: the resting termination along U (V vs RHE), every boundary with `band_propagated` (anchor bars through the CHE; "add an anchor here") and `band_model_form` (spread across models; needs ≥2 scans) kept apart, `needs_anchor` ranked by the first. A pooled model whose recorded correction set is a strict subset of the others' is dropped from the sweep first — a mixed corrected/uncorrected band prices the correction, not the model form; dropped models land in `che_sweep.dropped_uncorrected` and the scan warnings (gr486949). Params: `config` (a catpath config, same shape as the pathway lane; `coverage` block defaults filled), optional `model_index` (0), `force_backend`, `target_node` (the GPU node), `point_U_RHE`, `window_U_RHE` ([-1, 0.5]), `grid` (151), `pH` (7, labels the SHE view only), `sigma_e_eV` (default = `config.search.energy_thresh`), `slab_extxyz` (injected slab, scanned since autocatpath 0.25.0; digest folds into `anchor_key`), `structure_ref` (the slab's structure ref — resolved + exported with `FixAtoms` when `slab_extxyz` is absent; refused if not a (111) slab or nothing frozen), `seed` (the engine's random draw — site order + adsorbate rattle — passed as `scan(seed=)`, recorded as `meta.seed`; a quest retry sets it to its generation so each retry is a fresh start; not part of `anchor_key`; an engine whose scan takes no seed runs unseeded and the event line says so). Refuses any facet other than (111). The stored sweep is read as a resting-state validity map by `view='pourbaix'` on `kind='structure'`/`'quest'` and the `/pourbaix/<handle>` page (slice 2). |

## Run a sandbox build — the `sandbox_run` job type

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `sandbox_run`       | `claude_docker` | Run an open-ended coding task (`mode:build`) or re-run a prior build's harvested tarball (`mode:run`, no claude/OAuth) in a throwaway, cgroup-capped container on an `agent_sandbox_host`. **Dark** — the `job_claude_docker` pass runs only where its `service_config` row (seeded from `agent_sandbox_hosts` group membership at deploy) has `prio>=1`; a human operator flips it live via the service-prio command. A put on a host without it queues a job nothing claims. Params: `mode` (`build` default \| `run`), `prompt` (`mode:build`, required), `artifact` (`mode:run` — a prior build's harvested `folder` ref id, required), `precis_access` (`none` default \| `read` — `mode:script` only, a per-run token'd read-only MCP (get/search/more) on a unix socket the script reaches through `/work/precis_client.py`, needs the `PRECIS_SANDBOX_READ_MCP` env flag on the daemon; `mode:build` refuses it), `target_node` (required — an `agent_sandbox_host`, never melchior), `resources` (required — `{'wall_seconds': <positive int>}`). Minimal build: `put(kind='job', args={'job_type': 'sandbox_run', 'params': {'mode': 'build', 'prompt': '…', 'target_node': '<sandbox host>', 'resources': {'wall_seconds': 600}}})`. `validate_submit` rejects an unsupported `mode`, `precis_access:read` on `mode:build` or without `PRECIS_SANDBOX_READ_MCP`, `secrets`, a non-sandbox target, `mode:build` with no `prompt`, `mode:run` with no `artifact`, and (`mode:build` only) a missing `CLAUDE_CODE_OAUTH_TOKEN`. Recurring `mode:run` (e.g. a dated pipeline) just wraps the same params in a `meta.schedule` todo — no special-casing, see `precis-recurring-help`. `image` (`code-task:<git-sha>`, default `code-task:latest` — the ansible play's movable tag; per-job override via `params.image`) is pinned in the launch argv and recorded as provenance in three places: the job's `meta.image`, the terminal `job_summary` text, and the harvest folder's `meta.image`. |

## Compute lane + plugin job types

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `pcb_place` / `pcb_route` | `job_inproc` | The enqueued halves of `put(kind='pcb', args={'op':'place'|'route'})` — placement-only anneal, resp. joint place+sketch anneal + realize checkpoint (see `precis-pcb-route-help`) |
| `retrosynth`      | `ssh_node`      | Plan a retrosynthesis route to a target molecule on the compute lane (see `precis-rxn-help`) |
| `autocatpath_seed` | `ssh_node`     | One `(model, seed)` unit of an autocatpath pathway exploration; stashes the JSON partial for the sibling aggregate (see `precis-pathway-help`) |
| `autocatpath_aggregate` | `ssh_node` | Fold the seed fan-out's partials into the pathway's graph + pooled-uncertainty result on the pathway ref |
| `autocatpath_kinetics` | `ssh_node` | Kinetics sweep on a CPU worker (the GPU seed lane is never touched): runs the (T, p_total) mean-field grid over a just-aggregated verify-tier pathway and stores `meta.kinetics_sweep` (optimum + competitive coverage map) and the refreshed `meta.kinetics_v2` on the pathway ref. Minted by the `autocatpath_aggregate` of a verify-tier pathway, never by hand — and only while the setting `pathway.kinetics_mint_enabled` is on (off by default), so with it off no kinetics jobs appear |
| `autocatpath_revalidate` | `ssh_node` | Re-validates a stored pathway result from its validation manifest on a CPU worker. It refuses with the mismatch named when the host's lock hash differs. Otherwise it re-runs the kinetics and sweep legs, judges drift per observable and appends the verdict to `meta.revalidation[<manifest_id>]` on the pathway ref. Job meta carries `verdict` and `complete`; `reproduced_partial(...)` with `complete: false` (relax/NEB and TPD not re-run) is not a validation. Only `put(kind='pathway', mode='revalidate')` or a direct job put mints it; nothing auto-mints it. It has no LLM executor |
| `autocatpath_explore` | `ssh_node`   | Legacy single-shot autocatpath run — still registered so pre-existing queued rows don't error-loop; nothing new mints it (superseded by the seed/aggregate fan-out) |
| `hexfold_relax`     | `ssh_node`   | Assembled-hexfold-block relax (hexfold H3): whole-block polish on a relax-ladder rung (`polish`, `ml` default) + `xtb` on H-capped buffer-frozen junction fragments (`fragment`, or `none`); the relaxed geometry becomes the structure's next version and per-mouth/per-seam strain before/after lands on `meta.generated.relax`. Params: `structure_ref_id` (required — a structure minted with junction bookkeeping), optional `on_version`, `polish`/`model`, `fragment`, `buffer_A` (12), `core_A` (4), `steps` (200), `target_node`. See `precis-hexfold-help`. |

## How does a quest get surface-coverage anchors?

Flag the quest `meta.surface_anchors: true`. Each tick then mints one
`surface_coverage_scan` per live candidate that reached the `neb` rung
of the fidelity levels or sits on the merged Pareto front
(`precis.quest.surface_anchors`), its exported slab injected as
`slab_extxyz`; the harvest stamps `anchors_job_id`, `anchor_key`,
`anchors_footing`, `resting_termination_at_point`, `needs_anchor_top`
and a deferred `theta0_check` onto the candidate meta. `U_RHE`/`pH`/
`window.U_RHE` come from `meta.operating_conditions` when set; a
named-list recipe lives in `precis-pathway-help`.

## Read raw worker logs
## What is the cluster actually doing right now?
## Confirm a pass ran / failed without Bash

```python
get(kind='job', id='/logs')
# → last 24h, WARNING+, newest first — the default "what's wrong?" view

get(kind='job', id='/logs?handler=dispatch&since=24&level=WARNING')
# handler= takes the short pass name ('dispatch', 'embed', ...), the full
# dotted logger ('precis.workers.dispatch'), or the pass named in the
# runner's per-cycle `worker: <pass> claimed=N ok=N failed=N` INFO row
# (payload.handler) — so level=INFO shows a quiet pass's heartbeat too

get(kind='job', id='/logs?host=caspar&level=INFO&q=timeout&since=168&page_size=50')
# host=, q= (substring on message), since= (hours, max 168),
# page_size= (default 100, cap 200; limit= the deprecated alias) all compose

get(kind='job', id='/logs?job=jo123')
# job= anchors on that job: its run window +-5 min on the host that claimed
# it (host= overrides); since= is ignored. Errors if never claimed.

get(kind='job', id='/logs?process=precis-worker-agentlane')
# process= is an exact match on the PRECIS_PROCESS env the plist sets
# (NULL unless set); a host with several worker units needs this —
# host= alone can't tell them apart

# top-level q= works too (id's query string wins on conflict)
get(kind='job', id='/logs', q='timeout')
# filters are also real kwargs; since= is hours
get(kind='job', id='/logs', args={'handler': 'dispatch', 'level': 'INFO', 'since': 6})
```

Read-only view over the centralised `worker_logs` table (migration
0015) — the same table an operator reads via `precis logs` on the
CLI. Header line states the resolved filter + UTC cutoff; each row is
`ts host handler LEVEL message`; the trailer says `N rows shown of M
matching`; no other agent surface reads this table (no Bash, no raw SQL).

## Is the external rate limiter pacing a provider?

```python
get(kind='job', id='/limits')
# → per named provider (s2, openalex, crossref, jlcpcb, ...):
#   `provider tokens=<avail>/<cap> refill=<n>/s
#   day=<used_today>/<daily_cap|->`, then `effective:`, `slots`, `state:`.

get(kind='job', id='/limits?provider=openalex')   # pin one row
get(kind='job', id='/limits?hosts=1')             # include host:<netloc> rows
```

Read-only view over `external_rate_limits` (migration 0121,
`precis.utils.rate_limit`) — the cross-host outbound limiter fetchers
like `fetch_oa` go through. `tokens` is the burst balance *right now*
(refill applied, clamped to capacity), so ~0 means the next call waits;
`day` is `used_today / daily_cap` (`-` = no daily quota lane).
`host:<netloc>` rows (per-host pacing, seeded by safe_fetch) hide
unless `hosts=1` or `provider=host:<netloc>`; the trailer is host-row
occupancy.

`effective:` is the resolved `EffectivePolicy`
(`precis.utils.outbound_policy`): `cls`, `rate` (`-` =
rate-less class), `concurrency` (a `resource_slots` resource — `slots`
shows `free/capacity`), `max_wait_s`, `connect_s`/`read_s`/`total_s`,
`on_refuse` (wait/defer/fail), `retry`; `(layer)` names who set each
field (class/code/db/inferred/deadline). `state:` = `parked` (live
Retry-After/429 park), `last_used`, `refused_today`/`last_refused`.

Overrides (operator, no deploy — never hand SQL):
`precis limits set <row> --class|--max-wait|--connect|--read|--total|
--on-refuse|--concurrency RESOURCE|N [--clear FIELD] [--create]
[--dry-run]`; a bare N edits the hold row's `resource_slots` capacity.
A write audits to `worker_logs` and lands fleet-wide in ~60 s (30 s
snapshot TTL).

## What changed since the last doctor tick?

```python
get(kind='job', id='/delta')         # since the last succeeded doctor_tick
get(kind='job', id='/delta?since=24')          # or a window in hours
get(kind='job', id='/delta?since=2026-10-22T06:00:00Z')  # or an ISO cutoff
```

One read that answers "what moved since the last doctor_tick job ran":
`NEW ALERTS` / `REPEAT SIGHTINGS` / `PERSISTENT OPEN` (still open,
nothing new — the baseline-noise candidates) / `NEW GRIPES` /
`GRIPE COMMENTS`, each row naming the ref to drill into. The cutoff
comes from the last *succeeded* `doctor_tick` job's `meta.started_at`
(falls back to the latest finished tick, then to 8h ago). This is the
doctor's gather entry point — the static `/rules` catalogue and
unchanged threads don't need re-reading.

## Has a fix reached the fleet yet?

```python
get(kind='job', id='/builds')             # per host/process build, last 24h
get(kind='job', id='/builds?since=168')   # widen to a week
get(kind='job', id='/builds', args={'sha': '<fix commit>'})
# ...and does each build CONTAIN that commit? adds contains_sha=yes|no|unknown
```

Every claim stamps the claiming worker's `version@sha` on the job
(`meta.lease_code`), so the jobs table records what code actually ran
where. Each row is
`host process version@sha jobs=N last=<UTC>`.

The stamp is taken at claim time — `/builds` shows what a process ran
on its last claim, not what it runs now; an idle lane keeps its
pre-restart build and `last=` still moves (polls update old jobs). An
old build is "no claim since the restart", not "deploy did not land" —
the deploy's post-bounce check (playbook 43) is the authority there.

A fix is live on a process when that process's newest build sha is the fix
or a descendant of it. Do not judge that by eye: pass the fix's commit as
`args={'sha': ...}` (or `id='/builds?sha=...'`) and git answers per row —
`contains_sha=yes` (the build is that commit or descends from it), `no`
(it does not), or `unknown` when no checkout is reachable, a
commit was never fetched, or the row has no build sha. `unknown` is
"cannot tell", never evidence either way. Read per **process**, not per
host (a host can run several units — the `process=` caution above);
two builds in the window is a restart boundary — read the newest.
`precis-status` answers a different question (your own process's build,
which in a container is not the fleet's).

## Is the fleet working or idle?

```python
get(kind='job', id='/throughput')            # last 24h per host/process
get(kind='job', id='/throughput?since=168')  # widen to a week
```

Rows are `host process claimed=N succeeded=N failed=N running=N
last_claim=<UTC>`, from the same lease stamps as `/builds`; hosts that
heartbeat but claimed nothing show `claimed=0`. The trailer gives the queue
depth now by executor. Read the two together: no claims beside an empty
queue is a healthy idle; no claims beside queued work is the fault.
Measurement only — no alert reads it; GPU busy fraction isn't measured.

## When does a cadence fire next?

```python
get(kind="job", id="/cadence/graph_garden")   # interval, next_fire_at, last fire+host
get(kind="job", id="/cadence")                # every scheduler_leases row
```

Each recurring cadence holds a `scheduler_leases` row — the lease clock
the decentralized scheduler claims. `next_fire_at` in the past means the
cadence is due, not broken (any live worker may claim it).

## See also

- [[precis-job-help]] — submit, poll, cancel; the parent-todo contract
- [[precis-pathway-help]] — the coverage-scan recipe and pathway lane
- [[precis-pcb-route-help]] — the pcb place/route lane
- [[precis-rxn-help]] — the retrosynthesis lane
- [[precis-status-help]] — the build serving *your own* process
- [[precis-nursery-help]] — alerts
- [[precis-workspace-help]] — the workspace script loop a `sandbox_run` will execute
