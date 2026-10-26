---
status: draft
title: "'cpu' capability lane for CPU-only jobs (gr511752)"
pillar: platform
prio: normal
---

# 'cpu' capability lane for CPU-only jobs (gr511752)

Source: gr511752. Reto ruling (gripe comment, 2026-10-24): **wait** —
keep the `precis_suspended_job_types` host_vars stop-gap until a second
CPU-only job type exists, then build option (a) from coordinator-09's
2026-10-24 message: a `'cpu'` capability probe plus a `ServiceSpec`
token, and remove the host_vars hold.

## Motivation / why

Kinetics v2 slice 4 mints an `autocatpath_kinetics` sweep after every
verify-tier aggregate. Spec D9 says it must never run on the GPU seed
lane, but no host advertises a `cpu` slot, and the claim path drops a
requirement no host advertises
(`workers/executors/_common.py::claim_executor_jobs`'s
unadvertised-requires fallback — the same hole the `embedder` probe
docstring calls out), so nothing keeps a CPU-only job off the GPU node.
The stop-gap is `precis_suspended_job_types` in the GPU node's
host_vars — an operator hold that names job types one by one, not a
lane every CPU-only job type inherits.

What exists today, with anchors:

- `workers/capability_probe.py`: `_PROBES[token] -> capacity|None`,
  vocabulary derived from `ServiceSpec.requires`
  (`capability_vocabulary()`); heartbeat writes `resource_slots`.
- `workers/executors/_common.py::effective_requires`: a job's
  `resource_slots` needs come from `SERVICES_BY_NAME[job_type].requires`
  (or explicit `meta.requires`).
- `workers/registry.py::SERVICES` holds `ServiceKind.JOB` rows keyed by
  job_type name (`embed_batch` is the pattern), but no row exists for
  `autocatpath_kinetics` or `autocatpath_revalidate` — they are plugin
  job types (`precis.job_types` entry points in `pyproject.toml`), so
  `effective_requires` returns `{}` for both.
- Stop-gap: `precis_suspended_job_types` (`deploy/group_vars/all.yml`)
  lists `autocatpath_kinetics` on the GPU node's host_vars (overlay).

## In scope

- A `_PROBES['cpu']` entry in `capability_probe.py`. Recommended
  semantics: `PRECIS_CPU_SLOTS` env override (0 = opt out), default
  `os.cpu_count()` — opt-OUT, so the moment `cpu` enters the vocabulary
  every worker host advertises it and only the GPU seed box is pinned
  to 0 via host_vars. A capacity > 0 also bounds CPU-job parallelism
  per host; `os.cpu_count()` over-advertises, so a saner default may be
  `max(1, os.cpu_count() // 2)` — decide in review.
- `ServiceSpec` rows (`kind=ServiceKind.JOB`, `category='jobs'`) for
  `autocatpath_kinetics` — and `autocatpath_revalidate` if the trigger
  question below resolves yes — with `requires=frozenset({'cpu'})`, so
  `effective_requires` reserves a `cpu` slot for them.
- The `precis_suspended_job_types` hold on the GPU node is removed once
  the lane is live (overlay change, not this repo).
- Tests in `tests/test_capability_probe.py` (or a sibling): the `cpu`
  probe honours the env override including 0, defaults to a positive
  capacity, and the full-coverage test keeps passing (a probe-less
  `cpu` token would redden it anyway).

## Explicitly NOT in scope

- A `JobTypeSpec.requires` executor-capability token — that vocabulary
  (`claude_bin` et al.) gates *executors*, not host lanes; the gripe is
  about which host claims the job.
- Claim-path changes: `effective_requires`, `resource_slots` and the
  unadvertised-requires fallback already do the right thing once `cpu`
  is advertised and required.
- Removing the host_vars hold *before* the lane ships, or touching the
  sealed migrations / `resource_slots` schema.
- Routing for jobs that want *either* lane (GPU-or-CPU); this only
  fences CPU-only work off the GPU seed box.

## Deploy ordering (the regression trap)

The env override is read by the probe, so `PRECIS_CPU_SLOTS=0` on the
GPU seed node's host_vars can land in the overlay at any time and takes
effect on the first heartbeat after the probe ships. Removing the
`precis_suspended_job_types` hold *before* `requires={'cpu'}` is live
AND the GPU box advertises `cpu: 0` reopens the hole — worse than
today, because the fallback claims ungated. Order: (1) overlay sets
`PRECIS_CPU_SLOTS=0` on the GPU seed node; (2) ship probe + requires;
(3) confirm the GPU node's `resource_slots` row reads `cpu=0` and a
CPU node's reads `cpu>0`; (4) remove the suspend entry. A host on old
code never advertises `cpu`, so a `requires={'cpu'}` job can never
land on stale code — the failure mode is jobs waiting, not
mis-routing.

## Acceptance criteria

- A `autocatpath_kinetics` job is claimed only by a host whose
  `resource_slots` advertises `cpu > 0`; with every CPU host's slots
  exhausted it waits `STATUS:queued` rather than landing on the GPU
  seed node.
- The GPU seed node's probe reports `cpu: 0` under
  `PRECIS_CPU_SLOTS=0`; a CPU worker reports a positive capacity with
  no env set.
- `capability_vocabulary()` includes `cpu` only via the `ServiceSpec`
  rows — no hard-coded token list grows.
- `precis_suspended_job_types` no longer needs `autocatpath_kinetics`
  on the GPU node (overlay change verified post-deploy).
- `tests/test_capability_probe.py` full-coverage test green; the
  unadvertised-requires sweeper alert stays silent for `cpu`.

## Target + blast radius

- `src/precis/workers/capability_probe.py` (`_PROBES`, `_probe_cpu`).
- `src/precis/workers/registry.py` (two `ServiceKind.JOB` rows).
- `tests/test_capability_probe.py` + claim-path test coverage.
- Overlay (not repo): `PRECIS_CPU_SLOTS=0` on the GPU seed node, then
  drop the suspend hold.
- Post-deploy look: `resource_slots` `cpu` rows per host;
  `autocatpath_kinetics` claim host; sweeper unadvertised-capability
  alert.

## Open questions / decisions log

- **Q1 — Is the trigger met?** `autocatpath_revalidate` (landed
  8743f90d8, 2026-10-24 ~06h before the ruling) is CPU-only in code
  (`REQUIRES=frozenset()`, ssh_node, "GPU seed lane is never claimed"),
  but kinetics-engine-v2's lane column gives its min-energy-path leg to
  the GPU seed lane — Reto may not count it. If it counts, this item is
  unblocked now; if not, it stays snoozed until a second CPU-only type
  genuinely exists. **Needs Reto.**
- **Q2 — Probe default capacity.** `os.cpu_count()` (permissive, counts
  all cores) vs a half/halved default vs a flat `PRECIS_CPU_SLOTS`-only
  opt-IN (rejected in "In scope" — silent un-gating until every CPU
  host is configured). Recommend opt-out `os.cpu_count()`-family.
- **Q3 — Does `autocatpath_revalidate` get `requires={'cpu'}` too?**
  Only if it is truly CPU-only end to end (its spec text mentions a
  GPU leg for min-energy-path). Verify what the job actually re-runs
  before adding the second row.
