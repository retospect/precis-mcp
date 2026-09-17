---
status: in-progress
title: hexfold fold-in — the sp² notation library as src/hexfold, se's `hexfold` generator, and the 0.2 discrete work
prio: high
model: opus
---

# hexfold fold-in

Living state for the hexfold ⇄ precis thread. **Durable truth is
`src/hexfold/spec.md`** (format 0.2 draft: notation, seams, smooth layer,
roadmap §28, open questions §29, decisions §30, sources §31). This file
holds only what the spec does not: where the build stands and what is
next. Delete on ship of the last step below.

Decision 2026-09-16 (Reto): hexfold (`github retospect/hexfold`, MIT)
folds into this monorepo to simplify deployment during rapid
development, with the boundary kept clean for a later pip re-export.

## Layout (decided)

- `src/hexfold/` — the package, MIT `LICENSE` inside, imports nothing
  from `precis*` (import-walk test); hatch `packages` += `src/hexfold`;
  `[project.scripts] hexfold = "hexfold.cli:main"`.
- `tests/hexfold/` — its 102 tests (7 files), run by `scripts/test`.
- `hexfold/` (repo root) — export seed only: short README pointing at the
  spec, `examples/*.hx`, `CITATION.cff`, `.zenodo.json`, MIT `LICENSE`.
- `src/precis_se/atomic/generators/hexfold_spec.py` + skill
  `precis-hexfold-help` + `tests/test_se_hexfold_generator.py` —
  cherry-picked from `feat/hexfold-integration` (`b76d5225`,
  `11b4a0d1`; also brings `se-pick-hierarchy.md`). After the fold-in the
  lazy import and `pytest.importorskip` become direct imports.
- Seam: hexfold owns topology (`Net`), precis owns geometry and storage;
  `params={"spec": "<.hx>", "fidelity": "check|stick|geo|emt|ml"}`
  (`dry_run` ≡ `fidelity='check'`); one connected net → one `structure`
  design bound to one se block; `GeneratedPort.atoms` carries the ring.

## Steps

1. [x] Fold-in (spec §28.1): copy, tests, export seed, pyproject, boundary
   test, cherry-pick, direct imports (2164cb83, 9bd482d0); targeted
   suites green, full gate at land.
2. [x] 0.2 discrete work (spec §28.2), five slices (d3e51217 header +
   `seam.rings` + sp³ ideal; 824db1e8 `fit` families; 21e5cf28
   `registry.closure` + `tube_ring_closure.hx` torus; 45bae687 sectioned
   JSON + `generated` + `gen.stale` + `op.dangling` + `capped_tube.hx.json`;
   f665d949 `seam` k ≥ 3 + per-sheet χ + `sheet_pill_bump.hx`). The
   acceptance example uses OPEN tubes above and below the hole: the
   capped pill needs the `cap(n,m)` flat-lid family (step 5). Found and
   fixed on the way: rebuilding a built spec re-expanded menus
   (ee3d5d17 — this was the `canonical_json(net)` call the se generator
   makes, crashing on DA-neck).
3. [x] Sources: 22 DOIs Crossref-verified, 20 papers imported, store ids
   in spec §31 (2026-09-16).
4. [ ] Dogfood on the DEV DB (`scripts/dev`; never the session MCP).
   The earlier inputs (`a_sheet_bud.hx`, `c_tube_bud.hx`) lived in a
   gitignored dir of the since-deleted `hexfold` worktree and are gone:
   re-author them (a sheet with a `[2+2]` bud; a capped (5,5) tube with a
   `[DA-neck]` bud) — or start from `hexfold/examples/nanobud_*.hx`.
   Blocked last time at the DSN in `precis-dev.sh`
   (pool: `failed to resolve host 'precis'` — password likely needs
   URL-quoting or the compose service name differs; check
   `docker/dev/compose.yaml`). Record per spec: dry-run echo, minted
   block uids, structure ids, atom/bond counts, whether the bond-mode bud
   became its own block, `topology` keys.
5. [ ] hexgen roadmap items (spec §28.3) in order, starting with
   `cap(n,m)` flat-lid family and `opening(port=)` → the pill.
6. [ ] `precis-surface-kernel.md` (stage 1 chain solver) once 1–2 hold.

## What builds today (hexfold 0.1, verified with `hexfold check`)

sheet(12,12) + C60 `[2+2]` (residual 0); tube(5,5) + `cap(5,5)` + `[2+2]`
flank (residual 0, all-hexagon cap seam; ~85° warnings at sp³ atoms —
fixed by step 2's 109.5° ideal); pillar (sheet `hex(0)` → `tube(6,0)`,
native `{7:6}` seam); nanobuds 9-6, 8-7, DA/DB necks; cone(P); C60 hole;
tube_fuse. **Fails:** pill on a sheet — `hex(r)` seats a zigzag
`(6(r+1),0)` post and `cap` exists only for `(5,5)` → `port.mismatch
6 != 10` (unblocked by step 5's lid family).

## Standalone-repo state (for the eventual re-export)

PyPI `hexfold` 0.0.1 placeholder published (name reserved); 0.1.0 not
published; `release` workflow disabled; trusted publisher + Zenodo wired
but not armed. Incident resolved: `472df94` swept stale IDE buffers into
main, `ed631cc` restored them. Once folded in, the standalone repo's
README becomes a pointer here.

## Acceptance (unchanged from the 2026-09-15 design session)

- `hexfold` generator builds a `(5,5)` tube + C60 `[2+2]` nanobud
  end-to-end into a `structure` design on the dev DB; byte-deterministic
  topology; `fidelity='check'` returns a report without minting.
- Adapter test round-trips `Net → GeneratedBlock → Scene` on atom count,
  bond count, path↔ordinal map — nothing about lattice semantics (that is
  hexfold's own suite).
- Follow-on (not this item): `cnt`/`fullerene`/`cone` generators collapse
  into hexfold specs so there is one lattice implementation.
