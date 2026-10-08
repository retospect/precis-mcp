# Shipping — the four ship paths in detail

CLAUDE.md carries the one-line version of each path and the rules that bite;
this file is the mechanics. The skills (`/land`, `/go`, `/qland`, `/qgo`,
`/round`) carry the step-by-step.

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

## /go — local gate + deploy

Ship with the LOCAL suite + diff-coverage gate (changed src lines need tests;
the gate's default lane is `-m 'not slow'` — the slow cluster is covered by
check.yml's unfiltered 6 shards on every push, `--slow` /
`PRECIS_GATE_SLOW=1` restores the full set) + `scripts/deploy` of the
**gated sha** (`--pinned`, never bare — bare re-resolves `main` and can ship
an ungated sibling qland), plus a budgeted advisory mutation pass
(`scripts/mutate-diff`).

## /qland — pytest-ungated burst-land

`scripts/ship --quick`: commit WIP → sync → **drift guard** (warns when
main's last all-green shard matrix is 24h old, refuses at 48h — a burst that
outran its verdicts wants a `/go`, not another qland; never refuses on an age
it could not look up) → **pre-qland lint** (ruff · mypy · import contracts ·
DB-free hygiene tests (type-ignore ratchet, posix/encoding guards, doc
pointers, secret scan) — no full pytest, no gate slot, ~3 min;
`PRECIS_QLAND_LINT=0` to skip) → squash-merge. For when many trees are in
flight — qland them one by one, then one `/go` gates the integrated `main` +
deploys (ship skips the push when the tree already equals main).

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
never committed to (`deploy/README.md`).

## Rounds and the fleet

When a coordinator has a peer round open (`scripts/round status`), end your
land by marking it from your own tree — `scripts/round in <sha>`,
`scripts/round none`, or `scripts/round eta <text>` — instead of messaging;
leave deploys to the coordinator (`/round`: deploys the exact open release
head with a fresh green CI verdict (newest green main without a release) via
`scripts/round gate|deploy` — no local gate, no ship lock). The fleet itself
— one tmux window per active thread plus Reto's `review` window — comes up,
and recovers after a crash, with `/fleet` (`scripts/fleet up`); in a fleet
session a question for Reto is a review-queue item
(`.claude/fleet/review-protocol.md`), not a stop in the pane.
