# Shipping — the four ship paths in detail

Failed commands must show the original error, command and exit code (or timeout
duration), with URL credentials removed; best-effort failures still warn.
Use `scripts/lib/git_network.py::run_git` for network git calls: transient pushes
retry briefly after reconciling the remote ref, retaining the original lease.
On an unexplained push failure, check githubstatus.com first. A converged
release whose `prod` publication failed can use `scripts/round deploy
--finish-pointer` to recover from its receipt without another rollout; runtime
confirmation remains required.

CLAUDE.md carries the one-line version of each path and the rules that bite;
this file is the mechanics. The skills (`/land`, `/go`, `/qland`, `/qgo`,
`/round`) carry the step-by-step.

When a backlog item ships and its file is deleted, append a shipping comment
to each associated gripe naming the landed **main commit SHA**, instead of
using the deleted backlog path as the resolution pointer; the coordinator
adds this after landing if a task branch is handed off. Preserve earlier
comments as history. Audit stale citations with
`scripts/gripe-dangling-backlog --comments comments.jsonl` from the checkout
being checked: enumerate every page of session MCP
`search(kind='gripe', status='*', page_size=100, page=N)`, then read each
`get(kind='gripe', id=ID, view='comments')` and save its full text as a JSONL
record `{"id":"gr123","text":"<response text>"}`. The advisory report includes
closed gripes and historical citations even after a SHA follow-up; a subset
snapshot checks only that subset. Production reads use only those session
MCP verbs; the checker itself reads local files and never repoints comments.

**Name the gripes a commit fixes** in one of two places, and only there:
a parenthesised id list ending the subject (`fix(x): y (gr123)`) or a
`Fixes: gr123` trailer line in the body (`Fixes: gr1, gr2` also works).
Any other mention (`blocks gr…`, `related gr…`, `per gr… c6`) is context
and gets nothing. A commit that *touches* a gripe without finishing it
uses the partial form `refs grN` inside the same trailing list
(`fix(x): y (gr1, refs gr2)`, gr547015): land-notify posts a
`landed (partial): <sha>` comment with no `resolved:` and no STATUS
write, so nothing auto-closes. A gripe that must *never* auto-close
carries the `hold:no-autoclose` tag (a plain tag, applied with
`tag(kind='gripe', id=N, add=['hold:no-autoclose'])`) — even if a commit
names it plainly, gripe-validate skips it and posts a `shipped in r<N>`
note instead of `STATUS:done`.

After a landing, `scripts/ship` comments `landed <sha> on main` on each
gripe the landed commit fixes. On a deploy, `scripts/release` comments
`deployed r<N> (<sha>); runtime confirmation pending` on each gripe fixed by
a commit in the old-prod..release range, when `round deploy` exits 3
(rollout done, runtime pending): the release owner's path. Runtime
confirmation (`scripts/round deploy --confirm-runtime`) posts nothing.
These are comments, not closes; a gripe closes with a `resolved:` comment
after the deploy is verified.

Both go through `scripts/gripe-note`: one prod call (`scripts/prod-precis
gripes --note`), writing as the host's prod DSN role (`agent_rw`), with no
repeat of a comment the gripe already has. It is best-effort: it runs
after the push or rollout, its prod call stops at 25 s (release waits at
most 30 s), and an outage, refusal or timeout prints one `gripe-note:` line
and exits 0. It never fails the ship or the deploy. Off switch:
`PRECIS_GRIPE_NOTE=0`. A host without the prod DSN skips. `prod-precis`
refuses writes from a tree behind `origin/prod`, so a stale tree prints a
failed post and posts nothing.

The handles ending the squash subject also move status (gr541800/gr543584
ladder: triaged → fixing → auto_verify|user_verify → done). After the gripe-note comment,
`scripts/ship` runs `scripts/lib/land_notify.py`: each `grN` in the
trailing `(grN, tdM)` list gets `resolved: <sha> (main, release pending)`
+ `STATUS:auto_verify` (`PRECIS_VERIFY_KIND=user` → `STATUS:user_verify`;
each `refs grN` (the partial form, above) gets only a `landed (partial):
<sha>` comment — no `resolved:`, no STATUS write;
each `tdM` gets `landed: <sha>` + `STATUS:done`.
Same best-effort contract, one `land-notify` line, forwarded to the quiet
epilogue next to `SHIP_STATUS`; off switch
`PRECIS_LAND_NOTIFY=0`. A faulting prod call retries once at double the
timeout; every handle's calls are journaled to
`scratch/land-notify-pending/<sha9>-<handle>.json` before they run and
deleted once posted, so a failed (or killed) notify leaves the unsent
calls replayable — `scripts/ship --replay-notes` drains the dir
(gr543833). The land emits `SHIP_STATUS: shipped` before these notifies,
so a dead notify cannot take the status token with it; the notify call
itself is alarm-bounded (`PRECIS_LAND_NOTIFY_BUDGET`, 600 s). Ship owns
this close — after a land, check the
ref's state, not the log, and never post a second `landed:` comment or
`STATUS:done` by hand (gr542762). After each rollout `scripts/release` runs
`scripts/gripe-validate`, which flips every auto-verify gripe whose resolved
sha is an ancestor of `origin/prod` to `done` — except a gripe tagged
`hold:no-autoclose`, which stays and gets a `shipped in r<N>` note per
release instead — keeps one ask-user todo
for `user_verify` gripes, and closes `needs_spec` gripes a prod spec
names (`--dry-run` lists them;
it is safe to run on a schedule; off switch `PRECIS_GRIPE_VALIDATE=0`).

Write the commit message to a file and `git commit -F <file>` — never
`git commit -m "$(cat <<'EOF' …)"`. A commit wrapped in a heredoc or
`$(…)` substitution is a shape the worktree guard cannot statically
parse, so it dies at parse time with a bare syntax error that names no
fix (`scripts/hooks/guard-commit-on-main.py` refuses it earlier, naming
this fix — gr514943), and the recipe's quoting breaks on apostrophes,
backticks and `$` in the message anyway (gr513763, gr514743). The
scratch file goes under `scratch/` — e.g. `scratch/msg-1.txt` — the
named gitignored scratch dir, so the message file can never be
committed and never trips ship's dirty-tree refusal (gr515326,
gr515092, gr515823; `.claude/` is the other gitignored session-state
dir). A plain
literal `-m "subject"` plus a second `-m` for the body stays fine when
the text carries no shell-active characters.

