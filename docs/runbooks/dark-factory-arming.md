# Dark factory — arming, kill switch, host settings

When you need to arm or disarm the dark factory (`plan_dispatch` ticking
plan subtrees and minting `run_slice` jobs), or set up a host that runs
slices. The job types are described in `precis-job-help`; this page holds
the operator recipes that skill deliberately leaves out.

## Arm

An operator write, not done by any deploy. One recurring todo; the minter
turns it into a `plan_dispatch` job every 5 minutes:

    put(kind='todo', text='plan_dispatch tick (dark factory)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'plan_dispatch'})

Disarm: close or pause that todo.

The same tick also returns a `STATUS:fixing` gripe to `triaged` once its
`claimed-by:` lease has expired (4 h) with no live job under it (gr542086);
`released=` counts those with the todo claims. Gated by the same kill switch.

## Gripe validation (verify → done)

`scripts/release` runs `scripts/gripe-validate` after a rollout. For a pass
that needs no release and no git checkout, arm the `gripe_validate` job type:
it closes every `STATUS:verify` gripe whose `resolved: <sha>` comment names a
sha in the `release.prod_head` record (the head or its `landed` list). Same
shape as the tick, every 15 minutes:

    put(kind='todo', text='gripe_validate tick (verify gripes to done)',
        meta={'schedule': {'every': '15m'}, 'executor': 'job_inproc',
              'job_type': 'gripe_validate'})

Disarm: close or pause that todo. Dry run by hand: a `gripe_validate` job
with `params={'dry_run': true}` lists what would flip. Not covered by the
factory kill switch (it only closes gripes whose fix is already deployed).

## Kill switch

- `PRECIS_FACTORY=off` in the worker's environment: `run_slice` refuses
  every submit, and `plan_dispatch` runs read-only (counts what it would
  have done, writes nothing). Unset it and restart the worker to re-arm.
- A `service_config` row for `plan_dispatch` with `prio = 0` (any host)
  also makes the pass read-only, as does `params.dry_run`.

## Slice hosts

- `PRECIS_SLICE_ROOT`: the repo checkout `run_slice` cuts `slice-td<id>`
  worktrees under. Default: the main checkout of the repo the worker runs
  from. Set it when the worker has no git checkout around its code.
- `PRECIS_CLAUDE_BIN`: the `claude` binary the slice session execs
  (default `claude` on PATH). Point it at a stub in tests.
