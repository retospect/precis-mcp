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
**Last reviewed:** 2026-10-02 (hold lifted). The diagram fold/annotation
web half landed (0f41f3f04). R1 like-with-like ranking (per-measure
network-basis stamp, stale competitor-margin demotion, scoped restale pass,
status-view marker; design review §14 + F1–F3 accepted) landed in the same
squash, then was backed out of main by the quest-file restore that follows
it. It waits on Reto's reading of his "no ranking change before
u-ph-window" ruling (review-queue `catalysis-selectivity-15`). To re-land,
revert that restore commit. The legacy backfill by value match is also his
call (`catalysis-selectivity-14`); catpath 0.23.0 (Part B) runs in Reto's
`catpath` tmux window, and he sends the release sha for
`uv lock -P autocatpath`. Pourbaix job A: rulings recorded, build next.
**Worktree:** `catalysis-selectivity`
**Active:** yes — Reto, 2026-10-02 ("high up").

## Do next

1. **backlog/pathway-nh3-network-completeness.md** — the screen measures
   NH₃ selectivity against the wrong competitors until N–O cleavage after
   hydrogenation, N₂ and N₂O are in the network; every ranking on
   qu164903 inherits the error. Slice 1 done (parked is incomplete);
   screening stays parked (R2); the like-with-like network-basis ranking
   is built and held (`catalysis-selectivity-15`). Left: catpath 0.23.0 (NO+NO coupling, NH₂OH
   scission, template-level `network_digest`) → `uv lock -P autocatpath`
   → the neb-tier re-run of pw455722's candidate (steps 5–6).
2. **backlog/pathway-diagram-step-annotations.md** +
   **backlog/catpath-desorption-link-kind.md** (its first slice, moved
   here from chemistry Horizon 2) — independent of 1, parallel. The web
   half (fold + label inference) has shipped; left: the catpath
   `added`/`removed`/`kind` link fields, then Reto's look at pw455722
   (the shoulder question).
3. **backlog/pourbaix-bulk-verdict-job.md** →
   **backlog/pourbaix-quest-gate.md** — Reto asked for it next after the
   network (2026-10-02); independent of 1 and 2. A candidate whose bulk
   dissolves across the operating window cannot be the catalyst, so it is
   ruled out before selectivity is spent on it (qu202468). The job (A)
   builds now; the gate (B) waits on A and on Reto's operating point.
4. **backlog/pathway-selectivity-u-ph-window.md** — the objective Reto
   named; blocked by 1 because a window over an incomplete network is
   the wrong window. Post-processing only, so cheap once 1 lands. Also
   owns a screening criterion that ranks: R2 (2026-10-02) measured that
   thermo-only margins do not.
5. **backlog/pathway-kinetics-promotion-gate.md** — where barriers are
   spent; needs 4's `U_sel` to pick the decisive edges.

## Horizon

1. **backlog/neb-barriers-in-the-catpath-pipeline.md** — owned by
   chemistry (its Horizon 1); Do-next 5 decides where NEB runs, this
   item makes NEB run at all. Kinetic margins wait on it.
2. **backlog/pathway-conditions-effects-report.md** — chemistry's report
   for qu164903; consumes the window scalars from Do-next 4 instead of
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
- **roadmap-quest** owns promotion dispatch; Do-next 5 changes the
  promotion rule; re-dispatch and infra retries now keep each candidate's
  own rung (`quest/compute.py::_redispatch_tier`, `_retry_tier`).
