---
status: idea
title: autocatpath_aggregate job 449981 held castor for 11h doing work that takes 33s — cause not yet demonstrated, and run_kinetics has no ceiling
---

# A 33-second job ran for eleven hours

## What happened
`autocatpath_aggregate` job 449981 (`no_to_nh3_pd`) was claimed four times
between 2026-09-25 18:00Z and 2026-09-26 07:25Z — once per deploy bounce, via
the epoch reclaim arm — and never finished. Its worker grew to ~117 GB RSS
(~290 MB/min sustained). It was cancelled 2026-09-26; castor has been flat
since (4.0 GB used of 124 GB, worker RSS ~117 MB, no growth over a 120s
double-sample).

Its four event chunks are identical and are the ONLY chunks it ever wrote:

```
ord 0  combining 2 seed partial(s) for no_to_nh3_pd   2026-09-25 18:00:30Z
ord 1  same                                            2026-09-26 05:14:25Z
ord 2  same                                            2026-09-26 06:18:50Z
ord 3  same                                            2026-09-26 07:25:10Z
```

That chunk is written after `_collect_seed_results` and before
`aggregate_seed_partials`, so every attempt got past collection and died
inside one of two calls in `precis_pathway/aggregate_job.py::_dispatch`:
`runner.aggregate_seed_partials` or `runner.run_kinetics`.

## What the reproduction shows
Run locally against **449981's real prod inputs** (both seed partials, 50
structures each) with the **same engine version prod reports** (autocatpath
0.22.0, from `../catpath`'s `dist/`; note PyPI tops out at 0.13.0, which
predates the kinetics module entirely — the engine is unpublished pending the
paper):

```
aggregate_seed_partials: ok in  3.8s   rss 180 -> 183 MB  (+3)
run_kinetics:            ok in 29.0s   rss 183 -> 185 MB  (+2)
kinetics_error: None     TOF 4.64e-10
```

**The whole job is 33 seconds and +5 MB.** `aggregate_seed_partials` is
ruled out as a hog. Nothing here reproduces an 11-hour hold or a leak.

## What is suspicious, and what is NOT yet shown
Instrumenting the same run:

- `autocatpath.kinetics` runs a Monte-Carlo uncertainty loop — **31,795**
  `np.linalg.eig` calls in one aggregate.
- The rate matrix spans **~23 decades** of eigenvalue magnitude (Re from
  -1.8e15 to ~1e-4) with eigenvector conditioning **1e13–1e14**, i.e. a
  near-defective eigenbasis.
- `kinetics._evolve` picks between an exact eigendecomposition and a **BDF
  `solve_ivp` fallback with no `max_step`, no step cap and no time limit**.
  Its own docstring names the hazard: a stiff integrator "can grind for hours
  on a rate matrix spanning ten decades". This matrix spans 23.
- The choice between the two is decided by `np.all(np.isfinite(y)) and
  y.max() > 0` on a result whose conditioning is 1e13 — i.e. **by round-off**.
  Here 2 of ~31,795 draws fell through to BDF, and both were cheap (0.04s,
  ~1,600 nfev).

A different LAPACK/BLAS (Accelerate on arm64 vs whatever castor links) gives
different round-off, so the fall-through rate is plausibly platform-dependent,
and each fallback is unbounded. That is a **coherent mechanism, not a
demonstrated one** — the local run never got slow. Do not write it up as the
cause.

Also note the attribution inherited from 2026-09-26 ("the consumer is
449981") rests on a temporal correlation: 11.2h of growth in a window with
zero MLIP dispatches. That excludes the MLIP calculator; it does not prove
449981 was the consumer. Given the job's measured cost is 33s, the
alternative — that something else in that worker process was growing and
cancelling 449981 merely coincided — is live and untested.

## The decisive experiment
Run the same reproduction **on castor**, in `/opt/precis/venv`, against the
same two seed partials. Same CPU, same LAPACK, same engine build.

- completes in ~33s → 449981 was never the consumer; the leak hunt reopens
- hangs or grows → the platform-dependent numerics story is confirmed, and
  the fix is upstream in `catpath`

This needs a cluster execution, so it needs Reto.

## What to fix regardless of the outcome
`run_kinetics` has **no ceiling of any kind**, and its own docstring calls it
"a diagnostic bonus riding on a successful aggregate, never load-bearing".
Its `try/except` cannot interrupt a hang. A call measured at 29s must not be
able to hold a worker for 11 hours whatever the reason.

Fix shape: run the solve in a killable child process with a wall-clock cap
(`PRECIS_AUTOCATPATH_KINETICS_SECONDS`, default generous — 900s is 30x the
measured cost), timing out into
`results_json["kinetics_error"] = "timed out after Ns"`. That is already the
contract for every other kinetics failure, so the aggregate still succeeds and
persists. `precis_pathway/runner.py` already has the seam to copy —
`run_seed_partial_subprocess` / `_child_cmd` / `_subprocess_main`, built for
exactly this reason (gr191351: "running the compute out-of-process also makes
a genuine hang killable").

This doubles as the instrument that settles the question in production: next
occurrence yields `kinetics_error: timed out` instead of silence.

## The structural gap behind it
`PRECIS_AUTOCATPATH_WALL_SECONDS` (`quest/compute.py::_autocatpath_wall_seconds`)
is only a **lease-length hint** — it bounds nothing. Nothing kills a running
`ssh_node` blocking dispatch, and `precis jobs kill` only stamps
`meta.kill_requested`, which a blocking dispatch never observes. That is why
restart → re-claim → grow → dark was unbreakable by anything except a manual
cancel. A real execution ceiling on the blocking-dispatch path would have
capped this at one lease instead of four claims across two days. Filed
separately as the broader fix; the kinetics bound above is the narrow one.

## Related
Kept standalone rather than folded into the 2026-09-26 backlog regroup's
`autocatpath-seed-health.md`: that umbrella is the **seed** job family, and
this is the aggregate. Same engine, different job type and different failure.

## Loose end
449981 was cancelled, and it has no `parent_ref_id` and no `idem_key`, so the
`no_to_nh3_pd` aggregate is **stranded** and needs a re-dispatch. Do that only
after the bound above is deployed, or it reproduces.
