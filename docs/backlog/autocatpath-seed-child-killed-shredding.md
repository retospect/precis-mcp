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

## Verify (batch 3)

Re-dispatch the 50 tick-zero replicates (same idem keys `tick0-<st>-r<n>`,
`requires={'gpu':1}`, `JobHandler.put`) after the fixes deploy. Expect
`STATUS:succeeded` with `meta.partial` populated; any `min_dist_A` for
single-atom states should read `null`. Then run the per-structure spread
analysis (host as batch variable) that both dead batches were for.
