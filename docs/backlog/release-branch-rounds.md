---
status: draft
pillar: platform
title: each round cuts release/r<N> from a green main sha, fixes land on it and merge forward into main, the deploy tags deployed/r<N> and merges the branch back as a real merge
---

# Release branch per round

## What

Reto, 2026-10-03 (review-queue `ship-gate-ci-1`): "we cut a release branch
and everyone is out of our hair while we prep the push … land stuff in that
branch without dealing with any late arrivals, and when we deploy, we can tag
deployed, merge the fixes back to main, and continue."

Today a round deploys the newest main sha with a green CI verdict
(`scripts/round gate`/`deploy`). Main keeps moving during the round, so a
late arrival can redden every later candidate. On 2026-10-03 one unguarded
import made every main run red for an hour. A fix for the round lands on
main behind whatever else arrived meanwhile. A release branch freezes what
the round ships: only fixes the round asks for get in.

## Design

The design note is `reviews/ship-gate-ci.md` § "Design note 3" (state dir,
outside the repo). Summary:

1. `scripts/round cut [--sha S]` creates `release/r<N>` at the newest green
   main sha and refuses while another release branch is open.
2. `check.yml` runs the full gate shape (lint + 6 shards) on every
   `release/**` push, in its own per-sha group, never cancelled.
3. `scripts/ship --release` applies the tree's squashed change onto the
   release head, pushes it with a CAS, and **merges the release forward into
   main at once** as a real `--no-ff` merge, so other sessions stop hitting
   the bug during the release window. A conflict stops and reports.
4. `round gate` reads only the exact release head's check.yml run/attempt:
   lint + six distinct 3.13 shards all successful (optional complete 3.12
   matrix), with completion age bound to that same certificate.
5. `round deploy` deploys the head pinned, moves `gated`/`prod`
   fast-forward, awaits coordinator runtime evidence, then tags `deployed/r<N>`,
   merges the branch back into main as a real merge (a no-op once every fix was
   forwarded) and deletes the branch. Main full gates defer global gated
   publication while the release owns it; local pins and main lands remain.

Main's CI stays the shared never-cancel-running group (d2c469007), so the
qland drift guard keeps reading main.

## Cost

CI jobs per round (~15 qlands, ~2 h): about 24 on main plus 8 at the cut
and 8 per release fix, so 40–48 jobs. Before the stopgap it was 120.

## Acceptance criteria

- One round runs end to end on a release: cut, one release fix forwarded
  to main, deploy, `deployed/r<N>` tag, merge-back, branch deleted.
  Afterwards `scripts/fleet refs` shows `prod` at the tag.
- A late qland on main during the release does not change the deployed sha.
- Conflict paths (apply onto the release, forward merge into main, a lost
  CAS on the release ref) each stop with both refs consistent and say what
  to do. All are tested on throwaway repos with a bare origin.

## Decisions (orchestrator verdict on design note 3, 2026-10-03 15:48Z)

- Build in slices (a) → (b) → (c). Slice (b) gets its own design note
  before it lands.
- (a) `round cut` also checks that every sha a peer marked `in` for the
  round is an ancestor of S, and prints the rest as `late: <peer> <sha>`
  before the announcement. It runs the migration-collision scan in the same
  step. Optional: `plan` skips the matrix on a `release/**` push whose exact
  sha already has a completed check.yml run (the cut sha carries main's
  verdict).
- Release fixes go `--quick`: the release push's own full run is the gate.
  They are made only on the coordinator's request, by the owning thread from
  its own tree, announced with `fleet say`. `round cut --abandon` is accepted.
- (c) needs a test that `scripts/deploy`'s ff-only `gated`/`prod` move,
  `scripts/round status`, the round diff review and `fleet refs` "behind
  main" all behave when `prod` is a second parent on main (the release
  head).

## Decided (Reto, ship-gate-ci-1, 2026-10-03 22:13Z)

- Forward merge in the same command as the release fix, not at deploy. A
  conflict stops and asks. Slice (b) design: `reviews/ship-gate-ci.md`
  § "Design note 5".

## Slices (builds)

(a) `round cut` and the `release/**` CI trigger: shipped 2026-10-03
(06247b684). The migration scan flags only duplicates beyond what
`origin/prod` already carries (main has historical 0037/0039 pairs), and the
plan-skip on an already-verdicted cut sha is deferred. (b) `ship --release`
with the forward merge: shipped 2026-10-03 (applies a tree's change onto the
open release and merges it forward into main in one narrow-lock section;
migrations and `safe_fetch.py` refused without `--allow-migration`). (c) `round gate`/`deploy` on the release head, the tag,
the merge-back, and docs (`/round`, `/fleet`, CLAUDE.md ship section).

## Slice (c) focused design (release worker, 2026-10-04)

