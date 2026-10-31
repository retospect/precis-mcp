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

Host needs: the slice host's checkout (`PRECIS_SLICE_ROOT`), `gh` (the
script's gate step), and whatever `scripts/deploy` needs for the deploy.
Push rights come from the vault, not the host: the fine-grained GitHub
token stored as `PRECIS_FACTORY_GIT_TOKEN` (`scripts/prod-precis secret set
PRECIS_FACTORY_GIT_TOKEN --prompt`; repository `precis-mcp` only, Contents
and Pull requests read/write) is installed as git's credential helper and
`GH_TOKEN` in the env of `land_slice`, `release_cut`/`release_deploy` and
every `gh` call (`workers/factory_git.py`). The slice session never sees
it. Without it those jobs run on the host's own credentials, and a service
account has none. Cut and deploy run in the same
checkout (its `.release-owner` marker makes the deploy the release's
owner). `ci_verdict_poll` needs only the DB and api.github.com; set
`release.repo` (`owner/repo`) on a host with no checkout.

## Inbound pull requests (pr_poll)

One recurring todo; every 15 minutes `pr_poll` lists the repo's open PRs
and mints one `pr-intake` plan (tagged `pr:<n>`) per non-draft PR that has
none. The armed `plan_dispatch` tick drives each plan:

    put(kind='todo', text='pr_poll tick',
        meta={'schedule': {'every': '15m'}, 'executor': 'job_inproc',
              'job_type': 'pr_poll'})

Disarm: close or pause that todo. Dry run by hand: a `pr_poll` job with
`params={'dry_run': true}` lists what it would mint, with each PR's class.
One PR by hand: `put(kind='todo', mode='plan', args={'template':
'pr-intake', 'pr': <n>})`.

The class decides the nodes (`precis.plans.pr_class`): `dependabot` runs
`dep_check` → (major bump: an `approval` ask, `land` / `ignore this major`
/ `hold`) → `land_slice --checked` → reviewer; `factory` lands and
reviews; `harness` is only an ask (land it yourself with `/go`);
`external` gets a read-only analysis, then an ask (`approval` for a fix,
`design-verdict` for a feature: `land as is` / `land after fixes: <list>`
/ `request changes on the PR` / `close`). Answer like the release ask:

    put(kind='todo', id=<decision id>, text='answer: land')
    tag(kind='todo', id=<decision id>, add=['STATUS:done'], remove=['ask-user'])

`pr_action` acts on the answer; nothing is posted to GitHub before it.

Host needs: `pr_poll` only the DB and api.github.com (set `release.repo`
when the worker has no checkout; `GH_TOKEN` lifts the 60/h limit).
`dep_check`, `land_slice` and `pr_action` run on a slice host: the
checkout, `uv`, push rights and `gh auth` (the PR close and comments are
posted as that login — gr543694). `pr.factory_builders` lists the logins
whose `(td<id>)` PRs count as factory slices (default Devin's bot).

## Kill switch

- `PRECIS_FACTORY=off` in the worker's environment: `run_slice`,
  `release_cut`, `release_deploy`, `dep_check` and `pr_action` refuse
  every submit, and `plan_dispatch` runs read-only (counts what it would
  have done, writes nothing). Unset it and restart the worker to re-arm.
- A `service_config` row for `plan_dispatch` with `prio = 0` (any host)
  also makes the pass read-only, as does `params.dry_run`.

## Slice hosts

A slice host is any worker whose heartbeat advertises `repo_checkout`,
`claude_bin` and `git`. No host is named anywhere: `run_slice` is claimed
only by a worker advertising all three; `land_slice`, `release_cut` and
`release_deploy` need `repo_checkout` + `git` (the lander and a re-cut or
deploy stay pinned to the host that ran the slice or the cut). Until a host
qualifies, the job waits `STATUS:queued` — it is not failed or counted as
an attempt — the sweeper raises `scheduler/unschedulable:<job id>`, and
after 10 min the tick notes `td<id>: job jo<N> queued <m> min — no host
advertises {tokens}`.

The probes, re-run every heartbeat:

- `repo_checkout`: `PRECIS_SLICE_ROOT` names a directory holding `.git`, or,
  unset, the worker's code sits inside a git checkout. A venv install
  (`site-packages`) has none: set `PRECIS_SLICE_ROOT`.
- `claude_bin`: `claude` on the worker's `PATH`, or `PRECIS_CLAUDE_BIN`
  set to a name on `PATH` or an absolute path (a launchd `PATH` often
  misses it).
- `git`: `git` on the worker's `PATH`.

Set the env vars in the worker's service environment and restart it; the
next heartbeat advertises the tokens. To check what a host advertises:

    scripts/prod-psql --ro "SELECT host, resource, capacity FROM resource_slots
      WHERE resource IN ('repo_checkout', 'claude_bin', 'git') ORDER BY host"

The other settings a slice host reads:

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