`git commit` records the **index**, not the worktree: after a
`git reset --soft` — and any regenerate-then-commit flow — the index can
still hold stale pre-regen blobs, and the commit silently ships them
instead of the current files (gr515222). Re-stage after regenerating
(`git add -A`) and verify what actually landed with
`git show HEAD:<path>` before pushing on.

## Red main, and "did it land"

`check.yml` runs the slim lane (lint + the docs lane for docs-only diffs;
gr495187) on every push to main, so a lint-red main's failures usually
already sit on GitHub — a pytest red surfaces on the release gate or the
nightly instead. Run `scripts/main-ci-status` first (run id, failing
jobs, UNCLAIMED or not), then `scripts/ci-failed <id>` (the signal-line
digest of `gh run view <id> --log-failed`); read the
whole summary line ("1 failed, 2 errors" is three problems). A full local
gate validates, never diagnoses. Claim the fix in `.claude/purpose`, fix every
named cause in one branch, un-red with `--remote`; it skips the local ruff
autofix, so run `ruff check` and `ruff format --check` before pushing.

Squash-landing mints a new sha, so ancestry checks lie for the commits you
wrote: `git merge-base --is-ancestor <sha> origin/main` is permanently false
for them, and `rev-list main..branch` / 3-dot `git diff` over-report. The
branch NAME is different: ship resets it onto the squash commit before the
push, so a landed branch is an ancestor of main and plain
`git branch --merged main` lists it (dirty tree or lost CAS race included).
A branch not listed there holds unlanded work.
Feature landed? `git cat-file -e origin/main:<path the commit added>`.
Branch holds unmerged work? `git log --cherry-pick --right-only --no-merges
--oneline main...<branch>`; non-zero is a candidate (a multi-commit branch
squashed to one commit still shows), confirm against files on main. For a
superseded stale branch, `git diff --stat origin/main <branch> -- <files it
touched>` showing main newer on every file is proof. Bulk triage: one
worker per branch; merge the unsuperseded with `git merge -X ours`; the
branch's tree cannot be entered from another session.

`scripts/ship` squashes with `git commit-tree` plumbing and CAS-pushes
(`git push origin <new>:main`) because all worktrees share one `.git/index`;
a hand-rolled `merge --squash` from two sessions co-mingles staged files.
The squash commit is the branch's own: `reset --soft` onto it just before the
push, so the branch tip is the landed sha. While ship runs the tree is not
yours: on a clean tree it then `reset --hard`s the branch to shipped main,
destroying edits made meanwhile; on a dirty tree it leaves them and the
branch stays on the landed sha. The squash re-mints author and
trailers under the shipper's identity, so a builder commit's own author is
not gated — a Devin-authored commit carrying the required
Co-Authored-By/session trailer is accepted (Reto, gr514432).

## /land — gate on GitHub (harness only)

`scripts/ship --remote`: refuse uncommitted edits outside `.claude/`
(named; never committed or reset for you, gr489288) → sync main → push `ci/<branch>` → wait
for the check.yml gate (shape and duration: `docs/conventions/testing.md`
§CI shapes; run ship in background, output to a log) → atomic CAS
squash-merge to `main`. A full-gate ship lands an exactly-tested tree, or
says it did not and pins nothing.

Wait on any CI gate — a ship or release log, or a check.yml run id —
with `scripts/ship-wait <log|run-id>` in the background: one until-loop
that exits on green, red or cancelled, prints the verdict plus the first
failure line, and exits nonzero on red or cancelled. It is the only way
to wait on a gate (gr515152): no polling subagents, no log-tail loops in
the main loop.

Wait on a backgrounded `scripts/ship` run itself with `scripts/ship
--await [--timeout S]` from the same worktree: the run that owns the
per-worktree ship lock records its pid/log/verdict in a per-worktree
state file, so no log arg is needed — a sibling ship refused by that
lock never touches the record (gr541681, gr564499).
It exits with the run's own code after printing `SHIP_STATUS` and the
first `✖` block; `124` + the log path on timeout (default 1200 s), `2`
when no ship is recorded (gr541681).

