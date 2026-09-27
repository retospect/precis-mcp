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
   **Radius-changing shell** done 2026-09-18, no solver change: the step
   between a neck and a wider bulge is a flat washer, `cap(6k,0) -
   hex(r)@…`, rule k ≥ r+3 (else `cut.overlap`); fusing the neck into the
   washer's hole mints six heptagons, fusing the bulge onto the washer's
   rim mints six pentagons, both as seam rings. First instance:
   `valve_shell.hx` / `valve_shell_lidded.hx`, (12,0) necks, a (24,0)
   bulge with two C2 wall holes, 4.7 Å radial gap to the `lid_pillbox.hx`
   (12,0) rotor. **Next slice** = `opening(port=)`, then the rest of
   §28.3 (the cosmetic canonical-frame symmetry sources stay deferred
   behind it, ruled 2026-09-18).
6. [ ] Smooth mapper: `precis-surface-kernel.md` holds the ticks for
   spec §28.4–6 and §28.8 (the valve tools); nothing about the order
   lives here.

## Follow-ups

- `check.py` residual skipped for multi-block files with consumed rims
  (also silently skips `sheet_pill_bump.hx`'s fused sheets) — restrict
  `consumed` to bond-attached sheets.

## What builds today (hexfold 0.1, verified with `hexfold check`)

sheet(12,12) + C60 `[2+2]` (residual 0); tube(5,5) + `cap(5,5)` + `[2+2]`
flank (residual 0, all-hexagon cap seam; ~85° warnings at sp³ atoms —
fixed by step 2's 109.5° ideal); pillar (sheet `hex(0)` → `tube(6,0)`,
native `{7:6}` seam); nanobuds 9-6, 8-7, DA/DB necks; cone(P); C60 hole;
tube_fuse; pill on a sheet — a capped pill above and a bump below, seamed
at the pill's foot ring, each tube top closed by a `cap(6,0)` flat lid
(`sheet_pill_bump.hx`, residual 0, no ERROR); flanged doughnut — two
`cap(24,0)` washers joined through a `(12,0)` tube wall and closed at the
outer equator by a `cap(36,0)` annulus in a k=3 seam, whose 24 seam atoms
are the trivalent Y-carbon functionalisation sites of
`rotary-ratchet-valve.md` (`flanged_doughnut.hx`, residual 0, no ERROR);
pendants on the Y carbons via `<seam>/s<i>` (`flanged_doughnut_oh.hx`,
six hydroxyls, residual 0, no ERROR).

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

## Ruling 2026-09-27 — target and order after `opening(port=)`

Reto: the target is "a generic framework to build arbitrary shapes …
Drexler's/Diamond Age nano machines", not any one box. Framing recorded
in `diamondoid-pattern-language.md`: se is the generic part system, each
shape language is a generator behind a **port type**; hexfold is the sp²
composed-from-parts language, `precis_surface` the freeform sp² one,
diamondoid (sp³ volume) the missing third.

Order for the assembly-as-conversation layer, all §28 items (this is tick
state against §28 and §25, not a second roadmap). **Revised 2026-09-27
(evening)** after the hierarchical-resolved-block discussion recorded in
`diamondoid-pattern-language.md`:

0. **Seam decay measurement** — relax a hexfold piece free and fused
   (`precis.structure.georelax.relax_graph` over `stick` seeds), per-atom
   displacement against graph distance from the rim, read off the decay
   length per rim type (zigzag, armchair). Sets the seam radius and
   validates the frozen-interior scheme everything below assumes. Numbers
   land in `diamondoid-pattern-language.md`; a test pins the decay.
1. Declare the **rim standard** in the spec (§10/§7) — two rim types,
   multiples of 6, the 30° grain-boundary adapter; proposal in
   `hexfold-seam-type-catalogue.md`. Same slice: **reserve the port
   payload** — `GeneratedPort` grows `lattice` + a typed `payload` slot,
   hexfold fills `{kind: rim, word, N, type}`; the sp³ facet is the second
   instance later. First, so `options` searches a typed space.
2. `options(handle, wish)` (§25.3, "the one genuinely new verb") over the
   existing `fit.alternatives` (`build.py::_fit_alternatives_finding`,
   built for `len` and `k` only). Wish = target + band; the band is the
   inner/outer tolerance shell.
3. `fit` on domain sets + chain propagation from pinned ends (§12.1 0.2,
   §22.3) and `sheet(W,H)` Å-extent snap *reporting*, wired to se's L2
   measures / `stackup` for referential tolerance. Confirmed unbuilt
   2026-09-27: no `domain`/`propagat` in `build.py`.
4. §28 step 4 — symbolic chain solver with stub geometry backend.
5. **Block joiner over resolved blocks** (se side; two blocks + port pair +
   seam type → seam motif, re-relax the two seam radii, `seam.leak`
   check, composite). Until here hexfold whole-spec composition is the
   joiner.
6. `catalogue` view + `hexfold_cache` (§26), keyed by environment type
   (bulk cell, edge motif), not block instance. Confirmed unbuilt.
7. Seam vertices (step 7) — pillbox and rectangular box are both examples,
   not the target; the corner-type quantisation is in
   `hexfold-sp3-seam.md`.

Not a search per part: §22.3 stands — one `options` call per wish, the
budget/placement split (§22.1) is the guard against spending the pentagon
budget greedily. Skill: extend `precis-hexfold-help`, no second skill.
Diamondoid scope: **ruled in the framework, out of the next build slices**
(2026-09-27); only the port payload (step 1) is reserved now. The sp³
interior fill is trivial and deferrable — see the diamondoid item.

**Steps 0 and 1 built (2026-09-27).** Step 0: seam decay measured and
pinned (`tests/test_hexfold_seam_decay.py`; numbers under "Seam decay,
measured" in the diamondoid item — 5 shells zigzag, 1 shell armchair on
the geo rung; `seam.leak` must be defined on bond/angle changes, not
displacement). Step 1: `Port.rim_type`, `rim.nonstandard` INFO, spec §10
rim standard, `GeneratedPort.lattice`/`payload` filled by `build_hexfold`.

**Step 2 built (2026-09-27, hexfold side).** `hexfold.options.options`
+ CLI `hexfold options`; `tests/hexfold/test_options.py`. Not built: the
se handler ("new handler" in §25.3) — a thin op over this function once
step 5 says what a handle is on a resolved block; `options` handles over
roll-up domains and collars `{Rxk @fit}` are still unexposed.

**Step 3 built (2026-09-27).** `hexfold.domains` — roll-up domains
`fit` / `fit in {…}` on `tube`/`cap`, arc consistency on rim `N` over
fuses and seams from the pinned ends (one probe build reads pinned `N`),
surviving product built and ranked, `fit.propagated` INFO, conflict →
`fit.unsolvable` with needs/offers/constraint; domains resolve before
`len=fit`. `hexfold.extent` — Å sheet extents snap to cells with
`extent.snap`, `measures(net)` = sheet `W`/`H`, tube `len` (band = snap
cell/period) and tube `R`. se: `GeneratedMeasure`,
`GeneratedBlock.measures`, `prepare_generate` mints `add_measure` rows
(m, gauge) so user relations onto `<block>.<inst>_W` stack up through
the existing `stackup` (`tests/test_se_hexfold_generator.py`). Not
built: collar domains; Å for tube `len` (the `options` Å wish covers the
query side); a per-connect `N` read for `@`-site hole destinations
(propagation uses the minted hexagon's 6).

**Residual from step 0 — fused seed placement.** `_place_seeds` filed
every fuse/bond transform under the destination instance and read it back
as the source's, so each neighbour got the transform computed for the other
side (mirrored behind the far rim; 8–78 Å crossing bonds in every
multi-instance example; `stick` then telescoped the halves). Fixed for
fuses, with the rim-frame normal now signed against the owning instance's
centroid (`tests/hexfold/test_place_seeds.py`). Still seeding long crossing
bonds after the fix, each a separate placement path and each worth its own
look before the block joiner (step 5) relies on seeds: bud `@` links
(`nanobud_87/96.hx`, `_bond_transform`), k ≥ 3 seams (`sheet_pill_bump.hx`,
`flanged_doughnut.hx`), fuses into `cap` hole rims (`valve_shell.hx`,
17.9 Å), and armchair fuses spaced one σ apart along the normal when their
dangling bonds are 30° off-axis (crossing bond σ/cos 30°, harmless — stick
closes it). `tube_ring_closure.hx` is a genuine loop and cannot be rigid.