Architecture-review D1–D6 revisions:

- Exact remote release head only; no main/parent fallback. CI helper binds a
  fresh SHA-keyed check view to check.yml's run/attempt and its distinct
  required jobs (lint + 3.13 shards 1–6; optional complete 3.12 set). Job
  conclusions and verdict age come from one attempt-jobs response. Unknown,
  duplicate, missing, mixed-head, partial, paginated or future-clock facts
  refuse. Main fallback retains its existing selection when no release exists.
- Separate common-dir advisory lifecycle lock: open/close/cut/abandon/deploy
  and release-fix final section; lifecycle before narrow ship when both apply.
  Stable inode, no unlink/age-steal; deployment child inherits descriptor
  ownership if wrapper dies. Scope is one checkout, not distributed locking;
  external controllers still require CAS/rechecks. Main lands do not acquire
  this lock. Main full gates nonblockingly defer global gated publication while
  lifecycle is busy or release recorded, retaining their local pins. Cut checks
  gated/prod ancestry, so a main publication that won before cut cannot open
  an undeployable release. No force rewind.
- Phase journal owns the release head beyond lock lifetime. Release ships,
  abandon/close refuse unresolved attempts. Retargeting is an explicit future
  coordinator reconciliation; never automatic. A cut intent is persisted
  before expected-absent creation and reconciled after uncertain acknowledgement.
- Deploy preflight: exact fresh CI certificate, descendant of cut, FF gated
  AND prod, unchanged release head, compatible local pin and immutable tag.
  Persist pin/certificate before effects; existing deploy SHA --pinned path
  retains its health/convergence checks. Recheck head before/after rollout.
- rc0 + fresh success marker + exact remote prod means **rollout returned
  success**, not runtime health. Exit 3 leaves the release open for actual
  coordinator observation of every required daemon's boot SHA/readiness and
  session MCP. `round deploy --confirm-runtime SHA --runtime-evidence TEXT`
  records that current live observation only after successful rollout. It is
  an operator attestation, not an automated installed-metadata health proof.
  Wrong/missing target/evidence or changed prod/head cannot retire the branch.
- Runtime-confirmed closeout publishes/reuses the immutable LIGHTWEIGHT
  deployed/r<N> tag; an annotated or conflicting tag is refused. Merge the
  pinned SHA forward with merge-tree/commit-tree and bounded FF CAS unless
  already contained. Recheck main/prod/tag/head, lease-delete only inspected
  release head, clear record last. Retain CI/runtime evidence in deployed state.
  Archive an immutable common-dir receipts/r<N>.json before final state clear
  and round replacement. Successful exact archive publication is the terminal
  completion commit point: if final round.json persistence is interrupted,
  reconcile the matching original journal from that archive before evaluating
  receipt refresh. Preserve archive bytes/evidence; conflicts still refuse.
  Subsequent install repairs are outside this completed round and do not produce
  a new health claim or reinstall. Completed deploy retries are terminal/idempotent and
  never re-enter main fallback. Present malformed/unreadable lifecycle state
  refuses all round operations without overwriting evidence. Round and shell
  publication/journal readers share the same identity/phase validator; empty
  release/deployed/deployment records are invalid, never absence. Runtime observation
  stores the exact success-marker content/mtime/inode; a changed generation,
  including the same SHA, supersedes the observation and requires fresh runtime
  evidence without another install. Recheck that generation before closeout.
  After acknowledged or uncertain remote deletion, fresh confirmation requires
  retained prior runtime proof, the matching immutable tag, exact current
  receipt/prod and main containment; unexplained missing branches still refuse.
  Failed tag/merge/delete resumes closeout without reinstall or CI age policy;
  newer/conflicting prod stops recovery. Missing branch resumes only from
  retained verified journal, matching tag/prod and main containment.

| Journal phase | Next operation | CI freshness | Release mutation |
|---|---|---|---|
| cut intent | reconcile expected SHA/create | selection already recorded | blocked |
| selected / rollout uncertain | same-SHA rollout retry | required before hosts | blocked |
| rollout returned success | current coordinator runtime observation | no reinstall | blocked |
| runtime confirmed | tag / final merge / leased retirement | closeout ignores age | blocked |
| remote deleted, state remains | validate evidence, clear record | no reinstall | blocked |
| retired | close/open next round | next round's policy | available |

Verification: bare origins and fake deploys cover exact certificate, main
publication ownership, second-parent prod/status/fleet refs/diff, release/main
races, killed wrapper/live child, stale runtime evidence, immutable tags,
conflicts, leased retirement and every partial retry. Full suite/live round
remains coordinator-scheduled. Existing raw scripts/deploy's installed-versus-
running gap remains health-hardening work; slice (c) requires coordinator
runtime proof before certifying/retiring a release.
