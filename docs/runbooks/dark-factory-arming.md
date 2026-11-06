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

## Release pass (cut, verdict, approval, deploy, confirm)

Mint one plan; the armed `plan_dispatch` tick drives it:

    put(kind='todo', mode='plan', args={'template': 'release-pass'})

The nodes run in order: `cut` (`release_cut`: `scripts/release --cut-only`
in the slice host's checkout, then `meta.release = {rN, sha, range, repo}`
on the plan root) → `poll` (`ci_verdict_poll`: records check.yml's verdict
in `release.ci_verdicts`, parked between polls while CI runs) → `gate`
(`tests-green` on the cut's range) and `loop` (a red verdict mints the
`release-fix` body: `digest` (`ci_red_digest`: `scripts/ci-failed` on the
red run, the digest posted as a `brief:` on the fix) → fix → `recut`
(`release_cut`, records the new
cut and range on the root, the gate, the loop and the approval options) →
`poll` (`ci_verdict_poll` on the new sha); red again mints the next
iteration — two automatic iterations, then the loop files a ruling ask
and a third never starts by itself) → `approval` → `deploy` (`release_deploy`:
`scripts/release --deploy-only r<N>`, pinned) → `confirm`
(`release_confirm`: the runtime attestation below).

The `approval` node is an `ask-user` todo (`meta.ask.kind = approval`,
options `deploy r<N>` | `hold`); it shows on `/asks` once the plan exists
and opens once the gate is green and it is answered. To deploy:

    put(kind='todo', id=<approval id>, text='answer: deploy r<N>')
    tag(kind='todo', id=<approval id>, add=['STATUS:done'], remove=['ask-user'])

Any other answer (`hold`) closes the ask but deploys nothing: `release_deploy`
refuses unless the newest `answer:` names `deploy r<N>` for the cut this plan
recorded, and the tick notes "not dispatched" each pass. To deploy later,
add a new `answer: deploy r<N>` comment.

The `confirm` node (`release_confirm`, `retries: 0`, pinned to the cut's
host like the deploy) then attests the runtime: every live worker's
heartbeat `code_sha`, read from the store with no network, must equal the
release sha, and when `release.health_url` is set the live web's
`X-Precis-Boot-Sha` header must too. On pass it runs `scripts/round deploy
--confirm-runtime <sha> --runtime-evidence "<text>"` then `scripts/round
close`, stamps the evidence on the plan root and `release.round`, and the
release is tagged `deployed/r<N>`; a failed close still succeeds the job —
`scripts/release` retries the close on its next run. A mismatch or gap
fails the job before any script runs and the tick's retry-cap ask goes to
a human; there is no rollback and no `good/r<N>` tag — the factory
attests, a failed probe asks, never rolls back.

`release.health_url` is an optional operator setting
(`scripts/prod-precis settings set release.health_url <url>`) naming the
live web's `/healthz`. Unset means the web is not attested — the workers
still decide; a set URL must be a public address `safe_get` may reach (the
SSRF guard refuses private ones, which fails the confirm), and a failing
or different-sha URL fails the confirm too. No URL or host is printed in a
job result or failure reason.

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

### Standing approval (release.auto_deploy)

`release.auto_deploy` (bool, default off, DB-only — no env fallback, so
a drifted host cannot arm it) is the owner's standing approval for the
deploy ask. While it is on, the plan tick answers a release-pass
`approval` node itself once the newest recorded CI verdict on the cut's
exact sha is green *and* `release.round` names an open round on that
head sha:

    scripts/prod-precis settings set release.auto_deploy true
    scripts/prod-precis settings set release.auto_deploy false

The answer is an audit comment on the approval node — the node stays,
it is not removed:

    answer: deploy r<N> (auto-answered under release.auto_deploy; sha
    <sha12>; range <prev>..<sha12>; CI green run <id> at <ts>; round
    record r<N> <state>; answered <ts>Z)

It covers every range — migrations, `safe_fetch.py` and harness diffs
included (me573231: no range inspection). It fails closed and leaves the
ask for a human when anything is off: the setting off or unreadable, no
green verdict recorded on the cut's exact sha (a sha prod already covers
or another sha's green does not count), `release.round` absent,
unreadable, or naming another release, state or head, and any existing
`answer:` — a human `hold` is never overwritten. These always still ask
a human: rollback, superseding a partial rollout, and the exhausted
release-fix loop — two red fix iterations run unattended, the second red
files the ruling ask and no third starts by itself.

### Stuck watchdog

The same `release_poll` pass also watches the live `release.round`
record for a release that stopped moving (td574370). It runs first in
every pass — at any hour, inside the no-release window and while a pass
is open — and its only write is one `ask-user` ruling todo, once per
`(round, stage)` (the dedupe tag is `release-stuck:r<N>:<stage>`).

