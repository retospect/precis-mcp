# multiscale design core

**Status:** ends when the shared design substrate the se/hexfold/pcb/cad
kinds stand on exists — design-state-core, pattern groups, complementarity,
make tree, attached models — per `backlog/multiscale-design-system-spec.md`,
with `backlog/multiscale-design-architecture.md` as the map. Today none of
the core pieces exist; the three `ready/high` items below are independent
enough to start in any order except where noted, and are the ones every
consumer kind (se-machine-design, pcb-platform) is implicitly waiting on.
**Last reviewed:** 2026-09-30
**Worktree:** `multiscale-design-core`
**Active:** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/design-state-core.md** — `ready/high`; unblocks pattern-groups
   below and is the node type every later milestone in this file writes
   into.
2. **backlog/complementarity-solver.md** — `ready/high`; independent of 1.
3. **backlog/pose-vector-units.md** — `ready/high`, small; independent of
   1 and 2, cheap to clear early.
4. **backlog/pattern-groups.md** — blocked-by 1 (design-state-core).
5. **backlog/make-tree-vs-design-tree.md**
6. **backlog/attached-models-layer.md**

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

## Parked

- (none)

## No action needed

- (none yet)

## Seam

`backlog/class-lattice-similarity-spaces-and-laws.md` is term-taxonomy's
item (a v2 section of its own taxonomy work); this file's Horizon 5 is a
pointer to it, not a second ranking. se-machine-design also consumes it for
pocket specs — three threads reading one item, one owner.
