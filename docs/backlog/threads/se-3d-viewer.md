# se 3D viewer

**Status:** ends when a reader can click any block of an se design and
read its pose, envelope, ports, findings and load path in one panel, and
the same scene yields publishable figures and the feasibility/cost answer
(backlog/se-feasibility-and-cost.md). Today the affordances work and are
verified by canvas pixel-diff at the deployed sha; the list is about making
that verification repeatable by someone other than the author, then the
deferred features. No live corruption. The viewer renders precis_se atomic
output, so hexfold's gr457995 shows here as a wrong picture; that is the
hexfold-toolkit thread's item.
**Last reviewed:** 2026-09-30
**Worktree:** `se-3d-viewer`

## Do next

1. **backlog/se-viewer-browser-level-check.md** — no browser-level check
   exists, so every correctness claim rests on a hand-built harness in a
   worktree that will be reaped. Leverage: makes 3–5 verifiable instead of
   assertable, and is the only guard against the defect that started the
   thread (a dead viewer behind a green suite).
2. **td458066** — Reto decision: whether prod is ever browser-checked by an
   agent and how the Basic credential is handled. Blocks 1's scope (local
   fixture lane vs prod lane are different builds), cheaper answered before
   1 is designed.
3. **backlog/se-3d-viewer-ux-batch.md**, visibility via the public setState
   API — applyContainerMode drives visibility through private
   `_rendered.nestedGroup.groups[path]` handles that do not survive a later
   setState(). Same class as the original inert toggle, fails silently.
   Below 1 because the adjacent case held on 2026-09-30 (n=0 pixel-diff), so
   the real trigger is narrower and needs the harness to pin down.
4. **backlog/se-3d-viewer-ux-batch.md**, per-block level chips — semantics
   ruled by Reto 2026-09-29 (`—` when a rung renders the block identically
   to its neighbour). New work is server-side: scene3d.json must carry, per
   block, which rungs differ. Above 5 because it is specified.
5. **backlog/se-3d-viewer-ux-batch.md**, bidirectional hover — the vendored
   bundle has no hover callback, so this needs an own throttled raycaster;
   the addressing half shipped. Last feature because no design is decided.
6. **gr457931** — se has no ops-export view and stores designs normalised, so
   a prod design cannot be reproduced locally; the harness can only check
   code, never prod data. Tooling for a thread with no corruption, so below
   the features.
7. **backlog/se-mechanical-drc.md** — fastener_insertion_path, final-state
   only, rulings 1–5 in the file. Independent validator pass and the largest
   piece of work, hence last.

## Horizon

One keystone, then everything that has been waiting on it. If cut short,
cut from the bottom.

1. **backlog/se-pick-hierarchy.md** — click anything, get every level it
   belongs to. Keystone: three parked and two Do-next items have nowhere to
   render until a block can be selected and addressed. Unblocked today
   (id/name unification shipped); shared with se-nucleic-chain, whose
   residue/base-pair instance is the same surface.
2. **backlog/se-3d-viewer-ux-batch.md**, selection inspector + per-block
   findings + honesty banner — waits on 1; the first panel where a block's
   pose, envelope, ports, connects and findings read at once; unparks three
   parked items in one go.
3. **backlog/se-interface-reaction-forces.md** — waits on 2 for somewhere to
   put the number; delivers Reto's original ask and the load-path data 4
   needs.
4. **backlog/se-mechanical-drc.md** beyond fastener_insertion_path — phase
   2: declared install-order boundaries, load-path-aware tuning. Waits on
   3, which answers the file's open question (c).
5. **backlog/se-container-block-is-not-first-class.md** — three subsystems
   each guess what a container is. Waits on nothing; ranked here because
   the payoff shows only once viewer, validator and DRC all read
   containers (from 4 on).
6. **gr458084** — the revision scrubber still full-page reloads; second
   consumer of the live-scene seam, where losing the camera hurts most.
   Convenience, not correctness.
7. **backlog/se-view-figures.md** — waits on 1–2 to know what a figure
   captures; publishable figures out of the same scene.
8. **backlog/se-feasibility-and-cost.md** — the far end the arc serves;
   waits on 3 and 4.

## Parked

- **backlog/se-3d-viewer-ux-batch.md**, colour-channel selector / clip
  plane — unparks with Horizon 2, when a block-selection surface exists.
- **backlog/se-3d-viewer-ux-batch.md**, hide the picking dropdown + label the
  tree icons — CSS-and-copy only; unparks the next time that template is
  edited for another reason.

## No action needed

- **backlog/se-3d-viewer-ux-batch.md**, "live scene, client-side isolate" —
  shipped, re-verified 2026-09-30 by pixel-diff at the deployed sha.
- The "Multiple instances of Three.js" console warning — expected, documented
  in blocktree-3d.js; no gripe.
