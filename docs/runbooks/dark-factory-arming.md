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

## Halted cadences (halt_heal)

A cadence that halts itself gets one frontier repair attempt before the ops
channel is told. Today the one registered halt is the gripe triage tick
(`app_state` key `gripe_triage:blocked`, set after three whole-batch
failures in a row). Each time the halt is armed or re-armed:

- First halt in a 24 h window: one `halt_heal` job (`claude_inproc`, read
  verbs only, no `put`) investigates what stopped the cadence and whether the
  cause is gone. A `CLEAR` verdict empties that one key, so the tick runs on
  its next pass; the job result and `app_state` `halt_heal:last:<key>` record
  it. No alert.
- `ESCALATE`, a reply without the verdict block, a session error, a quota
  pause past the park cap, or any exception raises one critical `halt_heal`
  alert and one push to the ops channel. The detail holds the halt note, the
  model's diagnosis (marked as untrusted), what was tried, the human action
  and the job handle.
- The halt coming back inside the same 24 h window escalates at once with the
  earlier diagnosis; a repair that recurs is a human's problem. That capped
  call also files one `[bounded-heal]` gripe (the shared heal primitive
  always does; it cannot be switched off per heal).
- A tick that leaves the halt in place, including an idle probe, requests
  a supervisor for a halt with no record or checks the existing job. A job
  with no report after 2 h
  escalates once (`halt_heal:last:<key>` goes to `outcome: overdue`).
- One page per episode: once a human was alerted (marker
  `halt_heal:alerted:<key>`), further re-arms start no session and write no
  second alert, in every mode, and a session that still answers `CLEAR`
  clears nothing; it escalates with its diagnosis on the open alert.
- The alert resolves when the cadence runs a good batch again (a probe
  succeeded, or you reset the key), never on age.

Operator switch, `app_state` key `halt_heal:mode` (set it with
`scripts/prod-psql`; an operator setting, not an agent write):

| value | effect |
|---|---|
| absent, empty or `heal` | session runs, a `CLEAR` clears the halt |
| `report` | session runs and its diagnosis rides the alert, but a `CLEAR` never clears: it escalates |
| `off` | no session, escalate at once (a job queued earlier also escalates without a session) |

An unknown value reads as `report`. Manual reset when the cause is fixed:
`scripts/prod-psql "DELETE FROM app_state WHERE key = 'gripe_triage:blocked'"`.

## Release pass (cut, verdict, approval, deploy, confirm)

Mint one plan; the armed `plan_dispatch` tick drives it:

    put(kind='todo', mode='plan', args={'template': 'release-pass'})

The nodes run in order: `cut` (`release_cut`: `scripts/release --cut-only`
in the slice host's checkout, then `meta.release = {rN, sha, range, repo}`
on the plan root) → `poll` (`ci_verdict_poll`: records check.yml's verdict
in `release.ci_verdicts`, parked between polls while CI runs; the verdict
is `lint` plus the `gate-fast` legs, so the deploy does not wait for the
run's soak legs and a red soak is not a red release) → `gate`
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

A fix session ending `validated:` (the red legs do not reproduce, no
commit) instead parks the fix node on `waiting-for:ci-rerun` and the
tick mints a `ci_rerun` job re-firing that run's failed legs — once per
run id; a green rerun closes the fix node and the loop's recut proceeds,
a still-red rerun re-arms the digest.

The deploy leg needs the deploy user to hold the operator's deploy files:
set `precis_fix_lane_deploys: true` in the gateway's host_vars, then run one
operator deploy so the play places the overlay, vault key, prod DSN file and
cluster ssh key in the fix-lane checkout (`deploy/README.md` "Fix-lane
deploys"). Without it `release_deploy` fails with "install-from-tree: no
overlay" and the round record is "not published: no prod DSN on this host".
The deploy account then holds the same deploy authority as the operator.

The first `release_cut` of a pass has no `meta.release.host` to pin to, and
the cut's checkout is the deploy's, so name the host that holds the overlay:
`put(kind='job', mode='setting', args={'key': 'release.cut_host', 'value':
'<node>', 'reason': '...'})` (the node's `PRECIS_NODE`; DB-only). Unset, the
tick holds the cut with a note instead of letting any `ssh_node` host claim
it (gr713351), and `release_cut` itself refuses a checkout with no overlay.

The job types also write the shared `release.round` record through the
store (gr575362): the cut creates the row (`state: cut`, the script's own
per-clone publish may have skipped on the slice host), the deploy merges
it forward to `deployed` with `deploy.rollout_at`/`deploy.sha`, and the
confirm's `record_confirm` moves it to `confirmed` — rank-ordered
forward moves only, best-effort, never a job failure. This row is what
`release.auto_deploy`'s open-round check and the stuck watchdog read.

The cut also clears the clone's own stale record: before `--cut-only`
launches, `release_cut` supersedes the checkout's local `round.json`
record when it is the one `deploying`/`deployed` record, or a `failed`
one whose rollout launched (a pinned sha,
no pending cut, no runtime confirm), the shared row proves prod moved
past it (`confirmed` at the same or a later number), and no
`release_deploy` job is live. It runs `scripts/round supersede r<N>`
detached with `PRECIS_GRIPE_NOTE=0` — record-only, the shared row is
never rewritten and the verb's own ancestor-of-prod gate is the proof —
then launches the cut on exit 0; a refusal fails the job with the log
tail so the retry-cap ask still fires. Any other shape (an open
`cut`/`gating`/`fixing`/`green` local record, a shared row that does not
prove the release) is left alone for the ask.

A `RELEASE_CUT` line is not believed on its word (gr724696):
`scripts/release --cut-only --sha S` prints it only for a release that
ships S — a resumed record that lacks S answers "release in flight", one
prod moved past exits 4 naming `scripts/round supersede r<N>` — and
`release_cut` fails the job (`stale-record`, nothing recorded) when the
reported sha does not contain the asked one or already sits in
origin/prod. The cut launch passes the shared row's number as
`PRECIS_ROUND_FLOOR`, so an abandoned release's number is not minted
again, and a cut that does reuse a dead row's number replaces the row
(gr714067).

The `approval` node is an `ask-user` todo (`meta.ask.kind = approval`,
options `deploy r<N>` | `hold`); it shows on `/asks` once the plan exists
and opens once the gate is green and it is answered. To deploy:

    put(kind='todo', id=<approval id>, text='answer: deploy r<N>')
    tag(kind='todo', id=<approval id>, add=['STATUS:done'], remove=['ask-user'])

Any other answer (`hold`) closes the ask but deploys nothing: `release_deploy`
refuses unless the newest `answer:` names `deploy r<N>` for the cut this plan
recorded, and the tick notes "not dispatched" each pass. To deploy later,
add a new `answer: deploy r<N>` comment.

A deploy that `scripts/deploy` refuses ahead of its reachability ping,
before any rollout step — the backup window, a checkout with no overlay,
the overlay sync, the rollback guard, the deploy lock — exits 75 through
`scripts/round deploy` and `scripts/release` (gr730609). The local record
goes back to what it was before the leg. On a first leg that is open with
no journal: the job fails, the release takes fixes again, a retry deploys
it, and a dropped pass's record is one `scripts/round cut --abandon` (or
the next cut's own abandon phase) can retire. A resumed leg returns to its
prior state and journal, so an earlier launched rollout stays a partial
rollout. A leg adds the launch marker only when it reaches the ping.
One thing does survive the refused leg: it had already moved
`origin/gated` to the release head, and the record keeps that sha
(`gated_seen` in the local `round.json`). A retry refuses when
`origin/gated` has since been deleted or moved back; the bound belongs
to the record, so `scripts/round cut --abandon` is what drops it.

The `confirm` node (`release_confirm`, `retries: 0`, pinned to the cut's
host like the deploy) then attests the runtime: every live worker's
heartbeat `code_sha`, read from the store with no network, must equal the
release sha, and when `release.health_url` is set the live web's
`X-Precis-Boot-Sha` header must too. On pass it first waits for the pass's
`merge_back` node to record `merged` or `noop` (up to two hours): the
script below merges the release head into main itself and stops on a
conflict that only the merge-back's resolver can settle. Then it runs
`scripts/round deploy
--confirm-runtime <sha> --runtime-evidence "<text>"` then `scripts/round
close`, stamps the evidence on the plan root and `release.round`, and the
release is tagged `deployed/r<N>`; a failed close still succeeds the job —
`scripts/release` retries the close on its next run. A mismatch or gap
fails the job before any script runs; when the web probe's is the only
failure the job files the ruling ask itself with the evidence, anything
else lets the tick's retry-cap ask go to a human; there is no rollback
and no `good/r<N>` tag — the factory attests, a failed probe asks,
never rolls back.

`release.health_url` is an optional operator setting
(`scripts/prod-precis settings set release.health_url <url>`) naming the
live web's `/healthz`. Unset means the web is not attested — the workers
still decide. The URL is operator-set, so the probe is a plain bounded
GET, not the SSRF-guarded `safe_get` — a private fleet address is fine
(gr607271). A failing or different-sha URL while every worker attests
files the confirm's own ruling ask (fix or clear the setting, then
answer to re-probe). No URL or host is printed in a job result or
failure reason.

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

`release.auto_deploy` (bool, default off, DB-only — no env fallback) is
standing approval for deployment. When armed at mint, the release pass
contains no approval node. Its deploy depends on the CI gate and is gated
by `precis.plans.release::may_release(deploy)`: no release window, no other
open pass, round or branch, prod not ahead of main, cleared engine reviews,
and a green verdict on the exact release head. GitHub ancestry and branch
reads fail closed; an outage holds deployment until the next successful tick.
The clock is checked at dispatch only. Once running, the script's window
guard refuses without spending a retry. Malformed windows hold cuts but
leave deployment to the script's guard.

    scripts/prod-precis settings set release.auto_deploy true
    scripts/prod-precis settings set release.auto_deploy false

With the setting off or unreadable at mint, the manual approval ask stays
unchanged: answer `deploy r<N>` or `hold`. Turning it off also holds an
already minted unattended pass. Existing legacy asks retain their answer
handling and never overwrite a human hold. The standing approval covers
every range, including migrations and harness diffs (me573231).
Rollback, superseding a partial rollout and exhausted release-fix loops
remain human decisions.

### Red-test waiver (release.red_waiver)

`release.red_waiver` (`on` | `off`, default `on`, DB-only; an unknown
value or a failed read reads `off` — only a stored or default `on`
grants) is Reto's "deploy anyway and gripe" ruling (2026-11-10):
one low-risk red test no longer costs a fix, a re-cut and 35-50 min of
CI. Only a TEST red qualifies — `ci_red_digest` rules an infra red first
(rerun, never a waiver). The digest node waives the red iff all hold:

- the red is a complete picture: the run has completed (one still in
  progress parks the digest and is re-read, never ruled on) and every job
  concluded `success`, `failure` or `skipped` (check.yml always skips its
  conditional jobs; a cancelled, timed-out, startup-failed,
  action-required, neutral or conclusion-less job refuses), it is not the `failed-first` job or the "skipped by design"
  matrix, there is no collection error (`error during collection`,
  `Interrupted:`, an `ERROR` on a file id), and each failed job's parsed
  ids equal its own `N failed` (+ errors) count with no cut id list or
  rescue (`_RESCUE_CAP`);
- 1-2 failing test ids parse from the digest (`MAX_WAIVED_TESTS`);
- no id (path or name) contains a test hard-stop word: migration/migrat,
  auth, oauth, vault, credential, secret, safe_fetch, ssrf, release,
  round, ship, deploy, gate, store, chunk, schema, invariant, backup,
  bump, ci_, waiver;
- the cut range (`meta.release.range` — prod..sha — read after a
  `git fetch origin`, `diff --name-only -z`; a head still missing
  refuses) has no path containing, anywhere, release, deploy, round,
  ship, backup, gate, migrat, bump, ci-, ci_, waiver, trust, auth, oauth,
  vault, credential, secret, safe_fetch or `store/`, nor matching the
  exact-file / prefix / glob lists in `plans/red_waiver.py`;
- a culprit commit is attributable (a range commit touching a failing
  test file or a src module it imports) — none, no waiver;
- none of the ids sits on a waiver gripe still unfixed (status outside
  done / wontfix / verify / auto_verify / user_verify / in_review),
  whatever its release number — a red is not carried forward.

Granted, the job files the waiver gripe, titled "release r<N> shipped
with known-red tests: <ids>" (body: run id, ids with assertion lines,
the culprit commits): tags `fix:code`, `broken-build`,
`release-waiver:r<N>`, `refs.prio` 1, `meta.release_waiver` carrying the
ids. It records the cut's `release.ci_verdicts` row as `green` with a
`waived` marker (the `tests-green` gate and the loop's `until` open; the
gate reason reads "CI red on <sha> WAIVED by gr<N>"), closes the
iteration's fix, recut and poll nodes `waived: gr<N>`, and writes every
step of the decision to the job summary. Refused, the summary names the
reason and the loop runs as before.

`gripe_dispatch` mints the waiver gripe's chain at once (no 15-minute
settle, no trust check, ahead of the lane slots) and the chain root's
`meta.claim_band = release` rides onto its `run_slice`/`land_slice` jobs,
which claim at the release legs' class (2.5), ahead of ordinary builds.

