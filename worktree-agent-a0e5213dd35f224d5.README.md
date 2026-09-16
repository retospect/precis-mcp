# worktree-agent-a0e5213dd35f224d5 — continuation prompt

Branch: `worktree-agent-a0e5213dd35f224d5`. Written 2026-09-15 after
blocktree slice 2 shipped. Paste the block below as the first message of a
fresh session to resume the multiscale design-system campaign.

---

Resuming the multiscale design-system campaign. Reorient, then continue.

**Where:** worktree `/Users/reto/precis-mcp/.claude/worktrees/agent-a0e5213dd35f224d5`
on branch `worktree-agent-a0e5213dd35f224d5`, clean and level with `main`.

**Goal:** advance the blocktree library build plan. Slices 1 and 2 are done;
**slice 3 (complementary port roles) is next on the critical path.**

**Done so far:**

- Blocktree **slice 1** (cross-design instancing) shipped `a5efc341`
  (2026-09-07), gaps closed in `15598afa`.
- Blocktree **slice 2** (discrete block states + stimulus-labelled
  transitions, se consumer side) shipped `7f7f3a75`; its shipped record is
  `b6301f6d`. Storage had already landed in `7bb28f55` (design-core round 1,
  migration `0162`), so slice 2 added no migration.
- Open gripe **gr342026**: `port_pose_overrides` is direction-only
  (`{port: {'direction': [x,y,z]}}`) because `Port`/`PortSpec` has no
  absolute position field. A state can re-aim a port but not move it.

**In flight:** nothing. No agents, no gate.

**Next logical steps:**

1. Read `docs/backlog/blocktree-library-build-plan.md` §Slice 3
   (complementary port roles: `azide ↔ alkyne`, connect gate moves from set
   intersection to complementary halves). **Before building, check whether
   the core half already exists** — see the trap below.
2. Settle gr342026 if slice 3 or a named consumer would bake in
   direction-only port semantics.
3. Reuse the complementarity vocabulary already specified in
   `nm-face-codes-and-scale.md` (donor↔acceptor, bump↔hole, +↔−) rather than
   minting a parallel one. Keep the declared-intent trust model: port roles
   are labelling, never chemistry proof.

**Re-read to reground:** `docs/backlog/blocktree-library-build-plan.md`
(§Slice 3, §Critical path), `docs/backlog/nm-face-codes-and-scale.md`,
`src/precis/design/states.py`, `src/precis_se/ops.py`, auto-memory
`multiscale-campaign-state.md`.

**Watch out:**

- **Check before building.** Slices 1 and 2 *both* turned out to have their
  core half already shipped, slice 1 in the same squash that authored its own
  spec. A spec section reading as open is not evidence it is unbuilt — check
  `src/precis/design/`, `git log --oneline -- <path>`, and the actual symbols
  first.
- **`scripts/test` is pytest-only; it does not run mypy.** A fully green
  coder chain can still redden the gate on types, costing a full gate cycle
  each time under contention. Removing a type-ignore that mypy calls
  "unused" can expose the real error it was masking.
- The gate host is often contended by sibling sessions — a slow, stalled, or
  killed run is congestion, not a code failure. Re-run in a quiet window
  rather than churning code.
- Don't let two trees edit `src/precis_se/drc.py` at once; an unshipped DRC
  connect-geometry pass sits at `aec09983` in worktree
  `agent-afb7d4aa6641540d3` (its ship is blocked by the permission
  classifier in agent context — Reto runs it himself).
- `main` is ahead of the cluster; deploying is Reto's call, not an agent's.

Start by reading the "Re-read to reground" pointers, then do step 1.
