# autocatpath seeds: `infra:child-killed` was a persistence bug, not killed children — residuals

> ROOT-CAUSED 2026-08-28 (sonnet root-cause pass, 47/48 jobs positively
> matched to the identical traceback). Core fixes shipped the same day:
> non-finite-float sanitizer in `executors/_common.py::set_meta`
> (`_finite_json`), cleanup-after-persist in
> `precis_pathway/runner.py::poll_seed_partial_detached` (done branch keeps
> the envelope; `finalize_seed_partial_detached` reclaims it after
> `seed_job._poll` persists). This file tracks what REMAINS.

## What actually happened (the record)

Batch 2 (jb265164–265213, 50/50 "failed"): every traceable child **ran to
completion** (10–45 min GPU each) and wrote a valid `result.json`. The
chain: single-atom N*/O* adsorbates have no pair distance, so catpath's
`validate.py` `min_dist = np.inf` survives to `trust.grade_clash` →
`evidence.min_dist_A: Infinity` — valid Python json, **rejected by Postgres
jsonb** (strict RFC 8259). `seed_job._poll`'s `ctx.set_meta` raised
`InvalidTextRepresentation`; `ssh_node.run_ssh_node_pass`'s poll loop
logged-and-continued; the scratch dir had already been deleted *before*
persist; the next poll found a dead pid with no envelope → false
`infra:child-killed`. Deterministic; uncorrelated with the day's two deploy
windows (the deploy-bounce hypothesis is dead). Batch 1's "missing GPU slot
token" attribution is likely the same bug misread — do not cite it as a
confirmed cause.

## Remaining

1. **jb265186 (spark) unexplained** — the 1/48 without the Infinity
   traceback in its window. Possibly a genuine one-off infra event (spark
   daemon restarts 08:26/09:07 that day) or a log gap. Don't fold it into
   "same cause" silently; if batch 3 shows a spark-only residual, start here.
2. **The generic swallow in `ssh_node.py::run_ssh_node_pass`** (~line 205):
   any exception from a job_type's `poll()` is still log-and-continue. With
   cleanup-after-persist the seed path now retries safely, but other
   detached job_types with their own cleanup ordering could reproduce the
   discard-then-misclassify shape. Audit other `poll()` implementations for
   cleanup-before-persist; consider a `record_failure` after N consecutive
   poll exceptions on the same ref so a permanently-failing persist
   surfaces instead of silently spinning to the wall deadline.
3. **Catpath upstream nicety**: `grade_clash` could emit `null` (or omit)
   instead of `inf` for the no-pair case — semantic, not load-bearing now
   that the precis boundary sanitizes. Fold into the next catpath handoff.
4. **Other `Jsonb(...)` writes**: `_common.py`'s lease/claim write (~line
   620) carries internally-generated finite values only — left unsanitized
   deliberately. Anything new that persists compute-derived numbers must go
   through `set_meta` or `_finite_json`.

## Verify (batch 3) — completed 2026-08-29, fix confirmed (items above remain open)

Batch 3 (jb266899–266948) + stragglers (jb269500–269508): 44/50 idem keys
succeeded, every success persisted `meta.partial` with `min_dist_A: null`
on single-atom states — the sanitizer + cleanup-after-persist behave
exactly as predicted. Spread analysis (host as batch variable) can run on
the 44.

5. **st210770 is a pathological structure, not infra** — its six
   `tick0-210770-r*` replicates have failed EVERY attempt across every
   batch (22+ failures, 0 successes; wall-timeout + `infra:child-killed`
   mix on spark/pollux/castor — hosts that completed st261823 seeds in the
   same window). The seed job on this structure likely blows up the relax
   (OOM on some hosts, runs-past-wall on others). Do NOT re-dispatch;
   exclude from the spread analysis (its failure IS the data point);
   inspect the structure (`st210770`) and reproduce one seed by hand on a
   node to classify. (Checked: jb265186 was `tick0-210211-r3`, NOT a
   210770 replicate — and that idem key SUCCEEDED in batch 3 (jb266921),
   so item 1 stays classified as a one-off spark infra event, now
   moot.)
