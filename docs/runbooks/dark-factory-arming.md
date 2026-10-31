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

## Release pass (cut, verdict, approval, deploy)

Mint one plan; the armed `plan_dispatch` tick drives it:

    put(kind='todo', mode='plan', args={'template': 'release-pass'})

The nodes run in order: `cut` (`release_cut`: `scripts/release --cut-only`
in the slice host's checkout, then `meta.release = {rN, sha, range, repo}`
on the plan root) → `poll` (`ci_verdict_poll`: records check.yml's verdict
in `release.ci_verdicts`, parked between polls while CI runs) → `gate`
(`tests-green` on the cut's range) and `loop` (a red verdict mints the
`release-fix` body: triage → fix → `recut` (`release_cut`, records the new
cut and range on the root, the gate, the loop and the approval options) →
`poll` (`ci_verdict_poll` on the new sha); red again mints the next
iteration, up to 3) → `approval` → `deploy` (`release_deploy`:
`scripts/release --deploy-only r<N>`, pinned).

The `approval` node is an `ask-user` todo (`meta.ask.kind = approval`,
options `deploy r<N>` | `hold`); it shows on `/asks` once the plan exists
and opens once the gate is green and it is answered. To deploy:

    put(kind='todo', id=<approval id>, text='answer: deploy r<N>')
    tag(kind='todo', id=<approval id>, add=['STATUS:done'], remove=['ask-user'])

Any other answer (`hold`) closes the ask but deploys nothing: `release_deploy`
refuses unless the newest `answer:` names `deploy r<N>` for the cut this plan
recorded, and the tick notes "not dispatched" each pass. To deploy later,
add a new `answer: deploy r<N>` comment. The deploy exits at "rollout done,
runtime pending" like `/go`: confirming runtime
(`scripts/round deploy --confirm-runtime`) stays a session's step.

Host needs: the slice host's checkout (`PRECIS_SLICE_ROOT`) with push
rights to origin, `gh` (the script's gate step), and whatever
`scripts/deploy` needs for the deploy; cut and deploy run in the same
checkout (its `.release-owner` marker makes the deploy the release's
owner). `ci_verdict_poll` needs only the DB and api.github.com; set
`release.repo` (`owner/repo`) on a host with no checkout.

## Kill switch

- `PRECIS_FACTORY=off` in the worker's environment: `run_slice`,
  `release_cut` and `release_deploy` refuse every submit, and `plan_dispatch` runs read-only (counts what it would
  have done, writes nothing). Unset it and restart the worker to re-arm.
- A `service_config` row for `plan_dispatch` with `prio = 0` (any host)
  also makes the pass read-only, as does `params.dry_run`.

## Slice hosts

- `PRECIS_SLICE_ROOT`: the repo checkout `run_slice` cuts `slice-td<id>`
  worktrees (branch `slice/<id>`) under. Default: the main checkout of
  the repo the worker runs from. Set it when the worker has no git
  checkout around its code. A leftover `slice-td<id>` branch from an
  earlier attempt is renamed to `slice/<id>`.
- `PRECIS_CLAUDE_BIN`: the `claude` binary the slice session execs
  (default `claude` on PATH). Point it at a stub in tests.
- The session runs `--permission-mode bypassPermissions` with the lane's
  `deny_write_paths` (`FACTORY_DENY_WRITE_PATHS` in
  `src/precis/plans/lanes.py`: the ship harness, the factory's own
  modules, alert rules and detectors) passed as `--settings` deny rules.
  The poll re-checks the branch diff and working tree; a hit fails the
  attempt and reopens the todo. The session never lands: a clean exit
  mints a `land_slice` job on the same host that runs `scripts/ship
  --quick` in the worktree (`PRECIS_SHIP_BIN` overrides it), so the check
  is pre-land; that host needs the ship's git push and land_notify DSN.
- The session's env is an allowlist (`ENV_ALLOWLIST` in `run_slice.py`):
  no DB DSN or vault variable reaches it. It needs `PRECIS_MCP_TOKEN`
  (env or the mounted secret) to reach the precis MCP, and the claude
  login under `HOME` (or `CLAUDE_CONFIG_DIR`); `ANTHROPIC_API_KEY` is
  not passed.