Under the release train (Reto 2026-10-23) `--remote` is the HARNESS lane:
required when the diff touches the landing harness, refused otherwise
("`--remote` is for harness diffs … Re-run scripts/ship --quick").
Harness-lane membership has ONE canonical list — the `HARNESS_DIFF` case
in `scripts/ship` (`scripts/ship`, `scripts/test`, `scripts/lib/*`,
`.github/*`; NOT the whole `scripts/` dir — `scripts/release`,
`scripts/deploy` and friends are not harness). `uv.lock`,
`pyproject.toml`, migrations and schema are not harness — they qland like
everything else.

When the operator explicitly assigns gating/release to another toolchain,
`PRECIS_QLAND_EXTERNAL_GATE=1 scripts/ship --quick` acknowledges that ownership
for one invocation and admits a harness diff to qland. Normal quick-lane lint,
secret scan and fast guards still run. This does not run a remote gate, mint
a gate warrant or deploy; it does not claim an external gate has already
passed. Without that acknowledgement the default harness refusal remains.

The repo-wide ship lock is narrow: held only for the seconds of fetch →
squash → CAS push → local-main ff, never across sync, lint, a gate or the CI
wait. If main moves meanwhile, a tree that adds a migration or touches
`safe_fetch.py` re-syncs and re-runs CI once; any other tree, or that one
after its retry, lands by an in-lock forward merge of main instead, prints
"not a deploy warrant", writes no `.ship-sha` and moves no `gated` ref, and
the squash carries a `Gate: forward-merged over N commits` trailer
(`--quick` forward-merges the same way when main moved, with a
`Gate: lint only` trailer, or `Gate: none` under `PRECIS_QLAND_LINT=0`).
Squawk on new migration SQL stays host-side. `--remote --impacted` = opt-in
local impacted pre-gate first; bare `--impacted` = legacy local-only gate.

## Sync, and agent branches

Every ship path except `--release` syncs Git Town style: squash the branch to
one commit on its merge-base, then `git rebase origin/main`. A conflict stops
mid-rebase: resolve, `git add -A`, then `GIT_EDITOR=true git rebase
--continue`, re-run ship (ship refuses to start while a rebase is in
progress). The `GIT_EDITOR=true` accepts the existing message — a bare
`--continue` can spawn an editor and hang a non-tty shell (gr514728). In a fleet wave the
resolution itself belongs to a coder sub-agent or the builder's own tree —
never a desk or coordinator main loop (the `gripe-fleet` skill's standing
rules). The in-lock forward merge
above stays a merge; the squash and the branch reset absorb it.

A hand `git rebase origin/main` with uncommitted edits refuses outright
("You have unstaged changes"), and the obvious stash/rebase/pop round-trip
is worse than refused: the stash stack is shared across the primary
checkout and every worktree, so `guard-git-stash.py` denies a bare
`git stash` and any `git stash pop` — a pop can apply a *sibling*
session's entry (gr513770). The cheap path is **commit the slice first,
then rebase** — ship squashes the branch to one commit anyway, so a WIP
commit costs nothing. Two narrower options: `git rebase --autostash
origin/main` works only with a clean index — `guard-autostash-staged.py`
refuses it staged because the pop restores the staged changes unstaged
and the next `git commit` silently takes part of the diff (gr514868) — or
a tagged `git stash push -u -m "<tag>"` restored with `git stash apply`,
never `pop`.

Hand-run sync has three wrong-base traps:

