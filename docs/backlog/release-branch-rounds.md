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
4. `round gate` reads the release head's verdict, under the same rule as
   main: lint + ≥ 6 `test-linux` all SUCCESS.
5. `round deploy` deploys the head pinned, moves `gated`/`prod`
   fast-forward, tags `deployed/r<N>`, merges the branch back into main as a
   real merge (a no-op once every fix was forwarded) and deletes the branch.

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
plan-skip on an already-verdicted cut sha is deferred. (b) `ship --release` with
the forward merge. (c) `round gate`/`deploy` on the release head, the tag,
the merge-back, and docs (`/round`, `/fleet`, CLAUDE.md ship section).
