# Shipping — the four ship paths in detail

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

## /land — gate on GitHub

`scripts/ship --remote`: commit WIP → sync main → push `ci/<branch>` → wait
for the check.yml gate (shape and duration: `docs/conventions/testing.md`
§CI shapes; run ship in background, output to a log) → atomic CAS
squash-merge to `main`. A full-gate ship lands an exactly-tested tree, or
says it did not and pins nothing.

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
mid-rebase: resolve, `git add -A && git rebase --continue`, re-run ship (ship
refuses to start while a rebase is in progress). The in-lock forward merge
above stays a merge; the squash and the branch reset absorb it.

An editing subagent's work enters your branch only by fast-forward:
`scripts/take-agent <agent-tree>` rebases the agent branch onto yours and
`git merge --ff-only`s it, and records it in this worktree's git dir
(`taken-agents`). After the land, ship removes every recorded worktree and
branch (keeping a dirty, moved or live-locked one recorded for the next
ship). Never copy an agent's diff by hand. A copied diff leaves the agent branch looking
unmerged forever, which is how 28 stale agent trees piled up on 2026-10-11.
Work you won't use: `scripts/take-agent --discard <agent-tree>`.

## /go — land, cut a release, gate it, deploy it

`scripts/ship --quick` lands the tree on `main` (lint only), then
`scripts/release` drives `scripts/round`: open a round if none is open,
`round cut --sha S` pushes `release/r<N>` at S = `origin/main`, check.yml
runs the full gate (lint + 6 shards, never cancelled) on that exact head,
and `round deploy` deploys it `--pinned` and moves `gated`/`prod`. Exit 3
leaves runtime proof to the session: verify boot shas and the session MCP,
then `round deploy --confirm-runtime S --runtime-evidence …` tags
`deployed/r<N>`, deletes the branch, and `round close` ends the round.

Main pushes after the cut never reach the release. One release at a time:
a `/go` whose sha the in-flight release contains exits 0 (rides along); any
other waits for it to retire, then cuts the next (exit 4 if the wait times
out). Release CI has no diff-coverage gate or mutation pass yet
(`docs/backlog/release-ci-diff-coverage.md`); the local full gate with
both is still `scripts/ship --mutate --full` + `scripts/deploy <.ship-sha>
--pinned`, now off the `/go` path. Exit-code table: `.claude/commands/go.md`
step 7.

## /qland — pytest-ungated burst-land

`scripts/ship --quick`: commit WIP → sync → **drift guard** (warns when
main's last all-green shard matrix is 24h old, refuses at 48h — a burst that
outran its verdicts wants a `/go`, not another qland; never refuses on an age
it could not look up) → **pre-qland lint** (ruff · mypy · import contracts ·
DB-free hygiene tests (type-ignore ratchet, posix/encoding guards, doc
pointers, secret scan) — no full pytest, no gate slot, ~3 min;
`PRECIS_QLAND_LINT=0` to skip) → squash-merge. For when many trees are in
flight — qland them one by one, then one `/go` releases the integrated
`main` (ship lands nothing when the tree already equals main).

## --checked — the qland burst lane with a selected pytest

`scripts/ship --checked` is `--quick`'s merge machinery plus one more gate
leg: after the pre-qland lint it runs the selection from
`scripts/select-tests` — the CONTRACT set of fast repo-invariant tests
(audited mode/kwarg tables, closed registries, ratchets, vocab and secret
scans) unconditionally, plus corpus-coupled tests for non-Python inputs
(skills `.md`, `.hx` examples, reviewed-blob pins, fixtures, deploy/docs
corpus) and, for Python changes, testmon when the worktree has a
`.testmondata` map or a static direct-importer scan when it doesn't. The
pytest leg takes a real fleet gate slot; the lint leg still doesn't. It
lands like a qland: no `.ship-sha`, no diff-coverage, forward-merge trailer
`Gate: lint + selected pytest` when it races main.

It exists because every r35 red was a lint-only land whose failure a cheap
invariant/snapshot/table test would have caught (gr485791). For the burst
lane it is the recommended default over bare `--quick`; the suite debt is
still settled by the next full gate. Run the same selection without landing
via `scripts/test --selected`. Refused with `--remote`, `--impacted`,
`--full`, `--mutate`, `--release` (also via `PRECIS_SHIP_CHECKED=1`).

## /qgo — ungated deploy

qland + deploy that sha **ungated**, then a repair gate only if a slot is
free (never queued — queuing starves the 2-slot semaphore). Prod may run
broken code until the next pass: accepted on a dev cluster, and
`scripts/qgo-guard` hard-refuses the two things prod cannot take back (any
`*/migrations/*.sql`, `safe_fetch.py`) — those take `/go`.

## Failure and refs

All paths abort+report on failure and are idempotent — fix and re-run.
Merge target is `main` (no `master`). Red gate: the failure is printed above
the `✖` — read *that*, never `scripts/ship` (remote-gate red: ship prints the
failing jobs + `gh run view <id> --log-failed`). Where a commit has got to is
three refs: `main` (landed), `origin/gated` (last full-gate green),
`origin/prod` (what the cluster runs) — script-moved, fast-forward only,
never committed to (`deploy/README.md`). While a release is open,
`release/r<N>` is the frozen sha being gated/deployed; `deployed/r<N>` tags
each one that went live.

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

## Rounds and the fleet

When a coordinator has a peer round open (`scripts/round status`), end your
land by marking it from your own tree — `scripts/round in <sha>`,
`scripts/round none`, or `scripts/round eta <text>` — instead of messaging.
Outside a fleet, `/go` is its own coordinator for one release (above); in a
fleet session leave deploys to the coordinator (`/round`: deploys the exact open release
head with a fresh green CI verdict (newest green main without a release) via
`scripts/round gate|deploy` — no local gate, no ship lock). The fleet itself
— one tmux window per active thread plus Reto's `review` window — comes up,
and recovers after a crash, with `/fleet` (`scripts/fleet up`); in a fleet
session a question for Reto is a review-queue item
(`.claude/fleet/review-protocol.md`), not a stop in the pane.