Safety net: when a later release confirms (`release_confirm`) and an
earlier waiver gripe is unfixed (status outside the fixed set above) with
no fix chain node `open` or `doing`, a "Revert the culprits of gr<N>"
`gripe-fix-direct` chain is minted — once per gripe
(`meta.release_revert_of`) — whose brief reverts the culprit commits
unless the fix has already landed (`moot: <sha>`).

Deploy side: `scripts/round` treats a release head whose exact CI is red
as the candidate (state `waived`, the waiver's age, a certificate naming
the gripe) when the newest `release.ci_verdicts` row for that sha is
green with a `waived` marker, read from prod like `release.soak_red`;
`scripts/release` `wait_ci` then proceeds and records nothing. The marker
is bound to ONE run (`run_id` + `attempt`): `round` honors it only if the
head's newest check.yml run (the exact verdict reports it) has exactly
that id and attempt — a newer run or a rerun voids it and its own verdict
governs — and a red recorded from the same run id never overwrites the
row (a red from another run does; so does a green). No row, no prod
access, an unbound or malformed marker, or any other field problem leaves
the gate as strict as before. The safety-net revert defers to a fix chain
only while it moved within 24 h.

### Who fixes a red release (release.fix_mode)

`release.fix_mode` (`clusters` | `owner` | `escalate`, default `clusters`,
DB-only; an unknown value or a failed read reads `clusters`) picks who a
TEST red that was not waived goes to. It is read when `ci_red_digest`
writes the brief, so a change takes effect on the next digest; change it
between passes.

- `clusters`: the digest splits the failing ids into clusters and mints
  one fixer per cluster on the foot of the ladder.
- `owner`: every test-red iteration goes to ONE fixer that owns every
  failing id of the run. The node is pinned to the ladder's top rung
  (`lanes.top_rung`) and stamped `meta.release_fix_owner`; no sibling is
  minted.
- `escalate`: iteration 1 runs as `clusters`, iteration 2 and later as
  `owner`.

The owner's spend is bound by its rung's `rung_caps` entry like any other
node: the job is minted with the remaining allowance and a spent cap parks
the node for rescue. A rung with no cap is unbound.

A builder lane cannot write to the graph. The fixer's standing knowledge
is [`release-fix`](./release-fix.md), which it extends in its own commit,
and the dispatcher files the `gripe:` and `unfixed:` lines of its hand-off
(`workers/release_fix_handoff.py`): one gripe per line, tagged
`origin:release-fix-handoff`, and the unfixed ids as a comment on the
pass's plan root.

    scripts/prod-precis settings set release.fix_mode owner
    scripts/prod-precis settings set release.fix_mode clusters

### Carrying a corrective commit (release.auto_carry)

The deploy gate holds a release whose engine land was cleared by a FIX or
REVERT until the corrective commit is on `release/r<N>`. A corrective that
landed on main after the cut is not on the branch. `release.auto_carry`
(bool, default on, DB-only; an unknown value or a failed read reads off)
lets the factory put it there itself (gr741489):

- `release_poll`'s open-pass tick mints one carry node under the pass
  root for every corrective the release still misses. The node is a
  `release_confirm` job in `carry` mode, so the tick pins it to the host
  and checkout the cut ran in. The pass shows it as a `carry` row.
- The job fetches `origin/main` and the release head, cherry-picks the
  commits with `git cherry-pick -x` in main's order inside a throwaway
  worktree, and proves each pick an exact copy of its source — the same
  tree proof the gate uses for a carried commit. It then pushes a plain
  fast-forward. No existing worktree is touched and nothing is forced.
- The corrective shas are the `landed:` records of the fix todos the
  reviews named, and only a commit on `origin/main` is picked. The
  branch copy is judged by the main land's review, as a hand pick is:
  no review hold is removed, and an engine commit nobody reviewed keeps
  its own hold.
- One carry of a release runs at a time (a per-release advisory lock; a
  second entry does nothing), in a worktree named for the run. The last
  look — the switch, the deploy start, the gate once more — and the push
  share the pass root's row lock, the one the deploy's start stamp
  takes, so a deploy cannot start between the look and the push.
- After the push the job puts the pass on the new head at once
  (`release_poll.follow_moved_head`): the head's CI verdict is polled
  before the deploy, and a red there goes to the fix loop.
- Nothing is pushed on a conflict, on a pick that is not an exact copy (a
  rename git followed, a merge commit) or on a git failure. The outcome is
  recorded on the node (`meta.release_carry.outcome`) and the carry ask
  files with the reason. The carry is not tried again until the release
  head moves.
- A fetch or a push that did not answer is tried again on the next tick,
  three times on one release head; then the ask files. So does a release
  head that moved under the push in three carries. The job itself ends
  `succeeded` either way: the node's outcome is the record.
- A carry node still open after 30 minutes stops holding the ask back.
- Never after the release's deploy started, and never for a hold whose
  carry ask was answered: an answer ships the release without the commit.
- A carry node never keeps the pass open. One that has nothing left to
  do — the deploy started, or the gate stopped waiting for its commits
  while it sat undispatched — is closed `won't-do` by the same tick.

Off, the carry ask files at once and a person carries the commit or
answers the ask.

    scripts/prod-precis settings set release.auto_carry false
    scripts/prod-precis settings set release.auto_carry true

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
3. The shared `release.round` record (`scripts/round` and the release
   job types write it)
   names no open round — a non-terminal state, any owner, is in flight;
   an absent record is "no round", an unreadable one blocks. One
   exception: a `cut` row whose recorded `pass` root is terminal with
   no deploy evidence (no `meta.release.confirmed`, the prod head does
   not name the cut sha) is a dropped pass — the poll writes it
   `abandoned` itself and goes on. `deploying`/`deployed` with a
   terminal pass keeps blocking and the blocker names
   `scripts/round supersede r<N>` (run where the publish works) or a
   hand `settings set release.round` with a terminal state.
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

## Stall watchdog (factory_rescue_tick)