- **Sync fetches are `git fetch origin`, never `git fetch origin
  main`.** The single-branch form fills `FETCH_HEAD` and updates
  `origin/main` only when `remote.origin.fetch` maps the branch (a
  stock clone's does); under a narrower refspec the tracking ref
  silently stays stale and the next `git rebase origin/main` works off
  the old tip. `git fetch origin` refreshes every tracking ref and is
  the habit (gr515809).
- **Part of the branch already landed squashed? Don't replay it.** A
  plain `git rebase origin/main` replays the landed commits too, and
  they conflict against their own squash. Replay only the unlanded
  tail: `git rebase --onto origin/main <sha of the last landed
  commit>`. When the branch's content all landed, skip the rebase and
  `git reset --hard origin/main` (gr515474).
- **A fresh tree taking over an existing branch resets, never
  rebases.** `git rebase <branch>` from a tree cut at `origin/main`
  replays every upstream commit the branch is missing — 46 commits and
  a conflict chain in the gr515502 case, silently wrong if no conflict
  had surfaced. `git reset --hard <branch>` puts the tree on the tip;
  a fresh tree has no commits of its own to replay.
- **A stack base that already merged is not a base.** Before proposing a
  fix lands on a branch — cherry-pick onto it, a PR against it, stacking
  a builder or agent on it — run `git merge-base --is-ancestor <branch>
  origin/main`; when the base is already in main, land on `main`
  instead, or the next release ships without the fix (gr518102).
  `scripts/rebase` (non-default base) and `scripts/take-agent` warn on
  this.

`scripts/ship` and `scripts/rebase` are quiet by default (gr497896): the
full transcript goes to a `/tmp` log whose path is printed, and the terminal
sees only the ✖ lines, a conflict's file list, the landed sha and any
`land-notify` line, and the final
`SHIP_STATUS:`/`REBASE_STATUS:` token. `--verbose` (or
`PRECIS_SHIP_VERBOSE=1`) restores the old full stream. `scripts/branch-audit
[BRANCH...]` prints each branch's landed/unlanded/partial verdict; the same
`scripts/lib/branch_audit.py` test drives `fleet-wave close`'s refusal.

An editing subagent's work enters your branch only by fast-forward:
`scripts/take-agent <agent-tree>` rebases the agent branch onto yours and
`git merge --ff-only`s it, and records it in this worktree's git dir
(`taken-agents`). After the land, ship removes every recorded worktree and
branch (keeping a dirty, moved or live-locked one recorded for the next
ship). Never copy an agent's diff by hand. A copied diff leaves the agent branch looking
unmerged forever, which is how 28 stale agent trees piled up on 2026-10-11.
Work you won't use: `scripts/take-agent --discard <agent-tree>`.

## /go — land, cut a release, gate it, deploy it

`scripts/ship --quick` lands the tree on `main` (lint + fast DB-free guards), then
`scripts/release` drives `scripts/round`: open a round if none is open,
`round cut --sha S` pushes `release/r<N>` at S = `origin/main`, check.yml
runs the full gate (lint + the shard matrix, never cancelled) on that exact head,
and `round deploy` deploys it `--pinned` and moves `gated`/`prod`. Exit 3
leaves runtime proof to the session: verify boot shas and the session MCP,
then `round deploy --confirm-runtime S --runtime-evidence …` tags
`deployed/r<N>`, deletes the branch, and `round close` ends the round.

When the factory is armed, `release_poll` ticks every 10 minutes and mints a `release-pass` plan only when `origin/main` is ahead of `origin/prod`, no pass, round or release branch is already in flight, and the clock sits outside `release.no_release_windows`. The minted plan runs the same cut → gate → approval → deploy → confirm shape as `/go`: `release_cut` cuts, `ci_verdict_poll` polls check.yml, and a red verdict runs up to 2 automatic `release-fix` loops (`ci_red_digest` digests the failure, a fix lands, `release_cut` recuts; `plan_dispatch._RELEASE_FIX_AUTO_CAP` caps this at 2 — a third red is a human call; a test red of 1-2 low-risk ids is instead waived under `release.red_waiver` — deploy anyway, file a top-priority `broken-build` gripe, auto-revert if it is still open after the next release). A green verdict reaches the `approval` ask, which `release.auto_deploy` (default off, DB-only) auto-answers `deploy r<N>` once CI is green on the cut's exact sha and `release.round` names an open round on it; otherwise a human answers. `release_deploy` then deploys pinned, and `release_confirm` attests the runtime — mechanical evidence, no rollback. The same pass also files a stuck-release ruling ask when a stage runs longer than `release.stage_deadlines` allows; answering it executes nothing. Detail: `docs/runbooks/dark-factory-arming.md` §Release pass, §Standing approval, §Stuck watchdog; the stage table is `docs/runbooks/release-cycle.md` §Factory loop.

Main pushes after the cut never reach the release. One *open* release at a
time, but the train pipelines (release-train §5): once release N is deploying,
`round cut` may cut r<N+1> at `origin/main` (which must contain N's head with
a differing tree). A `/go` whose sha an in-flight release contains exits 0
(rides along); any other exits 0 with "rides the next cut" — the driver cuts
N+1 at deploy start, there is no queue. Release CI has no diff-coverage gate or mutation pass yet
(`docs/backlog/release-ci-diff-coverage.md`); the local full gate with
both is still `scripts/ship --mutate --full` + `scripts/deploy <.ship-sha>
--pinned`, now off the `/go` path. Exit-code table: `.claude/commands/go.md`
step 7.

**Don't wait (Reto 2026-10-16).** When the release owner is ready to cut,
it cuts. No release waits for a red fix, a straggler's `eta`, a planning
pass or a ruling still in discussion. Cut at the newest all-green main sha;
if main is red on a commit that is not ready, revert that commit (it
relands later) or cut at the last green sha, whichever is faster. Never
weaken a test or ratchet to make the cut. Work that misses the cut rides
the next release.

## /qland — pytest-ungated burst-land (the default lane)

`--quick`: ruff + secret scan + the fast DB-free guards (skill vocabulary,
render, type-ignore ratchet, next-literal ratchet, ruff scripts-excludes sync,
scripts-use-uv shebangs) + mypy on the changed `src/`/`tests/` python
files (`scripts/lib/mypy_changed.py`: only errors in those files fail, so a
red in an imported module never blocks an unrelated land; gr579772;
`PRECIS_SHIP_QUICK_MYPY=0` skips it); full mypy, import contracts and all
pytest run at the release gate (Reto 2026-10-30: "Don't even bother with python checks,
we'll do it all as a batch in the release branch"). The secret scan
(`tests/test_deploy_tree_no_secrets.py`) stays because the repo is public and
a qland push publishes at once; the guards joined after r229-r231 each went
red on a guard `--quick` had skipped (gr573555) — ~22 s without the test
stack. `--checked` and `--release` keep mypy, import contracts and the
hygiene tests in their lint.

`scripts/ship --quick`: refuse a dirty tree → sync → **drift guard** (warns when
main's last all-green shard matrix is 24h old, refuses at 48h — a burst that
outran its verdicts wants a `/go`, not another qland; never refuses on an age
it could not look up) → **pre-qland lint** (ruff · secret scan · fast
DB-free guards · mypy on the changed files — no full mypy, import contracts,
rest of the hygiene batch or pytest, no gate slot;
`PRECIS_QLAND_LINT=0` to skip) → squash-merge. For when many trees are in
flight — qland them one by one, then one `/go` releases the integrated
`main` (ship lands nothing when the tree already equals main).

Under the release train `--quick` is the default lane for every non-harness
diff; a harness diff (the `HARNESS_DIFF` classifier in `scripts/ship` —
the canonical list, see /land above) is refused before any gate and takes
`/land`.
The same refusal covers `--checked`.

## --checked — opt-in: qland plus a selected pytest

Not the default: builders and sessions land with plain `--quick` as each tree
finishes, with no batch plan and no batch-size counting (Reto 2026-10-30,
reaffirmed 2026-10-31 and 2026-11-05). Pytest runs once, in the release
branch gate; a red release is fixed forward.

`scripts/ship --checked` is `--quick`'s merge machinery plus one more gate
leg: after the pre-qland lint it runs the selection from
`scripts/impacted-tests` — the one diff→tests selector (see "Impacted-test
gates" below), shared with `scripts/test --selected`. The CONTRACT set of
fast repo-invariant tests (audited
mode/kwarg tables, closed registries, ratchets, vocab and secret scans)
runs unconditionally, plus PATH_MAP/corpus-coupled tests for non-Python
inputs (skills `.md`, `.hx` examples, reviewed-blob pins, fixtures,
deploy/docs corpus) and, for Python changes, the python index's
call/import evidence plus measured coverage when a fresh per-test DB is
around; testmon joins in when the worktree has a `.testmondata` map. The
pytest leg takes a real fleet gate slot; the lint leg still doesn't. A
harness/schema diff has no honest subset — the selector answers FULL and
`--checked` refuses (harness takes `--remote`; schema takes `--quick`,
`--full` or `--impacted`).
It lands like a qland: no `.ship-sha`, no diff-coverage, forward-merge
trailer `Gate: lint + selected pytest` when it races main.

Factory lands run basic checks only (gr599427, Reto 2026-11-08):
`land_slice.ship_flag` picks `--checked` only when the selector's answer
is a small spot selection (`pytest --collect-only` over the selected
targets reports ≤ `SPOT_SELECTION_MAX_IDS` = 200 items — items, not
target lines — with the selector and the count together answered inside
`SPOT_SELECTION_TIMEOUT_S` = 3 min; `testmon=1` is unbounded and goes
`--quick`), else `--quick` — engine
diffs follow the same rule, and a FULL-selector refusal relaunches
`--quick`, never `--full`. The full suite is the release gate's job —
GitHub CI on `release/r<N>`; a red release goes through the existing
fix loop. The td574937 E1 "an engine land never drops to lint" clause is
superseded: the engine review reads the landed diff, not a checked gate.

It exists because every r35 red was a lint-only land whose failure a cheap
invariant/snapshot/table test would have caught (gr485791). Use it when a
diff touches something those tests guard; the suite debt is still settled by
the release gate. Run the same selection without landing
via `scripts/test --selected` (or `scripts/impacted-tests` bare). Refused
with `--remote`, `--impacted`,
`--full`, `--mutate`, `--release` (also via `PRECIS_SHIP_CHECKED=1`).

## Impacted-test gates

`scripts/impacted-tests` is the one diff→tests selector: `BASE [HEAD]`
prints `file<TAB>reason` (or `FULL`); with no BASE it speaks the checked
wire protocol (`testmon=N` then bare targets) for `ship --checked` /
`test --selected`. Evidence, strongest first:

- **measured** — the per-test-context coverage DB (`.coverage` +
  `.coverage.precis.json`, produced by `scripts/test --coverage` and the
  nightly `coverage-db` job; `--fetch-db` downloads the latest artifact to
  `$PRECIS_COVERAGE_DB_DIR`, default `~/.cache/precis/coverage-db`). Used
  only when fresh (< 48h, not older than the base) and a file's manifest
  hash matches its BASE blob — coverage line numbers are base-side.
  The slice lander's deploy account has no GitHub credential: `scripts/deploy`
  runs `--fetch-db` on the controller (best-effort, warns on failure) and
  the 20g play copies the map to the lander's `PRECIS_COVERAGE_DB_DIR`
  (`precis_coverage_db_dir`), set in the 20b/20g worker unit env that
  `land_slice` → `scripts/ship` inherits. No local map: copy skipped.
- **static** — the python index's call graph and imports: `discover_tests`
  per changed symbol, test modules importing the changed module, test
  files named `test_<stem>`, test sources naming the changed path or
  basename (prompts, skills, templates, fixtures, scripts), and test
  sources naming a changed spec/tuple attribute — a spec-pin test
  (`XHandler.spec.edit_modes == (...)`) has no import edge and no name
  match (gr599427). The two
  weakest navigation tiers (test-name substrings, fixture-parameter
  guesses) are deliberately excluded — they match hundreds of tests on
  names like `main`.
- **contract** — the CONTRACT set of fast repo-invariant tests runs on
  every non-empty diff (audited tables, closed registries, ratchets,
  doc/secret/vocab scans).
- **corpus** — PATH_MAP glob couplings (handler vocab → parity tables,
  reviewed-blob pins, migrations → guard tests, skill corpus, `.hx`), plus
  resolved corpus refs: a change under a path a test reads selects it.
- **always** — a changed test file is itself; `*/migrations/*.sql` selects
  the migration/schema battery; docs-only diffs get the doc-hygiene set; a
  non-doc file with zero evidence adds the smoke set (`--smoke`: dispatch,
  kwarg parity, CLI, empty-DB migrate, startup schema gate, skill corpus,
  web import).
- **full** — `pyproject.toml`, `uv.lock`, `conftest.py`, `scripts/test`,
  `scripts/impacted-tests`, `docker/`, `.github/workflows/` changes answer
  `FULL`: no subset is honest when the test machinery (or the selector
  itself) moves. This set is wider than the `HARNESS_DIFF` harness-lane
  list — `pyproject.toml`, `uv.lock` and `docker/` are not harness-lane
  paths; they just have no honest subset.

When unsure it widens; it never narrows below the always-include floor.

Where it gates today: **`scripts/ship --checked`** (selection over
`merge-base origin/main..HEAD`; FULL refuses) and **`scripts/test
--selected`**. Release gating on the impacted set is **deferred** —
releases keep the full suite until the selector has a track record
(Reto, 2026-10-15). The `BASE HEAD` mode and the nightly coverage DB are
kept so a later slice can flip the release lane on: a `test-release` job
selecting over `deployed/r<N-1>` was designed and measured (~15–30 min
static-only on release diffs, tighter once the measured tier feeds it),
and the soak-vs-verdict wiring is on `docs/backlog/threads/ship-gate-ci.md`.

**Post-deploy: refresh the session MCP image.** The session MCP runs from
the operator-side `precis-mcp:dev` image, which bakes autocatpath at build
time; a deploy that moves the `uv.lock` autocatpath pin does not update it
(gr493148). The release owner runs `scripts/mcp-image-refresh` after such a
deploy (`--dry-run` first: plan, both versions, installed-script diff, no
changes). It compares the image's `precis.autocatpath_rev` and
`precis.uv_lock_sha256` labels with the served lock — a dependency bump
that leaves the autocatpath pin alone still shows stale — names which
part differs, and compares the installed ensure script with the repo
copy; "up to date" when everything matches. `--force` rebuilds and
recreates regardless of the labels. Otherwise (before building anything) it decides on the
installed script: byte-identical to a past revision of
`deploy/mcp-http/precis-mcp-http-ensure.sh` in git history means no local
edits, so it auto-applies (`scripts/mcp-http-install --apply`, which backs up
the old copy). A script matching **no** revision (local edits, or missing) is
the one human stop (as is a dirty repo copy or unreadable git history): exit 3,
nothing built or changed, diff content never printed (hashes and line counts
only). The installed file is re-checked just before the apply. After a stale-image
`scripts/build-image precis-dev` and the apply, it runs `--recreate`
(blue-green) and waits up to `--timeout` (180s) for the new colour to answer
401 on both probes, else exit 4. `--dry-run` prints the plan and the
decision (auto-apply or refuse) and changes nothing. Not wired into
`scripts/release` or `scripts/deploy` (the repo copy compared is the
checkout's HEAD, so the release owner runs it from the deployed sha). The
prepare step also logs `autocatpath SKEW` at child start when the baked
commit is not the pin.

## /qgo — ungated deploy

qland + deploy that sha **ungated**, then a repair gate only if a slot is
free (never queued — queuing starves the gate-slot semaphore). Prod may run
broken code until the next pass: accepted on a dev cluster, and
`scripts/qgo-guard` hard-refuses the two things prod cannot take back (any
`*/migrations/*.sql`, `safe_fetch.py`) — those take `/go`.

## Failure and refs

All paths abort+report on failure and are idempotent — fix and re-run.
Merge target is `main` (no `master`). Red gate: the failure is printed above
the `✖` — read *that*, never `scripts/ship` (remote-gate red: ship prints the
failing jobs + `scripts/ci-failed <id>`). A dead package-index route
is NOT a red: before the gate command, ship syncs the gate's project env
offline-first (`uv sync --offline`, falling back online only on a uv cache
miss — the cache lives on the per-worktree `gate-cache` volume shared by
both gate services), and any fetch/transport failure exits 68 with
"NETWORK failure" naming the unreachable host instead of a lint/test verdict
(gr496615). Where a commit has got to is
three refs: `main` (landed), `origin/gated` (last full-gate green),
`origin/prod` (what the cluster runs) — script-moved, fast-forward only,
never committed to (`deploy/README.md`). While a release is open,
`release/r<N>` is the frozen sha being gated/deployed; `deployed/r<N>` tags
each one that went live. `good/r<N>` (gr517170) marks a deployed release
that ran ~24h clean — `scripts/round good` pushes it from the coordinator's
sweep, `round good --resolve` prints the newest, `round status` shows it.
Rollbacks target the newest `good/r<N>`, not a guessed `deployed/r<N-1>`.

The shared session MCP serves a plain clone kept on `origin/prod`, moved by
the same deploy step (`env_prod_checkout_sync` in
`scripts/lib/env-pointers.sh`). Its path is derived as `<dirname of the git
common dir>-prod` — the sibling `-prod` of the checkout's repo — which only
matches a machine that deploys from the repo the server was installed
against. A deploy run from a second checkout (a fleet worktree of another
clone, another machine) derives a path that may not exist; the sync then
warns instead of staying silent, and the served clone goes stale
(gr482226). `PRECIS_PROD_CHECKOUT=<clone path>` overrides the derivation;
`deploy/mcp-http/`'s ensure script also re-follows `origin/prod` at
SessionStart. When a clone does resolve but still is not at the deployed
sha after the sync attempt, `scripts/deploy`'s confirmation
(`_confirm_prod_clone`) fails the run naming both shas.

## Object-store tidy — scripts/git-tidy

Every squash-land leaves the branch's commits unreachable, the worktree
reaper deletes the branches, and nothing expires them: git's defaults
keep unreachable reflog entries 30 days and pruneExpire 2 weeks, so the
shared `.git` accumulates tens of thousands of dangling objects
(gr548630: 16k unreachable commits / 261k trees on 2026-11-01).
`scripts/git-tidy` is the recipe — an operator runs it by hand from the
main checkout; it is deliberately never wired to a hook, cadence or
worker (Reto 2026-11-01).

`scripts/git-tidy` with no args is a dry run: it prints `git fsck
--unreachable --no-reflogs` counts, every stash entry with age, branch
and sha (stashes hold their objects reachable and hide WIP nobody owns —
the script lists them, dropping by sha stays manual), and the commands
`--apply` would run.

`scripts/git-tidy --apply`:

1. Refuses while any ship lock is held — the repo-wide squash lock
   (`precis-ship.lock.d` under `--git-common-dir`) or any worktree's
   `precis-ship-worktree.lock.d`, judged by lock-holder.sh's live-holder
   rule so a stale lock warns and proceeds. A gc racing a ship could reap
   objects the in-flight squash still needs.
2. Writes the durable repo-local config (`--local`, idempotent):
   `gc.reflogExpireUnreachable=7.days`, `gc.pruneExpire=3.days`,
   `gc.auto=1000` — after the one-off bankruptcy pass, plain `git gc
   --auto` self-cleans instead of regrowing the pile.
3. Runs `git reflog expire --expire-unreachable=1.day.ago --all`, then
   `git gc --prune=1.day.ago`, and prints the fsck counts before/after.

Never `--prune=now`: ~50 live sessions share the object store, and a
session that just wrote objects for an in-flight ship or commit would
lose them. The 1-day window is the safety margin.

## Rounds and the fleet

Routine fleet status — landed, red, waiting, done — goes on the board,
never in a message: `scripts/board post <window> <one line>` appends a
stamped line to the shared gitignored `.claude/fleet/board.log` in the
main checkout; the coordinator reads it each sweep with
`scripts/board read [--since <ts>]`. SendMessage is only for things that
need action now — and a harness without it (Devin) reaches the
coordinator over tmux instead: `scripts/fleet panes say
<coordinator-window> '<text>'` (`scripts/fleet panes status` lists the
windows) or `scripts/fleet say -m '<text>' <window>` (gr514682).
`scripts/board` is a repo script, so a tree stacked on
an old base can predate it — run the main checkout's copy by absolute
path, or rebase the tree first (gr515501). Post lines over 120 chars are
refused: keep the line short, put detail in a handoff or gripe.

Hand releases are that same `release_poll` todo paused — never both armed. Origin (branch heads, `deployed/r<N>` tags, the `release.round` row) is the truth for release numbering and in-flight state; a clone's `round.json` is only that host's own journal of its own verb calls.

When a coordinator has a peer round open (`scripts/round status`), end your
land by marking it from your own tree — `scripts/round in <sha>`,
`scripts/round none`, or `scripts/round eta <text>` — instead of messaging.
Outside a fleet, `/go` is its own coordinator for one release (above); in a
fleet session leave deploys to the coordinator (`/round`: deploys the exact open release
head with a fresh green CI verdict (newest green main without a release) via
`scripts/round gate|deploy` — no local gate, no ship lock). The fleet itself
— one tmux window per active thread plus Reto's `review` window — comes up,
and recovers after a crash, with `/fleet` (`scripts/fleet up`); in a fleet
session a question for Reto is an `ask-user` todo with `meta.ask`
(`search(kind='todo', view='asks')`), not a stop in the pane.

**A branch lands only by ship run in its own tree** (Reto 2026-10-30).
Subagent work enters through `scripts/take-agent`; a tier-R fleet builder's
work lands with `scripts/fleet-wave land <tree>`, which runs that tree's own
`scripts/ship`. Copying a commit onto another branch (cherry-pick, a `*-pin`
branch) lands the copy and leaves the original looking unmerged forever —
the 147 `*-pin` branches this rule retires.

**A lander removes its own landing tree** (Reto 2026-11-02). When a ship
from a scratch tree it made (`qland-<x>` tree, `land-<x>` branch) reports
shipped, the same session removes that tree and deletes that branch — no
further go needed. Never someone else's tree.

Two fleet-coordination rules (Reto 2026-09-30):

- **The no-deploy window.** Deploys wait out the 03:00–04:20Z nightly
  backup window; when a peer asks when the deploy runs, the answer is
  "after 04:20Z", never a time inside it. `scripts/deploy` already
  enforces the migration side mechanically (`container-ops.md` §Ship vs
  deploy; runbook `migration-deploy-window.md`); the ruling covers the
  deploy itself, code-only included.
- **Peer-ping cadence.** Ping only pending peers — every ping is a billed
  turn — no "gate starting" broadcasts, and drop a peer silent for 3+
  pings. A peer's "landed `<sha>` remote-green, carry it" is the /go
  trigger.

## Long form — ship workflow and commit messages (moved from CLAUDE.md, 2026-11-01)

The resident brief (`CLAUDE.md`) keeps one line per rule; the detail that
used to sit there lives here (context-memory-hierarchy S4). Nothing below
is new.

Work always happens on a task branch in a separate worktree (`claude -w
<name>`). Never edit or commit in the primary `main` checkout; integrate
branches in a dedicated integration worktree.

- **`/land`** — `scripts/ship --remote`: GitHub's check.yml gate, then CAS
  squash-merge to `main`. Harness only under the release train — the
  canonical list is the `HARNESS_DIFF` case in `scripts/ship`
  (`scripts/ship`, `scripts/test`, `scripts/lib/*`, `.github/*`); a
  non-harness diff is refused, name `--quick`. Run ship in background,
  output to a log.
- **`/go`** — `scripts/ship --quick` to `main`, then `scripts/release`: cut
  `release/r<N>` from main at a fixed sha, gate it on check.yml, deploy that
  head pinned. Later main pushes don't touch it; one release at a time, a
  second `/go` rides along or waits.
- **`/qland`** — `scripts/ship --quick`: lint + fast DB-free guards, no pytest. The
  release-train default lane for every non-harness diff — `uv.lock`,
  `pyproject.toml`, migrations and schema included; a harness diff is
  refused, name `--remote`. Burst-land many trees, then one `/go`.
  Land each tree with plain `--quick` as it finishes; no batching plan.
  `--checked` is an opt-in: the same machinery plus a cheap selected pytest
  (`scripts/impacted-tests`: contract invariants + corpus/import map + index
  evidence, testmon when a map exists; gr485791); preview the selection with
  `scripts/test --selected`.
- **`/qgo`** — qland + ungated deploy. Never for `*/migrations/*.sql` or
  `safe_fetch.py` (`scripts/qgo-guard` refuses; those take `/go`).

All are idempotent — fix and re-run. Red gate: read the failure above the
`✖`, not `scripts/ship`. Refs: `main` (landed), `origin/gated` (last
full-gate green), `origin/prod` (what runs), `release/r<N>` (the open
release) — script-moved, never committed to.

Many sibling sessions run at once: the SessionStart hook prints only a
counts line (trees, dirty, live sessions — gr499042, ~3.6k tokens saved per
session), so before starting work and before touching shared files run
`scripts/inflight` (or `--json`) to see sibling trees, purpose lines and
overlap; once your task is clear, write one line to `.claude/purpose`. When
a coordinator has a round open (`scripts/round status`), mark it from your
tree (`scripts/round in <sha>|none|eta <text>`) instead of messaging. In a
fleet session leave deploys (and `/go`) to the coordinator. Fleet: `/fleet`;
in a fleet session a question for Reto is an `ask-user` todo with
`meta.ask`, never a file (`search(kind='todo', view='asks')`; skill
`precis-work-help`). Merged+clean+sessionless worktrees auto-reap. Work that
belongs to a thread (`docs/backlog/threads/<slug>.md`) updates that file in
the same commit — delete what shipped, insert what you filed at its rank
(README there).

Never pipe `scripts/ship|deploy|bump` into `tail`/`grep`/`tee` — a pipeline
reports the *filter's* status, so a red gate reads as exit 0. Redirect to a
log, or `set -o pipefail`.

**Commit messages: `type(scope): what changed`** (aim ≤72 chars, hard limit 120;
feat/fix/docs/test/chore/refactor…; `docs(backlog)` for specs), optional
short why as body, then the required Co-Authored-By/session footer. Write
the message to a file and `git commit -F <file>` — the `-m "$(cat <<'EOF'
…)"` recipe trips the shell rules and breaks on apostrophes (gr513763).
`scripts/ship` reuses your branch's single commit message or takes `-m`; it
refuses `ship(...)`/`wip(...)` placeholders and unprefixed subjects. A
multi-line message or one carrying `'`/`` ` ``/`$` goes via `git commit -F
scratch/commit-msg.txt`, never a quoted `-m` (→
`docs/conventions/container-ops.md`). Non-Claude builders (Devin) use the
literal footer `Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>` then
`Claude-Session: none (Devin builder, <fleet name>)`.
