# catalysis-selectivity

**Status:** ends when a pathway answers "does the move toward the target
win at every fork, thermodynamically and kinetically, and over which
potential and pH window" for NO→NH₃, on a network that contains every
known route and competitor, and the explorer shows each step as one
chemical change with what entered and left. Today (Reto, 2026-10-02, from
pw455722) the screen runs a minimal network that cannot reach NH₃
associatively, the objective is span at one U, and the diagram draws
H-supply as a dashed sawtooth; order is network first (every number
downstream is measured against it), the diagram in parallel (it is how
Reto reads the results), then the objective, then where barriers run.
**Last reviewed:** 2026-10-02 (thread created)
**Worktree:** `catalysis-selectivity`
**Active:** yes — Reto, 2026-10-02 ("high up").

## Do next

1. **backlog/pathway-nh3-network-completeness.md** — the screen measures
   NH₃ selectivity against the wrong competitors until N–O cleavage after
   hydrogenation, N₂ and N₂O are in the network; every ranking on
   qu164903 inherits the error. Slice 1 (is the parked `ammonia` template
   complete?) is a read, do it first.
2. **backlog/pathway-diagram-step-annotations.md** +
   **backlog/catpath-desorption-link-kind.md** (its first slice, moved
   here from chemistry Horizon 2) — independent of 1, parallel. The web
   half (fold + label inference) needs no engine change; the catpath
   link fields follow.
3. **backlog/pathway-selectivity-u-ph-window.md** — the objective Reto
   named; blocked by 1 because a window over an incomplete network is
   the wrong window. Post-processing only, so cheap once 1 lands.
4. **backlog/pathway-kinetics-promotion-gate.md** — where barriers are
   spent; needs 3's `U_sel` to pick the decisive edges.

## Horizon

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — owned by
   chemistry (its Horizon 1); Do-next 4 decides where NEB runs, this
   item makes NEB run at all. Kinetic margins wait on it.
2. **backlog/pathway-conditions-effects-report.md** — chemistry's report
   for qu164903; consumes the window scalars from Do-next 3 instead of
   U_L / U_opt alone.

## Parked

- (none)

## No action needed

- (none yet)

## Seam

- **chemistry** owns the catpath engine's health and output contract;
  this thread changes the network content and adds link fields. Engine
  edits here bump the catpath version through chemistry's
  `catpath-wheel-version-reuse.md` path, never a second wheel.
- **plugin-split** owns `pathway-presentation-shared-module.md`; if it
  lands before Do-next 2, the diagram change goes into it.
- `pathway-viewer-ux-batch.md` item 3 (where H₂O leaves) is subsumed by
  Do-next 2; the rest of that item stays with chemistry.
- **roadmap-quest** owns promotion dispatch; Do-next 4 changes the
  promotion rule; re-dispatch and infra retries now keep each candidate's
  own rung (`quest/compute.py::_redispatch_tier`, `_retry_tier`).