Sends every plan node that went silent to RESCUE (one `rescue:<class>`
tag, one gripe per failure signature). Arm is an operator write — one
recurring todo, a job every 5 minutes:

    put(kind='todo', text='factory_rescue_tick (dark factory stall watchdog)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'factory_rescue_tick'})

Disarm: close or pause that todo. Dry run by hand: a `factory_rescue_tick`
job with `params={'dry_run': true}` — the job summary lists what it would
rescue and writes nothing.

The pass is DB-only and follows the same kill switch as `plan_dispatch`
(`PRECIS_FACTORY=off`, or a `plan_dispatch` `service_config` row with
`prio = 0`): read-only, lists would-rescue, writes nothing.

What it judges: a dispatchable node (or a `sha-in-prod` gate) whose newest
signal — its tags, comments, events or child jobs — is older than its
stage deadline: idle 20 min (also: cancelled newest job → `cancelled`;
pending-sha gate whose build already landed → `post-land-write`; a node
under a root closed terminal → `terminal-root` — and a signal-dead
non-terminal *job* left under that closed root is cleared
`STATUS:cancelled` outright, linked onto the same signature gripe,
rather than rescued (gr730548); no enabled lane fits the
node's class), queued 30 min, build 5 h, land 6 h, review 45 min, gate
72 h. A hold is not a stall: `waiting-for:`, `halt`, paused, an open ask,
`rescue:` or a live `blocked-by` target exempt a node; a lane-parked node
(fit lanes full or backed off) waits — the live jobs on those lanes are
judged by their own deadlines. A job-less node at a non-`open` STATUS or
under a `claimed-by:` lease is a hand lease, not a stall either
(gr659982): dispatch mints only for `STATUS:open` unclaimed candidates,
so a `doing`/`blocked`/claimed node with no job was moved by hand and
its silence is the claimer's work time. `release-pass`
plans are skipped (the
`release-stuck` watchdog owns release stages).

The `tick-silent` heartbeat reads the dispatcher's real liveness — the
newest `plan_dispatch`/`plan_tick` pass heartbeat in `worker_logs` or a
succeeded `plan_dispatch` job's STATUS stamp, whichever moved last —
with the recurring todo's own signals as a second source: silent only
when neither moved in 30 min, then it rescues the recurring todo once.
A live lane ticker (`--only plan_tick`) counts by its pass heartbeat,
so a factory ticked only by the lane (the prod setup — the recurring
todo paused, no job rows) reads alive; with no live recurring todo and
nothing moving there is nothing to tag, so the pass notes the gap.

### Diagnose and re-arm

The same tick's second pass acts on nodes already in RESCUE. A node
rescued under a task class (`builder`, `land`, `review`, `release`,
`lane`, `intake`) gets ONE diagnose task under its plan root, pinned to
lane `claude-sub` (one attempt, `meta.retries` 1, read-only by
instruction, `meta.rescue_for` links it to its node) and the node is
`blocked-by` it while it waits. Engine classes (`stalled`, `cancelled`,
`tick-raises`, `tick-silent`, `post-land-write`,
`wait-deadline`, `infra` — an unknown class reads as engine), a second
rescue (`meta.rescue.n >= 1`), and a rescued gripe skip the diagnose:
the ask goes straight up (a gripe is noted and left — `meta.ask.blocks`
takes a todo handle only). The exception is a node under a closed
ancestor (gr576258): a `terminal-root` rescue, or any class with a
`won't-do` up its own parent chain, has nothing to retry — the pass
closes the node `STATUS:won't-do` with one note (`root td<N> is
<state>; closed by the watchdog`), closes any live ask blocking it,
and files no ask; a bounded batch of `ORPHAN_BATCH` (20) closes a pass
drains an old backlog in a few ticks. The same bounded close — drawing
the same batch — applies to an already-old rescue (gr666159): a node
whose last real signal (its own tags/comments/events other than the
rescue pass's `rescue:` bookkeeping — a human comment or a `brief:`
update after the tag keeps it live — plus every child job's claims,
events and log stamps, a live job keeps it live) predates
`STALE_ASK_AFTER` (24h) is dead work, so
it closes `won't-do` (`silent since <ts> (> 24h); stale rescue closed
by the watchdog`) instead of minting a diagnose or filing an ask —
one ask per LIVE node, no stale-ask flood. Live engagement is never
closed over: a node with an episode ask, an open same-class ask or an
in-flight diagnose keeps its own path — and so does a node with a
nonterminal child job: every one of the job's signals can predate the
bound while it still runs, and the job may still land, so the stale
close (like the orphan close) defers until the job is terminal
(td673697 review).

The session replies on the task with one first line: `rescue: REARM —
<corrected brief>` or `rescue: NOFIX — <why>` (a `handoff:` prefix
counts; anything else is unparsable). REARM removes the `rescue:` tag,
returns `STATUS:doing` to `open` (the tick only dispatches `open`),
stamps `meta.rescue` (`n`, `at`, `diagnose`, `cls`) and the re-arm floor
(`rearmed_after_job` on the newest ended job, `rearmed_ask_at`,
`rearmed_answer` — so an infra-red land's re-land stays off), and posts
the corrected `brief:` comment the rebuilt run reads; the run continues
from the task's newest pushed work (`meta.build_ref`) in a fresh clone,
pinned to the host whose hub holds it (§Disposable workspaces). `rescue:
NOFIX fixed <sha> — <why>` (gr635531) closes the node `won't-do` and
the plan gripe `verify` with no ask when `<sha>` is an ancestor of
origin/main in the worker's fix repo (`PRECIS_FIX_REPO_DIR`); an
unverifiable sha is a plain NOFIX. NOFIX, a
failed or unparsable diagnose, and every straight-ask case file ONE
`ask-user` + `showstopper` ruling todo blocking the node; the diagnose
task closes `done` on a verdict, `won't-do` on a failure, and a
retry-cap ask the tick filed on it is closed superseded.

Read a pass on the node: its `rescue:` comments (`re-armed from td…`,
`asked td…`), `meta.rescue`, the `brief:` comment, and the diagnose
task's comments. After answering an ask, remove the `rescue:<class>`
tag — the pass never clears it. Dry run (`params.dry_run`) and the kill
switch cover the rescue pass too: `would` lists mint/rearm/ask actions
and nothing is written.

## CI watch (ci_watch)

Turns a red `check.yml` run into gripes. Each tick reads the newest
`lookback_runs` (default 3) completed `event=schedule` nightly runs on
main plus the newest 3 completed `release/**` runs carrying a `soak` job
(`gh api` on the worker's own credentials — no checkout needed; set
`release.repo` when there is none) and files ONE gripe per failure
signature: `ci:check.yml:<file>::<test>` (`[param]` stripped) for a
pytest red, `ci:check.yml:infra:<job name>` (tagged `fix:infra`, not
`fix:code`) for a failed job with no test node. Dedupe is the exact
title `nightly red: <sig>` / `soak red: <sig>`: open → a `seen again:`
comment; terminal → refile, under 7 days naming the old handle. An open
unresolved gripe with no live todo link is flake-closed after two
consecutive green runs; a `resolved: <sha>` gripe gets `green on <url>`
once a green head contains the sha. `ci_watch.seen` (cap 50) is the
idempotency ledger. Same kill switch as `plan_dispatch`: read-only
under it or `params.dry_run`, and `ci_watch.seen` is not written either.
Silence one signature with an open `ci:mute` tag on its gripe.

Armed by one recurring todo — a job every 30 minutes:

    put(kind='todo', text='ci_watch tick (CI run to gripe)',
        meta={'schedule': {'every': '30m'}, 'executor': 'job_inproc',
              'job_type': 'ci_watch'})

Disarm: close or pause that todo. The `fix:code` gripes reach a build
lane by themselves: `gripe_dispatch` opens its door to a gripe carrying
`meta.ci.sig` stamped `filed_by.principal = 'worker:ci_watch'` — the
repo's own CI output about its own code, not reader intake. The
principal is the key, not the client: a stdio `precis serve` client
binds no principal, so its writes fall through to
`worker:<PRECIS_CURRENT_SOURCE|cli>`; only the `job_inproc` tick's own
bound source (`tick_context(source = spec.name)` in the executor)
lands `worker:ci_watch`. `fix:infra`
gripes stay out of the lanes; route them to an operator.

## Todo dispatch (todos tagged factory:build)

A second source of work next to gripes: a plain open todo carrying the
open tag `factory:build` gets a chain (brief session, build, pre-land
review, `sha-in-prod` gate). The pass (`workers/todo_dispatch.py`) runs
right after `gripe_dispatch` in the same 10-minute fire, so only on a host
where `gripe_dispatch` is lit; under the kill switch it mints nothing.
Dark by default:

    scripts/prod-precis settings set factory.todo_dispatch.enabled true
    scripts/prod-precis settings set factory.todo_dispatch.max_open 3
    scripts/prod-precis settings set factory.todo_dispatch.enabled false

Off reads and writes nothing; started chains keep running. `max_open`
(default 3; negative reads as 0) bounds unlanded todo chains, a chain in
its brief step or stalled included. A pass where the gripe pass had free
build slots and found no eligible gripe may grow past it into those
slots; at most 3 new chains per pass, none ever cancelled.

Read a pass in the worker log of the lit host:
`todo_dispatch: base=… free=… open=… budget=… minted=… refused=… failed=…`.
The pass result also carries `minted`, `skipped` (first 50, plus
`skipped_total`), `refused`, `refusals_applied`, `released`, `closed` and
`open_todo`; the scheduler logs only the line and the claimed / ok /
failed counts. Reason words in `skipped`: `not-open`, `plan-node`, `ask`,
`hold-tag`, `needs-spec`, `skip-tag`, `open-child`, `blocked`, `chained`
(live chain), `closing` (landed, source not yet closed), `other-repo`
(a `repo:` / `project:` tag the factory does not land; one comment),
`settling` (changed in the last 15 minutes). `factory:skip` (set by the
todo triage session for "other lane or fenced") wins over `factory:build`.

Stop conditions:

- **Refused by code, no session spent.** A todo whose title or body
  names a path no lane may write, or a skill that governs the factory
  (by path or slug), gets one ask (reason `decide`, `fenced-path` in the
  report) and `needs-spec`; prod-graph-only work gets reason `not-code`.
- **Refused by the brief session.** One big-model session writes the
  builder's brief or refuses (`rewrite`, `split`, `decide`, `built`,
  `not-code`): one ask to the owner on the source todo, `needs-spec` on
  it, the chain closed `won't-do`. The owner answers in the ask with
  exactly one of `rewrite` (drops `needs-spec`; offered again once
  settled), `split` or `drop` (drop `needs-spec` and `factory:build`),
  `close as done` (also closes the todo). Read 2 minutes after the ask
  closes; none or several named: nothing changes, one comment says so.
- **Chain ended without a land** (dropped, or closed with no commit): the
  source loses `factory:build` with one comment; tag it again to offer it
  again. A brief session that ends as neither brief nor refusal is a
  failed attempt; at the retry cap (2 by default) the brief node enters
  rescue and files a rescue gripe like any exhausted node.
- **Tag withdrawn while a chain is live:** the build stops dispatching;
  after the stall window (`factory.gripe_dispatch.stall_hours`, default
  6) the gripe pass's stall sweep files a stalled-chain ask.

Completion: the land puts `landed: <sha>` on the source todo, which stays
open; once the sha is in prod the gate closes it with a
`done: <sha> is in prod` comment. A sweep each pass closes any the gate
missed.

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

Lane health self-heals (gr588221): the hourly `lane_health` job probes
each enabled lane's credential (a one-word `claude -p` ping on the slice
env, the devin/codex login status, the kimi membership check while its
lane is on); a red probe parks the lane on `parked_reason` — skipped in
dispatch exactly like `enabled=False` — and the next green probe unparks
it, while a lane still parked after 6 h files one `lane-parked:<lane>`
alert. The probe itself runs on a slice host — a detached child of the
claiming `ssh_node` worker — so it sees the same binaries and logins the
sessions do; a probe that cannot run reports `unprobed` and never parks.
`enabled` stays the human switch: nothing in the probe writes it.

## Slice hosts

A slice host is any worker whose heartbeat advertises `repo_checkout`,
`claude_bin` and `git`. No host is named anywhere: `run_slice` and
`lane_health` are claimed
only by a worker advertising all three; `land_slice`, `release_cut`,
`release_deploy` and `release_confirm` need `repo_checkout` + `git` (a
task's next build, review and land stay pinned to the host whose hub
holds its work, a re-cut, deploy or confirm to the host that ran the
cut). Until a host
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
  set to a name on `PATH` or an executable absolute path (a launchd `PATH`
  often misses it), **and** credentials available to the worker user:
  `CLAUDE_CODE_OAUTH_TOKEN` from the environment or vault/bootstrap lookup,
  `.credentials.json` under `CLAUDE_CONFIG_DIR` (default `~/.claude`), or
  the macOS `Claude Code-credentials` keychain item. Installing the CLI or
  creating `~/.claude` alone does not advertise the token; log in as the
  worker user first. This checks credential presence, not upstream validity.
- `git`: `git` on the worker's `PATH`.

Set the env vars in the worker's service environment and restart it; the
next heartbeat advertises the tokens. To check what a host advertises:

    scripts/prod-psql --ro "SELECT host, resource, capacity FROM resource_slots
      WHERE resource IN ('repo_checkout', 'claude_bin', 'git') ORDER BY host"

The other settings a slice host reads:

- `PRECIS_SLICE_ROOT`: the host checkout. Its git common dir holds the
  hub (`precis-hub.git`), and `run_slice` and `land_slice` cut their
  attempt clones under its `.claude/worktrees/`
  (§Disposable workspaces). Default: the main checkout of the repo the
  worker runs from. Set it when the worker has no git checkout around
  its code.

  Finished stacks: an attempt folder's compose project
  (`precis-test-<folder name>`) is brought down (`-v`) whenever the
  folder is dropped, and every land launch first sweeps the host's other
  slice stacks
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
- `PRECIS_KIMI_BIN`: the `kimi` binary a `kimi_tree` slice execs —
  `kimi -p <brief> --output-format stream-json` headless, like the devin
  lane's direct exec (never through `scripts/kimi-tree`, whose `-p`
  refusal is for interactive launches). Resolution is `PRECIS_KIMI_BIN`,
  then `kimi` on PATH, then `~/.kimi-code/bin/kimi`. The lane carries no
  precis MCP; the session authenticates through the `KIMI_MODEL_*`
  provider env the worker synthesizes from `KIMI_CODE_API_KEY` at submit,
  so it never touches the user's Kimi config. A missing binary or key is
  a host refusal — the same no-attempt shape as the devin preflight.
