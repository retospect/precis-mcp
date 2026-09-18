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
4. [x] Dogfood on the DEV DB (2026-09-17). Inputs are now committed
   examples: `hexfold/examples/sheet_bud_22.hx` (sheet(12,12) + C60
   `[2+2]`) and `capped_tube_da_neck.hx` (tube(5,5) + cap(5,5) +
   `[DA-neck(3)]`). Path: `scripts/dev` on this worktree's compose
   project → `precis-test-db` resolves; `Migrator.discover_sources` (the
   se chain is a plugin source — the legacy single-dir form leaves
   `se_blocks` missing); `precis tools put --kind se` with one `generate`
   op (the CLI mirror of the verb). Record: `fidelity=check` echoes the
   report and mints nothing ("no blocks yet"); `stick` minted se refs 14
   (`hxdog-sheet`) and 16 (`hxdog-tube`), each ONE block `bud` (uids 1,
   2) bound to structure refs 13/15 (`hxdog-sheet-bud`, `hxdog-tube-bud`);
   sheet: 348 atoms, 500 bonds, port `h_rim`, rings {6:141, 5:12}; tube:
   442 atoms, 658 bonds, port `h_in`, rings {6:191, 5:17, 7:7, 8:2}. The
   bond-mode bud is NOT its own block (one connected net → one block, as
   designed). `topology` keys: hexfold, spec, canonical_json, ports (se
   name → {hx, atoms, word, B}), regions, report, seed_kind, n_atoms,
   n_bonds, rings. Found and fixed: (a) multi-instance specs gave dotted
   port names (`h.rim`) that `add_port` refuses → `h_rim` + `hx`;
   (b) `canonical_json(net) != canonical_json(text)` with `origin` —
   0.1's canonical frame anchored defects at (0,0) regardless of rims
   (dragged the DA-neck host hole onto the tube's `in` rim; clipped sheet
   glyphs at the corner) and ignored menu-generated holes and site
   references in connects → spec §14.2 note; (c) `run_length` compressed
   digit symbols (`55`→`52`, re-read as one symbol) → digit runs stay
   `5.5`; (d) the generate echo dumped every topology value (kilobytes
   of ordinals) → keys/lengths only.
5. [ ] hexgen roadmap items in spec §28.3's order. **`cap(n,m)` flat-lid
   family** done 2026-09-18: zigzag `(6k,0)` family = the `hex(k−1)`
   flake, pentagons as seam rings; `sheet_pill_bump.hx` is now the
   capped pill, `lid_pillbox.hx` the rotor; armchair lids open. It
   unblocked both the pill (this item's acceptance) and the rotary
   ratchet valve's rotor, which is a lid pair (`rotary-ratchet-valve.md`).
   **Next slice** = the canonical-frame symmetry sources (cosmetic), then
   the radius-changing shell (valve shell), `opening(port=)`, and the
   rest of §28.3.
6. [ ] Smooth mapper: `precis-surface-kernel.md` holds the ticks for
   spec §28.4–6 and §28.8 (the valve tools); nothing about the order
   lives here.

## What builds today (hexfold 0.1, verified with `hexfold check`)

sheet(12,12) + C60 `[2+2]` (residual 0); tube(5,5) + `cap(5,5)` + `[2+2]`
flank (residual 0, all-hexagon cap seam; ~85° warnings at sp³ atoms —
fixed by step 2's 109.5° ideal); pillar (sheet `hex(0)` → `tube(6,0)`,
native `{7:6}` seam); nanobuds 9-6, 8-7, DA/DB necks; cone(P); C60 hole;
tube_fuse; pill on a sheet — a capped pill above and a bump below, seamed
at the pill's foot ring, each tube top closed by a `cap(6,0)` flat lid
(`sheet_pill_bump.hx`, residual 0, no ERROR).

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
