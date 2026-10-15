# multiscale design core

## Resume

- **Pillar:** 3d-design
- **Next:** pattern-groups (design-state-core shipped in r37, post-deploy
  check passed 2026-10-15).
- **Blocked by:** No declared active owner.
- **Unblocks:** A shared substrate for se, hexfold, PCB and CAD.
- **Acceptance:** Use [pattern-groups](../pattern-groups.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `multiscale-design-core`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: [fleet roster](../../../.claude/fleet/threads.tsv); dated allocation decisions below are historical.

**Status:** ends when the shared design substrate the se/hexfold/pcb/cad
kinds stand on exists — design-state-core, pattern groups, complementarity,
make tree, attached models — per `backlog/multiscale-design-system-spec.md`,
with `backlog/multiscale-design-architecture.md` as the map. Today none of
the core pieces exist; the three `ready/high` items below are independent
enough to start in any order except where noted, and are the ones every
consumer kind (se-machine-design, pcb-platform) is implicitly waiting on.
**Last reviewed:** 2026-09-30
**Worktree:** `multiscale-design-core`
**Allocation decision (historical):** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/pattern-groups.md**
2. **backlog/make-tree-vs-design-tree.md**
3. **backlog/attached-models-layer.md**

(design-state-core.md deleted 2026-10-15: shipped in r37; the post-deploy
check passed — designs load and validate, no dangling_* in drc, checkpoint
accepted. History: `git log -- src/precis/design src/precis_se`.)

(pose-vector-units.md deleted 2026-10-13: every round shipped; the
notation-robustness rerun moved to backlog/claude-code-on-local-serving.md.)

## Horizon

1. **backlog/multiscale-optimisation-method.md**
2. **backlog/margin-budget-tree.md**
3. **backlog/structural-solution-space.md** — the cad-sdf field export it
   was blocked on has shipped (se-machine-design's cad-sdf item, deleted;
   field leaf + `realize(strategy='simp')` bridge); sequenced here by
   choice.
4. **backlog/drive-characteristic-scale.md** — blocked on a ruling.
5. **backlog/class-lattice-similarity-spaces-and-laws.md** — owned by
   term-taxonomy; consumed here for pocket specs. Seam, not a duplicate
   rank — see below.
6. **backlog/njit-hot-loops.md** — numba kernels for the shared geometry
   loops (structure pair-MIC, hexfold stick relax shipped 2026-10-01); open:
   prod-data parity check (Reto), chain relax, pcb coupling pairs (pcb
   thread owns), serve warm-up.

## Parked

- (none)

## No action needed

- (none yet)

## Seam

`backlog/class-lattice-similarity-spaces-and-laws.md` is term-taxonomy's
item (a v2 section of its own taxonomy work); this file's Horizon 5 is a
pointer to it, not a second ranking. se-machine-design also consumes it for
pocket specs — three threads reading one item, one owner.