- `PRECIS_MCP_SLICE_TOKEN_DIR`: the shared session MCP's per-slice grant
  registry (`<mcp-http state dir>/slice-tokens`, created by the ensure
  script). Needed when the worker runs as a different user from the one
  running the server (the agent host: launchd `deploy` vs the operator): the
  default is under the *worker's* home, where no server reads, so the
  session would start on the worker's `PRECIS_MCP_TOKEN` — and a service
  account has none, which the session sees as `401 AUTH_HEADER_REJECTED`
  / "Not logged in" (jo546987). The deploy renders it on the gateway from
  the host var `precis_mcp_slice_token_dir` (no default: the dir is the
  operator's) in playbooks 20b and 20g; set the variable in the operator
  overlay, never the plist — the r224 redeploy (2026-11-06) re-rendered
  the plists with the hand-set value gone, and every slice after it
  launched "mcp token: shared" and built blind to the MCP (gr571617). The ensure script makes the dir setgid
  group-writable; the worker user must share that group (a builder
  account outside it needs the grant in "A host that is both the gateway
  and a builder host", step 3). The grant file
  holds the bearer's SHA-256 digest at mode 644, not the token: on a Mac
  host colima's virtiofs reads every mounted file as the colima user, so
  a 0600 grant owned by the worker account was unreadable to the server
  and every slice call answered 401 (jo547598). Before launching, the
  worker sends one `initialize` with the session's bearer to the
  `.mcp.json` URL and fails the attempt in seconds on anything but a
  2xx, so a broken grant never costs a session's wall clock.
- `PRECIS_SLICE_READ_MCP`: unset is today's behaviour — Codex and Devin
  code slices launch with no precis MCP. Set to `triage` (the intended
  production value) or `reader` and a `code`-class slice on a `read_mcp`
  lane (the `codex_exec` and `devin_tree` adapters) mints a *scoped*
  grant — the same grant-file machinery as a Claude slice's, plus
  `scope=`/`refs=` fields the server enforces (see "Slice grant read
  scopes" below). Refs mint as the todo itself plus the gripes its
  `fixes` links resolve to. A mint, preflight or config-write failure
  never fails the attempt: the grant is revoked and the session
  launches as today (`mcp token: none (<reason>)` in the job event).
  Generators and non-`code` task classes never mint — a child-minter
  needs the write-capable grant, which stays Claude-only.
- The session's `UV_CACHE_DIR` is `<tempdir>/precis-slice-ops/uv-cache`
  (`PRECIS_SLICE_OPS_DIR` overrides the parent), not the service account's
  `~/.cache/uv` — the checkout's hooks run through `uv`, and a root-owned
  home cache (a `sudo` bootstrap) killed every Bash hook in jo546987.
- A todo's repo is its own `repo:<name>` tag, else its nearest tagged
  ancestor's; no tag means precis. `plan_dispatch` skips a node whose repo
  is not buildable and open (`lanes.repo_refusal`) and stamps
  `params.repo` on the `run_slice` job for a non-default repo; submit
  refuses a `params.repo` that differs from the todo's tag, and later
  phases read the job, not the tag. `land_slice` lands a non-default repo
  only when the job's `params.repo` names it, the profile's `lander` is
  `land_slice`, `factory.open_repos` lists it and the todo's `repo:` tag
  agrees (`land_slice._repo_refusal`); it then hands the land to
  `src/precis/workers/repo_land.py`. For a non-default repo the only
  generator admitted is a reviewer (`templates.mint_reviewer(repo=)`),
  and only when the profile sets `reviewer` (catpath does, td659522 step
  5): it runs read-only in its own attempt clone, cut from the
  repository's hub at the landed sha, and leaves one verdict. Every
  other generator node stays refused there. Its review target is checked
  at launch, not at submit: a target that does not resolve yet is an
  attempt-free `review-wait`, backed off under the attempt-free cap.
- A non-default repo builds from its `lanes.RepoProfile`
  (`lanes.REPO_PROFILES`). `run_slice.submit` finds the clone through
  `PRECIS_SLICE_REPOS`, writes the profile's exclude lines into the
  clone's and the attempt clone's `.git/info/exclude`, fetches the base (`run_slice.fetch_base_tip`: a fresh
  ops gitdir outside the clone under the factory git credential, so no
  credential-bearing git runs inside the clone) and refuses unless the
  rev the precis `origin/main` `uv.lock` pins for the package is an
  ancestor of the repo's `origin/<base>` (`run_slice._prepare_repo`). The
  session gets a repo-specific brief (`run_slice._render_repo_brief`:
  read-first files, setup and lint commands, touched tests only, the
  profile's deny paths, and rules that beat the task text: no pull, no
  merge from origin, version bump, tag or pull request; a lane that
  pushes rebases onto `origin/<base>` from the local hub and pushes its
  attempt ref as its finish steps say, and a lander conflict is resolved
  in that rebase; Codex, which cannot push, gets no fetch, rebase or
  push, and after a lander conflict handoff one `git merge
  origin/<base>`), those deny paths, the profile's `UV_PYTHON` and no
  precis MCP.

Opening a second repo, in order:

1. Clone it once on the slice host with an https origin and no
   credentials in the URL. The worker never clones; it refuses a missing
   clone, an `origin` that is not the profile's https remote, or one with
   userinfo. The repository's hub lives in that clone's git dir.
2. Set `PRECIS_SLICE_REPOS` in the worker's environment: a JSON object,
   repo name to absolute clone path on that host, e.g.
   `{"<repo>": "/path/to/clone"}`. The slice lane (playbook 20g) and the
   collapsed worker (20b, which also runs `job_ssh_node`) render it from
   the host var `precis_slice_repos` (a dict, set in the overlay for the
   gateway; unset, the key is omitted). A host advertises
   `repo_checkout_<name>` only when its heartbeating worker unit has
   `PRECIS_SLICE_REPOS` naming an existing clone, and a second-repo
   `run_slice` / `land_slice` job is placed only on such a host. With no
   such host the job stays queued with the usual "no host advertises"
   note. The builder-host lane (20i) renders its own
   `PRECIS_SLICE_REPOS` from the host var `precis_builder_slice_repos` (a
   dict, no default, key omitted when empty; never the gateway's
   `precis_slice_repos`): a builder account's clone is its own, placed by
   the operator under that account. Unset, a builder host takes no
   second-repo slices. After the first deploy,
   read the rendered units and check the value is valid JSON.
3. Prove the factory git token from that host against the repo:
   `git ls-remote` under the credential, plus one API read.
4. The repo's profile must have a lander: `lanes.known_repos()` lists
   only those and the setting cannot open any other. catpath's `lander`
   is `land_slice`, so it is openable.
5. If the profile has a `releaser` (catpath does), prove the token's
   Actions read and write as "How a second-repo release is cut" says.
6. List the repo in the DB-only setting `factory.open_repos`
   (`lanes.open_repos`: the setting intersected with `known_repos()`).

catpath is openable but closed by default: it builds and lands only once
the operator lists it in `factory.open_repos`.

How a second-repo slice lands (`src/precis/workers/repo_land.py`), in order:

1. Take the repo's lease (`src/precis/workers/repo_lease.py`). A land that
   finds it taken parks and tries again every two minutes.
2. Read the commit to land from the repository's hub: `params.ref` must
   hold `params.head` (a land without `ref` is refused); fetch it into
   the host checkout. Then fetch the base tip (`run_slice.fetch_base_tip`).
3. Squash that commit into one commit in the job's land tree, a detached
   worktree of the host checkout (`slice-td<id>-land-j<job>`) cut on that
   tip.
4. Refuse the profile's deny paths and any change of `[project].version`
   in `pyproject.toml`.
5. Launch the profile's setup, lint and test commands detached, under an
   env with no credential (no ssh agent, gh config or docker endpoint).
   The commands are registry constants, never read from the tree.
6. On a green gate, re-check the lease (this job still holds it), the
   handle's sha (its parent is the tip, the diff still clears the deny
   paths), re-read the factory switch and the
   admission rules (`land_slice._repo_refusal`: the lander, `factory.open_repos`,
   the todo's tag against the job's repo), stamp `meta.landed_repo`, then
   push that sha as a plain fast-forward (`factory_git_mirror.push_cas`).
7. Write `landed: <sha>`, close the todo, remove the land tree, delete the
   task's hub refs, clear `meta.build_ref` and `meta.conflict`, stamp
   `meta.build_floor`, release the lease.

On the todo: a `landed: <sha>` comment on success. A `handoff: lander:`
comment marks a charged attempt: a red gate, a merge conflict, a deny-path
hit or a version change. The todo reopens with `attempt:<n>` and the hub
refs stay for the next build. These are attempt-free and the land is minted
again: a raced or unverifiable push, a gate that was killed or died without
an exit code, a setup failure with no dependency file in the diff, a
factory git mirror refresh that still fails after its retries
(`mirror-refresh`; the cause is in the failure reason), a repo
closed or a todo retagged during the gate, and the wall deadline.

The lander never force-pushes, opens a pull request, resolves a gripe,
changes the version, cuts a release or tags. A release is the separate
`repo_release` node below.

Kill switches: remove the repo from `factory.open_repos`, or set
`PRECIS_FACTORY=off`. Both are re-read right before the push, so a land
whose gate is running is not pushed.

How a second-repo release is cut (`src/precis/workers/job_types/repo_release.py`):

A release node is one mechanical todo per batch, minted with
`templates.mint_repo_release(store, plan_root, repo, blocked_by=[reviewer
ids], bump=None)` under the plan root: tagged `plan:<root>` and
`repo:<repo>`, `blocked-by` each reviewer, `meta.retries = 1` (a blind retry
would spend a version; submit refuses any other value). It never carries
the `release-pass` tag, so the precis release pass never sees it. It
refuses a repo whose profile names no `releaser` (catpath's is
`repo_release`), an empty `blocked_by` and a `bump` outside
`auto`/`minor`/`patch`.

It dispatches when all hold: the reviewers are done; no open slice or
reviewer node of the repo remains under the plan root (a `holding: N open
slice or reviewer node(s)` note names them, and nodes that wait on the
release, directly or through a phase, do not hold it); the repo is in
`factory.open_repos`; a host advertises `repo_checkout_<repo>`.

In order:

1. Take the repo lease as kind `release`. Lands of the repo queue behind
   it, and a waiting release outranks new lands.
2. Pick the version: bump `[project].version` by minor when a `feat`
   commit landed since the previous tag, else patch, never major; the
   `bump` param overrides. A tip that is already tagged is a no-op
   success (`released: ... (no-op: ...)`).
3. Edit `pyproject.toml` and the project's `uv.lock` entry as text (one
   line each), commit in a detached release tree.
4. Run the profile's `release_check_cmds` (catpath: `uv lock --check`)
   under an env with no credential.
5. Stamp `meta.repo_release` `state: pushed`, then push the commit as a
   plain fast-forward. A raced push prepares again on the new tip, at most
   3 rounds.
6. Wait for the profile's `ci_workflow` (catpath: `ci.yml`) run on that
   exact commit: one read every 120 s; the wait ends attempt-free after
   6000 s; GitHub unreadable for 1800 s ends it too.
7. A cancelled run is re-run, at most 2 times, only once no other run of
   the workflow is active on the branch.
8. Green: create the annotated tag `<tag_prefix><version>` (catpath
   `v`), write the `released: <tag> <sha> (CI run <id>)` comment and
   `meta.repo_release` with `state: tagged`, free the lease.

Failures, as the operator sees them (the node's `handoff:` comment has the
reason):

- Red CI: no tag. The commit stays on main. The record is `red`; the node
  asks; the next try takes a new version.
- The tag exists elsewhere (`tag-conflict`): tags are never moved, a human
  decides whether the version is spent.
- No CI run appeared 1800 s after the push (the push event was dropped;
  `ci.yml` has no manual trigger), or the re-runs are spent (a third
  cancel): the version is spent, a human decides.
- The token lacks Actions access (404 on the run read, 401/403/404 on the
  re-run): a human grants it. These three carry `meta.coordinator_ask`,
  so the node's cap ask goes to the coordinator.
- Attempt-free infrastructure (`meta.attempt_free = infra-release`,
  retried by itself) by `red_class`: `origin-fetch`, `tag-oracle`,
  `release-tree`, `release-read`, `release-record`, `tag-push`,
  `github-unreadable`, `ci-wall`, `batch-open`, plus the land classes it
  shares: `origin-raced`, `origin-push`, `land-handle`, `repo-closed`,
  `repo-held`, `gate infrastructure`, `gate setup`, `timed-out`.

Record states: `pushed` (committed and pushed, not tagged: the next job
resumes it, waits on the same commit and tags it; never recut), `red`
(CI failed; the next job starts fresh at a new version), `tag-conflict`
(a human), `tagged` (done). A resumed `pushed` record whose tag already
sits at its sha is recorded and closed.

Before listing a repo in `factory.open_repos`, prove from the slice host,
as the worker's user and with the factory git token, that the token can
read and re-run the repo's Actions runs. The read half, run in the
worker's environment (the vault lookup needs `PRECIS_DATABASE_URL`; a
plain login shell has none) and with `GH_TOKEN` unset:

    uv run python -c "import httpx
    from precis.workers import factory_git as g
    t = g.factory_git_token()
    assert t, 'the factory git token did not resolve'
    u = 'https://api.github.com/repos/<owner>/<repo>/actions/runs?per_page=1'
    h = {'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + t}
    print(httpx.get(u, headers=h).status_code)"

It must print 200; 404 means the token has no Actions read on the repo
(GitHub hides what it cannot read). The assert matters: with no token the
request goes out unauthenticated and a private repo answers the same 404,
and a control read against a public repo passes without any token. Nothing
prints the token, keep it that way. The write half can only be proven by
re-running a real cancelled or completed run of the repo (the "Re-run"
button's API call as the same token: `POST .../actions/runs/<id>/rerun`);
there is no dry run. Without Actions read a release can never tag; without
write a cancelled run needs a human.

Known limits:

- A release job that dies between its push and its tag frees the lease. A
  land may then push and cancel the release commit's CI run; the next
  release job resumes the `pushed` record and spends one re-run.
- The check and the gate run as the worker's OS user: no credential, not
  sandboxed.
- `v*` tags cannot be deleted or moved on the remote, so a wrong tag
  cannot be fixed.

How a second-repo batch is minted (`templates.mint_repo_batch`, the
`repo-batch` plan template):

    put(kind='todo', mode='plan', args={'template': 'repo-batch',
        'repo': '<repo>', 'slices': 'td1, td2', 'glue': 'td3, td4'})

- `slices` names existing build todos. Each must be open, a leaf with no
  live job, carry its own `repo:<repo>` tag, and carry no tag that parks it
  for the dispatcher (`waiting-for:`, `ask-user`, `halt`,
  `hold:no-dispatch`, a claim): remove such a tag before the mint.
- The dispatcher also reads holds through ancestors. A slice whose old
  parent chain carries such a tag, is paused, or is blocked-by an open
  todo is refused, because the move would lift that hold unseen: lift it,
  or detach the slice first (`link rel='parent', mode='remove'`).
- A slice that already carries a `plan:` tag is refused, so a retried
  mint cannot build a second batch over the same slices. A slice that
  waits (`blocked-by`) on a glue todo is refused: glue waits on the pin,
  so that batch could never start.
- The batch is its own plan: its parent, if any, sits outside every live
  plan and is none of the named todos.
- The mint moves each slice under a new root, which carries no repo tag.
  The slice keeps its body, its repo tag and its links; `meta.llm_tier` is
  dropped, and a comment on the slice records where it was moved from.
- Under the root it adds one reviewer per slice (blocked-by the slice),
  one release node (blocked-by every reviewer) and one pin-bump node
  (blocked-by the release, tagged for the default repo).
- `glue` names the todos that must wait for the new engine: the
  precis-side halves, a wave pointer. Each gets a `blocked-by` edge to the
  pin node and stays where it is, outside the root.
- `bump` (`auto`, `minor`, `patch`) is passed to the release node.
- The mint is one transaction: a refusal leaves nothing behind.
- It refuses until the repo's profile names a lander, a releaser and a
  pinner. catpath names all three (its pinner since 2026-11-15, after the
  arming proof below). Minting dispatches nothing by itself in a closed
  repo: the slices still wait on `factory.open_repos`.
- If the door fails, a dev session calls `mint_repo_batch` with the same
  arguments on prod (Reto 2026-11-15).

Gate image wait (F7, Reto 2026-11-15): the pin-bump node does not close on
its land. The land writes `landed:` and leaves the node open; ship's
land_notify is held for a pin land, mirrored or not, and the lander writes
the subject's other handles itself. The dispatcher's next tick mints a
`pin_bump` pinned to the land's host (`target_node`), which goes straight
to the wait. A no-op bump waits in the same job. Every 2 minutes the job
runs `docker image inspect precis-dev` on that host with the land env (no
credential) and reads the `precis.autocatpath_rev` label. The node closes,
and the glue unblocks, when that label is one of these:

- the pinned sha;
- the sha that precis origin/main's `uv.lock` pins, when this pin's land
  is under that main (a later pin of the same package landed first);
- a commit that the job's catpath mirror, as fetched when the bump ran,
  says descends from the pinned sha.

The job reads these locally and fetches nothing. A stale, unlabelled or
missing image files one ask, "td<pin>: rebuild the gate image on <host>",
under the plan root. The ask does not block the node, because the open
node already holds the glue. Docker that does not answer files the ask
after 6 h. If the ask is closed while the image still lacks the pin, it is
filed again, at most once an hour. A wait behind for over 6 h also raises
the `pin_bump` `stale-open` alert, even while its job is parked. To clear
it, run
`scripts/build-image precis-dev` on that host from a precis checkout at or
after the pin's land (pull first). The node and the ask close by themselves
within about 2 minutes. The factory never builds the image. Only the land
host's image is checked. A hand close of the node, or a hand
`scripts/ship … (td<pin>)` that closes it, ends the wait; the job then
succeeds quietly and the dispatcher closes the open ask.

Arming a pinner: before a profile names `pin_bump`, run the job's derive
step once on the slice host against today's pin. As the worker's user, in
the worker's environment, in a throwaway directory: a `git archive` of
origin/prod, a throwaway catpath mirror filled by
`factory_git_mirror.fetch_source_mirror`, the env from
`pin_bump._detached_env`, then `uv lock --check`,
`uv sync --frozen --extra catalyst` and `pin_bump.DERIVE_SCRIPT_PY` with
the pinned version. The key it writes must equal the anchor literal
committed in `tests/test_coverage_job.py`. catpath passed on 2026-11-15
(autocatpath 0.50.0, 15 s).

Known limits:

- A parked image wait is a running `ssh_node` job: it holds one of the
  land host's executor slots until the image is rebuilt.

- Second-repo lands hold a per-repo lease for the whole gate
  (`src/precis/workers/repo_lease.py`). A land that finds it taken parks and
  retries every two minutes without charging the builder; after six hours
  it fails attempt-free (`repo-held`) and is minted again. A hand push to
  the repo's main can still race: the push is a plain fast-forward and a
  raced push is retried.
- A `landed:` sha on such a todo belongs to the other repo
  (`meta.landed_repo`, `lanes.LANDED_REPO_KEY`). A plan for it must carry
  no `sha-in-prod` gate.
- Of the generator nodes, a second repo admits only a reviewer, read-only
  in a detached tree at the landed sha, on a profile that sets `reviewer`;
  every other generator stays refused there. The read-only part is
  advisory: the session runs with permissions bypassed like a builder,
  and a commit or a dirty tree (`run_slice._review_violation`) only adds
  a line to the job summary. Nothing is landed from the tree, and it is
  removed after the run. "No credential" means the session's environment
  only (no token, no MCP grant): `HOME` and `SSH_AUTH_SOCK` still pass,
  the same exposure a builder has.
- The gate runs as the worker's OS user: it holds no credential but is not
  sandboxed.

- The session runs `--permission-mode bypassPermissions` with the lane's
  `deny_write_paths` (`STRICT_DENY` in `src/precis/plans/lanes.py` — the
  full `FACTORY_DENY_WRITE_PATHS` plus the never-relaxable
  `HUMAN_ONLY_PATHS`) passed as `--settings` deny rules.
  With the DB-only setting `factory.deny_relaxed` on (default off —
  nothing turns on by itself) the set shrinks to `HUMAN_ONLY_PATHS +
  INTERIM_DENY`: secret-placing deploy files, inventories and the
  harness's own rules, plus the check-bearing modules until the
  td574934 A3 hold deploys. The poll re-checks the branch diff and
  working tree; a hit fails the attempt and reopens the todo. The
  session never lands: a clean exit mints a `land_slice` job on the
  same host that runs `scripts/ship --quick` in its own clone cut from
  the hub (`PRECIS_SHIP_BIN` overrides it), so the check is pre-land; that host
  needs the ship's git push and land_notify DSN. A land whose diff
  touches an `ENGINE_PATHS` glob stamps `meta.engine_paths` on its job
  and ships `--checked` when the selector's answer is a small spot
  selection — else `--quick` like every land (gr599427, Reto
  2026-11-08: lands run lint plus a spot check; GitHub CI is the gate —
  a FULL-selector refusal relaunches `--quick`, never `--full`); until every
  engine commit in `origin/prod..origin/main` — read from git, not
  jobs, so a hand land with no `land_slice` job still gates
  (gr597625) — is cleared by a covering reviewer closing LAND
  — or FIX whose fix task landed — `release_poll` mints no pass and
  `release_cut` refuses. A wait that can still resolve files one
  `release-review-wait:td<land>` ask once the newer of the land and
  its newest covering reviewer is 24 h old; a land with no covering
  reviewer gets one minted under the plan root its own `plan:` tag
  names (a commit no todo links — or whose linked todo has no live
  plan root — gets a sha-keyed reviewer under a fresh phase root),
  `blocked-by` the landed todo so a REVERT verdict
  holds it
  through the rebuild, at most two mints a pass — the ask files at
  once only where minting cannot run (no review-fit lane, the mint
  raised), where `plan_dispatch` itself is switched
  off, or the wait can never resolve — a dead verdict, a dead root,
  a fix that ended unlanded, a reviewer parked in rescue (an open
  `rescue:` tag) — and an answered ask clears that one
  land. An ask closed without an `answer:` comment holds nothing:
  the next pass refiles it and the blocker names the live ask's id.
  A land cleared on a corrective commit that is not on `release/r<N>`
  holds the deploy too. The factory first carries the commit onto the
  branch itself (§Carrying a corrective commit); the same ask files only
  when that cannot be done, or at once with `release.auto_carry` off.
  Answering it deploys the release without that commit; the commit on
  the release branch clears the hold without an answer — a docs- or
  test-only one included, which is no review unit but still counts as
  carried. While the gate holds a deploy, the tick writes the
  refusal as a comment on the `release_deploy` node, once per distinct
  text (gr702442). Two kinds of commit on `release/r<N>` bring no code
  of their own and get no review of their own (Reto 2026-11-19): a
  commit that repeats a main commit patch for patch (a cherry-pick) is
  judged by that main land's review, and carries it for the hold above;
  an exact revert of a commit of the same release drops out together
  with the commit it undoes. Both are proved on the trees, not read
  from a subject or trailer: replaying the main commit onto the branch
  commit's parent (or the undone commit backwards) must give the branch
  commit's tree exactly. A revert of code prod already runs, a revert of
  a revert, and any commit with a change of its own — an indentation
  change included — keep their review.
- The file-overlap hold is bounded. A started, unlanded build keeps
  other builds that touch its files out of the claim (td603849), but
  only for `factory.overlap_hold_max_runs` ended `run_slice` runs
  (default 3; cancelled runs, host refusals and attempt-free failures
  leave the hold budget intact).
  After that it holds only while its `land_slice` is live, and a real
  conflict with the work that went ahead rebuilds at its land. `0`
  restores the unbounded hold. The hold writes nothing on the held job:
  the tick's `file-overlap hold — waits on td<N>` note names the holder.
- The session's env is an allowlist (`ENV_ALLOWLIST` in `run_slice.py`):
  no DB DSN or vault variable reaches it. It needs `PRECIS_MCP_TOKEN`
  (env or the mounted secret) to reach the precis MCP, and the claude
  login under `HOME` (or `CLAUDE_CONFIG_DIR`); `ANTHROPIC_API_KEY` is
  not passed.

## A host that is both the gateway and a builder host

Ruling 2026-11-21: factory builds on the gateway run as the least-privilege
`coder` account like the other builder hosts, not as `deploy` (which has
sudo). Two identities then sit on one machine: `<host>` (deploy: the plan
ticker, release jobs, the collapsed worker, the fix lane) and `<host>-coder`
(the 20i `worker-builder` unit: `run_slice` and `land_slice`). Both claim
`run_slice` until the deploy identity is switched off it, so the order below
matters. Operator steps; the coordinator does not run them for a builder.

1. Overlay: the host in `builder_hosts` with `precis_builder_user: coder`;
   `precis_builder_mcp_host` set to this host (no tunnel is rendered on the MCP
   host itself); `precis_mcp_slice_token_dir` (already set for deploy's
   units, rendered into the builder unit too); optionally
   `precis_builder_slice_repos` (a second repo's clone under the builder's
   home, placed by hand) and, on macOS, `precis_builder_colima_cpu`,
   `_memory`, `_disk` (integers, GiB; empty keeps colima's own defaults, as
   deploy's VM has: this tree sizes neither; applies at the next
   `colima start` from stopped).
2. Hold the builder at zero before anything can claim: set the cap key
   `claim.executor_cap.ssh_node.<host>-coder` to 0 through the claim-setting
   door (`put(kind='job', mode='setting', args={'key': ..., 'value': 0,
   'reason': '...'})`). Placement advertises a lane the moment its binary and
   login exist, so an installed but logged-out CLI would otherwise take a
   build and fail it.
3. Give `coder` the slice-token directory (the operator's, setgid and
   group-writable for a general group `coder` is deliberately not in).
   Without it `run_slice` does not fail: `slice_tokens.registry_dir()`
   returns `None` for a directory it cannot reach, and a failed mint is
   logged and swallowed (`_mint_slice_token`), so the session launches on
   "shared" (the builder has no `PRECIS_MCP_TOKEN`: preflight is skipped and
   every MCP call answers 401). Narrowest grant: an ACL entry for `coder`
   on that one directory, no group and no change to the ensure script
   (its `chmod g+ws` leaves the entry in place). macOS:
   `chmod +a "coder allow list,search,add_file,delete_child,readattr,readextattr" <slice-tokens dir>`;
   Linux: `setfacl -m u:coder:rwx <slice-tokens dir>`. If
   `sudo -u coder test -w <dir>` still fails, a parent denies traversal
   (`setfacl -m u:coder:x` / `chmod +a "coder allow search"` on it). A
   dedicated group is the alternative, but it needs `deploy` in it too
   (its mints use the general group today) and the role's account task
   (`groups: ""`) strips supplementary groups on every run. Untried on a
   host: check the next launch event says `mcp token: slice td<N>j<M>`.
4. `scripts/host-ops/play deploy/playbooks/20i-precis-builder-hosts.yml`:
   creates the account, its checkout, rootless Docker or its own colima, the
   coverage map copy (macOS) and the unit, which heartbeats as `<host>-coder`
   and claims nothing at cap 0.
5. `scripts/agent-cli-update --host <host> --apply`: one pass per account that
   runs model CLIs, `coder` then `deploy` (`agent_cli_users`), so both stay
   current.
6. Vendor logins as the builder, in a real terminal:
   `ssh -t <host> 'sudo -iu coder claude'` (then `/login`),
   `... codex login --device-auth`, `... devin`. deploy's own logins stay.
7. Smoke test as `coder`: `claude -p "reply ok"`,
   `codex exec --skip-git-repo-check "reply ok"`, `docker info`; and read
   `resource_slots` for `<host>-coder` (`builder`, `repo_checkout`, the lane
   `*_bin` tokens).
8. Raise the cap to the real value. Both identities now claim builds; watch
   one build end to end as `coder` (launch event, land, `scripts/ship` gate).
9. Drain, then switch the deploy identity off `run_slice`. A task whose hub is
   in deploy's checkout (`meta.build_ref.host` = `<host>`, cleared when it
   lands) pins its next build and review to `<host>`, which a switched-off
   deploy identity will not claim: let those land first. Check:
   `scripts/prod-psql --ro "SELECT ref_id FROM refs WHERE kind='todo' AND
   retired_at IS NULL AND meta->'build_ref'->>'host' = '<host>'"`. Then set
   `precis_deploy_claims_run_slice: false` on the host and redeploy 20b and 20g.

What the switch does, and does not: both deploy units render `run_slice` into
`PRECIS_SUSPENDED_JOB_TYPES`, so the deploy identity claims no build, and its
heartbeat stops advertising the `builder` floor, so `gripe_dispatch` stops
counting its slots as build capacity. The plan tick keeps minting builds; the
lane picker and the host-agnostic placement are untouched. release_cut,
release_deploy, release_confirm, ci_red_digest, ci_rerun, pin_bump,
repo_release and any `land_slice` pinned to the bare `<host>` keep draining on
deploy; a land minted by a `coder` build pins to `<host>-coder`. Existing
knobs were checked and do not do this: the per-host executor cap and reserve
act on all of `ssh_node`, `claim.max_running.<job_type>` has no per-host form,
and `service_config` pins apply to no run_slice. The play refuses the switch
when the host has no managed builder user and `builder_hosts` names no other
host. A fleet-wide operator hold of
`run_slice` now also withdraws the `builder` floor everywhere, so queued
builds raise the unschedulable alert for its duration. Rollback: the variable
back to `true` and redeploy.

Builds on other builder hosts only: the same variable works before the gateway
has a builder account of its own. With `precis_deploy_claims_run_slice: false`
and at least one other host in `builder_hosts`, the gateway keeps the plan
ticker and the release jobs and builds nothing; the builder hosts whose cap
key is above 0 take every build. Step 9's drain applies unchanged. Until the
"Before a second build host is enabled" items below are closed, keep exactly
one builder host above cap 0.

## Disposable workspaces (where a build's work lives)

Nothing important lives only in a build folder (gr741695). The rules are
the docstrings of `src/precis/workers/job_types/_slice_ws.py` and
`src/precis/utils/hub_hook.py`; this section is the operator's view.

Where the work is, stage by stage:

- **The hub.** One bare repository per host checkout, `precis-hub.git`
  in the checkout's git common dir (`git -C <checkout> rev-parse
  --path-format=absolute --git-common-dir`). The worker makes it
  (`_slice_ws.ensure_hub`) and moves its `refs/heads/<base>` to
  GitHub's tip by a hub-side fetch at each launch. Nobody works in it.
  Builder traffic stays on the host: sessions push to the hub, never to
  GitHub; only the lander writes to GitHub.
- **A build or review.** Each `run_slice` job cuts its own fresh clone
  `<checkout>/.claude/worktrees/slice-td<todo>-j<job>` from the hub, on
  local branch `slice/<todo>`, `origin` = the hub. It starts at the
  task's newest pushed attempt, else the base tip. Before a session runs
  on pushed work the worker rebases it onto the current `origin/<base>`
  (`_slice_ws.rebase_start`; work that holds a merge commit is left
  alone). The rebased commits are that job's own work, pushed under its
  ref even when the session adds nothing. On a conflict the worker
  aborts the rebase, leaves the clone at the pushed commit and names the
  paths in the brief. No folder is reused by a later job. A reviewer's
  clone gets the reviewed attempt's ref fetched as
  `origin/slice/<target>`.
- **The job's hub ref**, `refs/factory/build/<todo>/<job>`, one per job.
  Lanes that push (Claude, Devin, Kimi) finish by committing, rebasing
  onto the hub's `origin/<base>`, running the checks again, pushing to
  that ref and reading it back. Codex does not push: the worker commits
  and pushes after it exits.
- **Job end.** The worker finalizes the folder (`_slice_ws.finalize`):
  every tip it holds reaches the job's ref, unfinished work as one
  `wip: snapshot` commit, other branches, stashes or an in-flight rebase
  as extra parents of one more commit. The job's `meta.push.state` is
  `pushed`, `empty` (nothing new), `withheld` (the hub's secret scan
  refused) or `failed`. On `pushed` or `empty` the folder is dropped,
  its compose test project with it. Otherwise it is kept: the task gets
  a comment `run_slice: the folder <path> on host <host> was kept:
  <why>.` and a warn alert `build folder kept: <folder>` is raised.
  The job's own poll does this when its session ends. For a job that
  ended any other way (a stop, a deadline, a crash) the host sweep does
  it, within about half a minute. A `failed` push is tried again by the
  sweep after 30, 60, 120 and 240 seconds before the folder counts as
  kept; a `withheld` one is not, the secret has to leave the commits
  first.
- **A land.** `land_slice` cuts its own clone
  `slice-td<todo>-land-j<job>` from the hub at `params.head`, points
  its `origin` at GitHub and runs `scripts/ship` there. Green: the clone
  is dropped, the task's hub refs deleted, `meta.build_ref` and
  `meta.conflict` cleared and `meta.build_floor` stamped with the land
  job's id (an attempt ref of an earlier job is stale from then on).
  Red: nothing is saved, the hub already holds the commit; the clone is
  dropped. The second-repository lander keeps a detached worktree of
  the host checkout under the same name. `pin_bump` puts its commit in
  the hub as its own attempt ref before it mints the land. A PR land
  (`params.pr`) keeps its linked worktree.

**No locks; one writer at a time.** Nothing here takes a lock, on a
folder or in the hub. A job has its own clone and no job enters
another's. Every save and every delete of a build folder runs in the
worker's watch pass on its host (`ssh_node`, one worker at a time per
host, one folder after another): the job's own poll when its session
ends, else the host sweep (`run_slice.tidy`, at most every 30 seconds,
`_slice_ws.sweep`). A launch cleans nothing up and `release` touches no
folder. The sweep also raises the alert for each folder it keeps,
resolves the alert of a folder that is gone and removes failed cuts
(`*.cut-*`) and half-removed folders (`*.drop-*`) older than an hour.

Three rules decide what the pass may do with a folder:

- **Deleted only after a save in the same pass.** A folder is removed
  only when a finalize of it in that pass came back `pushed` or
  `empty`. A stored `meta.push` alone never licenses a delete; a
  finalize of an unchanged folder gives the same commit again and
  pushes nothing.
- **Never under a live session.** Every launch leaves a run dir,
  `<ops>/td<todo>/<32 hex>/` with `baseline`, `pid`, `handle`, `exit`
  and `log` (`<ops>` is `precis-slice-ops` in the worker's temp dir).
  A job whose stored handle names no process (a paused job, a launch
  whose handle was never saved) is looked up there. A session found
  alive keeps its folder, and a relaunch adopts it instead of starting
  a second one. A process whose identity cannot be read in a pass (`ps`
  timed out) counts as alive in that pass.
- **No signal without an identity.** A stop signal goes to a process
  group only when the launch recorded who its leader is and one read in
  that pass confirms it, or the leader is gone and only its group's
  members answer. Otherwise the pass waits; the job's deadline ends it.
  A terminal job's session that still answers is stopped after 30
  seconds and reported as stuck after 15 minutes.

A launch for a task whose earlier build folder on the host is not yet
settled waits for the sweep: one job event, `run_slice: td<N> launch
waits for the earlier folder <folder> to be settled`, then a look every
30 seconds. This costs no attempt. After 20 minutes (the push retries
plus their allowance) the job ends attempt-free (`workspace`) and the
message names the earlier job's state; the usual cause is a job row
that never went terminal or a session process that will not die. A
folder whose job row is missing, or belongs to another task, is not
waited for.

**What a kept-folder alert asks for.** The alert `build folder kept:
<folder>` says which case it is:

- the work is not in the hub (a failed or withheld push): read and push
  it, see "A kept folder" below;
- the work is in the hub but the folder could not be removed: remove
  the directory by hand;
- a process still answers in it after the job ended: find and stop the
  process, then the sweep settles the folder;
- no job of this worker's database owns the folder (it was cut under
  another database, or its job row is gone): look at it by hand, the
  sweep never judges it;
- a leftover (`*.cut-*`, `*.drop-*`, or a name that is no attempt
  folder) that could not be removed: it holds no work the hub lacks,
  remove it by hand;
- the sweep itself failed on it: the alert quotes the error.

**Hub tidy-up.** Once a day the sweep runs git's incremental
maintenance on the hub (`git maintenance run --task=loose-objects
--task=incremental-repack`). A run packs loose objects and combines
small packs, and deletes only the files the run before it replaced, so
a clone, a push or a fetch beside it loses nothing and nothing waits
for it. It never removes objects nothing refers to (a landed task's
old attempts), so a hub grows slowly. Never run `git gc` or `git
repack -d` on a hub while jobs can start: they write and delete packs
in one step, and a clone that copies files in that moment can come out
incomplete. With the factory stopped and no `run_slice`/`land_slice`
job running on the host:

    git --git-dir "$hub" gc --prune=2.weeks.ago

(`--prune=now` reclaims everything at once, including the commits of an
attempt ref deleted by mistake.) The tidy-up needs git 2.30 or newer;
on an older git the sweep logs `hub maintenance FAILED` once a day and
nothing else changes.

**Host pin.** A hub is local to its host. While a task's
`meta.build_ref` names a host, its next build, review and land are
minted with `params.target_node` = that host (job `meta.hub_pin`); a
`target_node` naming another host loses to the pin, with a note.

**A task's newest work.** `get(kind='todo', id=T)` shows
`meta.build_ref = {state, ref, sha, head, wip, by, note, host, at}`:
`ref` is the hub ref, `sha` its tip, `head` the builder's last real
commit (`sha`, or the commit under a `wip:` snapshot; a land takes
`head`), `by` builder or worker, `host` the build host. A job's own
result is its `meta.push` (same shape); `meta.start` is the commit it
started from. On the build host:

    hub=<git common dir>/precis-hub.git
    git --git-dir "$hub" for-each-ref refs/factory/build/<T>/
    git --git-dir "$hub" log --oneline main..refs/factory/build/<T>/<J>
    git --git-dir "$hub" reflog refs/factory/build/<T>/<J>

A launch whose recorded ref no longer holds `build_ref.sha` ends
attempt-free (`workspace`) with the reflog command in its message: put
the ref back, or clear `meta.build_ref`. It never falls back to the
base.

**A kept folder.** On the build host, read it (`git status`, `git log
origin/<base>..`). To keep its work, push it from the folder with the
worker's flag; the hub's hook refuses a push with no session or worker
identity:

    PRECIS_HUB_WORKER=1 git push origin HEAD:refs/factory/build/<T>/<J>

Never add `+`: it can drop commits the hub ref already holds. A refusal
as not a fast-forward means the hub holds other work under that ref:
leave the folder as it is and file a gripe naming it. A `precis-hub:
refused (secret-scan)` names `path:line`: take the secret out of every
commit (amend or rebase) and push again. The next attempt continues
from the newest attempt ref by job id at or above `meta.build_floor`.
To discard the work, remove the folder (bring its `precis-test-<folder
name>` compose project down with `-v` first, if it has one). The alert
clears at the next sweep on that host once the folder is gone (within
about a minute while a worker runs there), or by hand:
`tag(kind='alert', id=<id>, add=['alert-state:resolved'])`.

**A task pinned to a dead host.** The pinned job stays queued. After 30
minutes the dispatcher files one ruling ask, `td<N>: job jo<J>
unplaceable <m>m — provision a host, wait, or drop the node?`, whose
body names the pin. A `start over` answer cannot help: its launch must
run on that host too. To release the pin by hand: cancel the job and
clear `meta.build_ref` (and `meta.conflict` and `meta.conflict_tries`,
and `meta.pending_land`, if set) on the task. That loses the work held
on that host: the task builds again from the base. The agent edit
doors do not take these keys; it is an operator write on prod:

    scripts/prod-psql "UPDATE refs SET meta = meta - 'build_ref' - 'conflict'
      - 'conflict_tries' - 'pending_land', updated_at = now() WHERE ref_id = <T>"

**A land conflict.** A land red on a rebase or merge conflict against
the base stamps `meta.conflict = {head, ref, base_tip, files, at}` on
the task, which reopens with `meta.build_ref` standing. The next build
is a conflict-only attempt (`params.conflict`, `try` from 1): tries 1
and 2 on the rung the build ran on, try 3 on the ladder's top rung,
counted in `meta.conflict_tries` and not as ordinary attempts. A try
fails when it ends without pushed work, when the land conflicts again,
or when its review says FIX. A pushed resolution gets a resolution-only
pre-land review (`params.resolution = {of, ref}`; the reviewer reads a
`git range-diff`), then its land; both claim first. While that land is
pending, the tick mints no other land of the same repository and base,
for at most 45 minutes from the push. A third failed try files one
coordinator ask, `td<N>: land conflict not cleared by 3 attempts — a
coordinator call`: resolve by hand or leave a `brief:` and close the
ask `acknowledged` (a fresh set of tries), answer `start over`, or close
the task `STATUS:won't-do`. Once a later land ends for another reason,
or `build_ref` is gone, the tick clears `meta.conflict` and
`meta.conflict_tries` with a note.

**Harness variables in a clone.** A clone's git dir is its own, so the
worker names the host's shared state in the env: a build session gets
`PRECIS_SLICE_ID=td<todo>` and `PRECIS_SLICE_JOB=<job>` (the identity
the hub's hook admits) and `PRECIS_GATE_SLOT_ROOT` (the host checkout's
git common dir, so `scripts/test` queues on the host's gate); a land
clone gets `PRECIS_GATE_SLOT_ROOT` and `PRECIS_ROUND_STATE_ROOT` (so a
`ship --onto` reads the host's release round journal and lock). Without
them, `scripts/lib/gate-slot.sh` and `scripts/lib/round-lock.sh` in an
attempt clone derive the host's common dir from its path, or refuse; a
set but missing directory refuses.

Still the host's, unchanged: the per-slice Docker test stack and its
cap, and the lander's own `index.lock` wait in its ship folder (an
attempt-free `index-lock` re-land without a builder).

Cutover checklist (the deploy that brings this in):

- Zero queued AND running `run_slice`/`land_slice` jobs before the
  deploy.
- Survey, then remove, the old `slice-td<id>` worktrees and local
  `slice/<id>` branches on the build host. One with a large unsaved
  change goes to Reto first. A launch imports a task's work from
  GitHub's `refs/factory/slice/<id>` once, so only work that never left
  the host is at stake; a land minted before the switch (no `ref`) also
  reads the local `slice/<id>`, so keep that branch until it landed.
- Open asks titled "index.lock stuck/re-formed" no longer close by
  themselves: close them by hand.
- The worker's interpreter must be able to import `precis`: the hub's
  hook stub runs it.
- An existing hub gets its hook path and the new hook stub at the first
  `ensure_hub`.
- `git --version` as the worker's user is 2.30 or newer (the hub's daily
  tidy-up).

Before a second build host is enabled:

- git 2.30 or newer on that host.
- Every worker process on a host gets the same `PRECIS_SLICE_ROOT` and
  `PRECIS_SLICE_REPOS`: a process with another list would resolve the
  other's kept-folder alerts on every pass.
- Lane choice must respect the pinned host (`_escalate`,
  `_review_lane` and the conflict path's try 3 do not check it yet).
- A reviewer whose targets sit on different hosts needs a rule (today it
  is pinned to the first by id).
- Stale refs of a task in another host's hub after a start-over must be
  handled.
- Releasing a task pinned to a dead host must be an answer on its ask,
  not a hand edit of the task's meta.
- The Codex sandbox can write the hub directory (gr742453).

Known limits:

- A land minted directly by a builder job, or already queued, is not
  held behind a pending conflict resolution.
- Work a session has not pushed is lost if the host's disk is lost
  before the job ends.
- The watch pass is one at a time per host by a database lock with two
  exceptions, as for every job type: the lock's connection cannot be
  opened, or a pass holds it longer than 15 minutes. Two workers on the
  host can then run the pass at once.
- A clone cut in the same second as the hub's daily tidy-up can fail on
  a file deleted under it. The launch then ends attempt-free
  (`workspace`) and the next one succeeds.
- A second-repository squash conflict takes the ordinary charged
  handoff, not the conflict path.
- The hub's daily tidy-up runs inside the watch pass and outside the
  sweep's 60-second budget: once a day the polls of that host can wait
  on it, for seconds as a rule, for at most five minutes.
- A Devin lane's acceptance-test run whose process identity could not
  be recorded at its launch is never signalled. The poll holds the
  folder until the job's deadline; after that the sweep does not count
  that run, and can save the folder while it still writes.
- A new attempt can start while an earlier folder of the task with a
  stored `pushed` record is still on disk. If that folder held work
  newer than its record, the sweep saves it under the earlier job's
  ref and the new attempt does not continue from it.
- The host sweep removes a run dir under the worker's
  `precis-slice-ops` once the dir is a day old, its job is terminal and
  nothing of its session still answers (gr743294). The newest run dir
  of each task always stays. A dir whose job cannot be proven terminal
  — no launch record, an unreadable or another task's job row — is
  never removed.

## Slice write scope (mcp.slice_scope)

The scope gate (`precis.principal.check`, td575705 G1) bounds what a
validated slice grant may write by the role its own todo declares —
reads always pass, and the role→write table sits in the module
docstring. Armed by `mcp.slice_scope`, DB-only (no env var) so the
sessions it gates cannot flip it:

    scripts/prod-precis settings set mcp.slice_scope observe   # the default
    scripts/prod-precis settings set mcp.slice_scope enforce
    scripts/prod-precis settings set mcp.slice_scope off

An unset row reads the registered default `observe`; an unknown value
observes too and warns once — a typo'd mode never silently opens the
scope. A failed `app_settings` read keeps the last good mode and warns
once — an armed `enforce` cannot decay to `observe` during a DB
outage, and a process that has never seen a good read fails closed on
`enforce`.

Arming ladder:

1. Ship with `observe`. A write the role's grant does not cover still
   runs, but the call's `tool_calls` row is marked
   `error_type='SliceScopeWouldRefuse'` with the
   `role=/rule=/verb=/kind=/target=` k=v line in `error_head`, and a
   `precis.slice_scope` log line records the same.
2. After ~24 h of slice traffic, read the would-refuses:

       get(kind='agentlog', id='/scope?since=24')
       get(kind='agentlog', id='/scope?since=24&role=builder&rule=tag-allowlist')

   The view groups by role/rule/verb/kind and lists the newest rows
   (enforced Denied rows share the one k=v `error_head`, so the readout
   parses both shapes identically); an empty window names the armed
   mode. All filters optional: `since`, `role`, `rule`, `verb`, `kind`,
   `page_size`.
3. Fill `SLICE_TAG_ALLOW` (`src/precis/principal.py`) with the tags the
   readout shows real builders reach for — a code change: ship and
   deploy it. Until then every builder `tag` would-refuses
   (`rule=tag-allowlist`), which is the expected bulk of the first
   window.
4. `settings set mcp.slice_scope enforce`. A refused write raises
   Denied listing what the role may write (`next=`: put it in the final
   message, the worker records it) and lands on `/scope` as a Denied
   row — the readout keeps working through the flip.

Kill path: `settings set mcp.slice_scope observe` (refusals stop, marks
continue) or `off` (the check is skipped). Either takes effect on the
next call — no restart.

## Slice grant read scopes (PRECIS_SLICE_READ_MCP)

The second, independent slice gate (gr574082): `mcp.slice_scope` bounds
writes by the *todo's* role; a minted `scope=` bounds the whole call
surface by the *grant's* scope — reads included. `slice_reader_scope.
refusal` is the allowlist, checked twice per call (boundary vs
principal layering): the raw request boundary
(`server._slice_request_refusal`, beside the personal gate — non-tool
requests refuse outright) and the dispatch backstop
(`tools.core._require_slice_grant_scope`), which sees the normalized
arguments. A scoped grant binds `slice:<grant>:<scope>` as its write
principal, so `principal.check` stays the third layer: a reader write
that reaches it is a defense-in-depth `scope-reader` refusal, and a
triage write is re-judged through the same boundary allowlist — the
verdict calls `slice_reader_scope.refusal` against the grant's
stamped refs plus the todo's live `fixes` links, and a refusal lands
as `rule=scope-triage`, so the effective refs bind at every layer.

- `reader`: `get`/`search` on kinds `python`, `md` and `skill`, plus
  `get` on the grant's `refs` (its todo and the gripes it fixes) —
  comments views only.
- `triage`: the reader surface plus gripe selection and the claim
  writes — `search`/`get` on any gripe, `put` comments on its todo and
  on gripes, `tag` `STATUS:`/`claimed-by:` on those, and
  `link(kind='todo', id=<own>, target='gr<id>', rel='fixes')`. The one
  claim is read-then-insert and races by design: the check reads the
  live links at the boundary while the insert lands in the link
  handler's own transaction, so two in-flight claims can both land —
  the raced second claim is visible in the links and the slice's own
  land clears only what it fixes.

Everything else refuses with a `next=`, and a scope stamp outside the
known set fails closed — a minted `scope=` can only name these.

The scope value a python get takes is the checkout alias, not a file:
`main::<qualname>` is the shared `/main` checkout (it can lag the
slice's branch). The python kind's `wt-<tree>` aliases come from the
checkout's linked-worktree registry, and an attempt clone is not a
linked worktree, so the slice's own clone is never indexed: the session
reads its files directly. Reads the brief tells the session:
`search(kind='python', mode='pattern', q='<name>')` first, then
`get(kind='python', id='main::<qualname>', view='source')`.

Arm per host: `PRECIS_SLICE_READ_MCP=triage` (or `reader`) in the
slice worker's service environment, beside `PRECIS_MCP_SLICE_TOKEN_DIR`
— the scoped grant files live in the same registry dir. Unset, empty
or any other value mints nothing: the launch is exactly today's
no-MCP one.