The record's `state_at` is the clock. Stages: `deploying` (the rollout
journal is live), `confirm` (deployed, `deploy.confirmed_at` absent),
`fixing` (the newest `release.ci_verdicts` row for the head is red —
counting a red on the round's older head until the current head has a
verdict), `gating` (no verdict, but a check.yml run is in flight on the
head — the one GitHub read the watchdog pays, made only past the `cut`
mark), and `cut` (no verdict and no run). A green verdict is the
approval/deploy wait and never asks. Records without `state_at` (every
round cut before this) and terminal states are never judged.

Minutes per stage come from `release.stage_deadlines` (DB-only, JSON;
default `{"cut":30,"gating":180,"fixing":240,"deploying":60,"confirm":45}`).
A stage absent, non-integer or `<= 0` is not watched; an unparsable or
non-object value watches nothing — fail closed, no ask.

The ask is a `ruling` (`acknowledged` | `false alarm`, blocks
`nothing`); it names the round, stage, minutes over, head sha and the
hand verbs that apply (`scripts/round deploy`, `scripts/round
supersede --partial --reason "<why>"`, `scripts/round cut --abandon`).
Answering executes nothing: deploys, supersedes, cuts and abandons stay
a human's call.

## Release loop (release_poll)

The loop starts itself: one recurring todo; every 10 minutes
`release_poll` mints one `release-pass` plan only when its gates pass,
and the armed `plan_dispatch` tick then drives it:

    put(kind='todo', text='release_poll tick',
        meta={'schedule': {'every': '10m'}, 'executor': 'job_inproc',
              'job_type': 'release_poll'})

Before the first arm, check for an open release-pass root minted before
the `release-pass` tag existed: the open-pass gate would miss it — close
it or tag it.

Disarm: close or pause that todo. Hand releases are that todo paused —
never both armed (the in-flight check also catches a hand release
already on origin). Dry run by hand: a `release_poll` job with
`params={'dry_run': true}` reads every gate and reports `would mint`,
minting nothing.

The gates, checked in order — the pass stops at the first blocker and
writes one `release_poll <repo>: blocked — <why>` line, so an idle
main-equals-prod steady state costs a single api.github.com request:

1. Clock: the `release.no_release_windows` setting — comma-separated UTC
   `HH:MM-HH:MM` pairs, each half-open `[start, end)`; a start later
   than the end crosses midnight (`23:00-01:00` covers 23:00-24:00 and
   00:00-01:00). Default `03:00-04:20`, the nightly backup window
   (`deploy/roles/backups/defaults/main.yml`); the 21:30-23:30Z
   directory-backup window is deliberately not in it — add it with
   `scripts/prod-precis settings set release.no_release_windows
   03:00-04:20,21:30-23:30`. An unparsable value blocks the pass.
2. No live `release-pass`-tagged todo still open.
3. The shared `release.round` record (`scripts/round` publishes it)
   names no open round — a non-terminal state, any owner, is in flight;
   an absent record is "no round", an unreadable one blocks.
4. `compare/prod...main` on GitHub reports `ahead_by > 0` with
   `behind_by == 0`: `diverged` or `behind` (prod holds commits main
   lacks, e.g. a hand hotfix) never mints.
5. No `release/r<N>` head in flight on origin: a head is in flight
   unless its `deployed/r<N>` tag exists AND prod contains it. The
   `matching-refs` reads are explicitly bounded — over-cap heads read
   as unreadable, over-cap tags as in flight.
6. One pass per main sha: no `release-main:<sha12>`-tagged root exists
   for main's head, in any state — a new pass needs main to move. The
   claim check and the mint are one transaction under an advisory
   lock, so two concurrent runners cannot both mint.

Host needs: only the DB and api.github.com (`GH_TOKEN` lifts the 60/h
unauthenticated limit, shared with `pr_poll` and `ci_verdict_poll`; set
`release.repo` on a host with no checkout). Not covered by the kill
switch — the job only mints; the plan runs when `plan_dispatch` is
armed.

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
  `release_cut`, `release_deploy`, `release_confirm`, `dep_check` and
  `pr_action` refuse
  every submit, and `plan_dispatch` runs read-only (counts what it would
  have done, writes nothing). Unset it and restart the worker to re-arm.
- A `service_config` row for `plan_dispatch` with `prio = 0` (any host)
  also makes the pass read-only, as does `params.dry_run`.

## Lane ticker (r184+)

The slices lane (`com.precis.worker-slices`, playbook 20g) also runs
`--only plan_tick`: the same `plan_dispatch` pass as a worker pass, at most
once per `PRECIS_PLAN_TICK_INTERVAL_S` (default 30 s), with no scheduler,
todo or job in between. Once the lane is live, pause the recurring tick todo
so there is one ticker:

    tag(kind='todo', id=543894, add=['STATUS:paused'])

The kill switch is unchanged: `PRECIS_FACTORY=off` on the lane's
environment, or a `service_config` row for `plan_dispatch` with `prio = 0`;
either makes each tick read-only (it logs what it would have done).

## Slice hosts

A slice host is any worker whose heartbeat advertises `repo_checkout`,
`claude_bin` and `git`. No host is named anywhere: `run_slice` is claimed
only by a worker advertising all three; `land_slice`, `release_cut`,
`release_deploy` and `release_confirm` need `repo_checkout` + `git` (the
lander and a re-cut, deploy or confirm stay pinned to the host that ran
the slice or the cut). Until a host
qualifies, the job waits `STATUS:queued` — it is not failed or counted as
an attempt — the sweeper raises `scheduler/unschedulable:<job id>`, and
after 10 min the tick notes `td<id>: job jo<N> queued <m> min — no host
advertises {tokens}`.

The probes, re-run every heartbeat:

- `repo_checkout`: `PRECIS_SLICE_ROOT` names a directory holding `.git`, or,
  unset, the worker's code sits inside a git checkout. A venv install
  (`site-packages`) has none: set `PRECIS_SLICE_ROOT`. The deploy renders it
  on the gateway from `precis_slice_root` (default `precis_fix_repo_dir`,
  the deploy user's `precis-fix-repo` checkout) in playbook 20b — the heartbeating unit
  whose probe writes the row — and in 20g, the lane that runs the jobs. Set
  the variable, not the plist: a redeploy re-renders every plist from the
  templates, and a hand-added value vanishes with it (2026-11-05, r222:
  every factory land queued "no host advertises repo_checkout", gr570787).
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

  Finished stacks: a landed slice's `precis-test-slice-td<N>` compose
  project is brought down (`-v`) before its worktree is removed, and
  every land launch first sweeps the host's other slice stacks
  (`precis.workers.slice_stacks`): gone trees (`-v`), quiet stacks idle
  past `PRECIS_SLICE_STACK_IDLE_S` (default 3600 s — the gate-cache
  volume is kept) and the oldest quiet stacks over
  `PRECIS_SLICE_STACK_CAP` (default 8). A stack with a running non-gate
  container, an attached exec or a live `precis-test-runs/<pid>` marker
  is never touched, and `PRECIS_SLICE_STACK_SWEEP=off` disables the
  sweep. The SessionStart reapers never run on this headless host, so
  without the sweep finished stacks pile into the 8 GiB docker VM until
  gates die on OOM-137 and address-pool exhaustion (gr570133).
- `PRECIS_CLAUDE_BIN`: the `claude` binary the slice session execs
  (default `claude` on PATH). Point it at a stub in tests.
- `PRECIS_MCP_SLICE_TOKEN_DIR`: the shared session MCP's per-slice grant
  registry (`<mcp-http state dir>/slice-tokens`, created by the ensure
  script). Needed when the worker runs as a different user from the one
  running the server (melchior: launchd `deploy` vs the operator): the
  default is under the *worker's* home, where no server reads, so the
  session would start on the worker's `PRECIS_MCP_TOKEN` — and a service
  account has none, which the session sees as `401 AUTH_HEADER_REJECTED`
  / "Not logged in" (jo546987). The deploy renders it on the gateway from
  the host var `precis_mcp_slice_token_dir` (no default: the dir is the
  operator's) in playbooks 20b and 20g; set the variable in the operator
  overlay, never the plist — the r224 redeploy (2026-11-06) re-rendered
  the plists with the hand-set value gone, and every slice after it
  launched "mcp token: shared" and built blind to the MCP (gr571617). The ensure script makes the dir setgid
  group-writable; the worker user must share that group. The grant file
  holds the bearer's SHA-256 digest at mode 644, not the token: on a Mac
  host colima's virtiofs reads every mounted file as the colima user, so
  a 0600 grant owned by the worker account was unreadable to the server
  and every slice call answered 401 (jo547598). Before launching, the
  worker sends one `initialize` with the session's bearer to the
  `.mcp.json` URL and fails the attempt in seconds on anything but a
  2xx, so a broken grant never costs a session's wall clock.
- The session's `UV_CACHE_DIR` is `<tempdir>/precis-slice-ops/uv-cache`
  (`PRECIS_SLICE_OPS_DIR` overrides the parent), not the service account's
  `~/.cache/uv` — the checkout's hooks run through `uv`, and a root-owned
  home cache (a `sudo` bootstrap) killed every Bash hook in jo546987.
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
