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

**Step 4 built (2026-09-28).** `hexfold.chain` — the part-level layer
above the `.hx` text: `Part` (kind; roll-up pinned / `domain` /
don't-care = backend catalogue; `periods` pinned / free in a range;
`spacer` with a real `(min, max)` band; `block` = an opaque pinned part
from a resolved block's typed port payloads, `part_from_payloads`),
`solve(parts, bottom=, top=, wish_A=)` = arc consistency on rim `N` over
the adjacencies from the pinned ends (`chain.propagated`,
`chain.unsolvable` needs/offers/constraint, `chain.mismatch`,
`chain.no_rim`, `chain.too_many` above 512 combinations) then a length
pass per surviving roll-up combination (free whole periods + spacer bands
nearest the wish; `chain.length` with the nearest total; rank = deviation,
adapter count, periods; `chain.adapter` INFO for z/a seams incl. typed
pinned ends). `GeometryBackend` protocol (`catalogue`, `rim`, `pitch_A`,
`fixed_length_A`); `StubBackend` answers from the chiral-index formulas
with no build (pinned equal to `hexfold.lattice` / `domains.rim_n` by
test). End parts with one rim auto-face the chain. Tests
`tests/hexfold/test_chain.py`; the cross-generator interface proof
`tests/test_se_hexfold_chain.py` (a hexfold-generated se block → pinned
part from `GeneratedPort.payload` + its `_len` measure → composes with
stub caps). Not built: a se handler op over `solve` (step 5 says what a
chain is on a design), `hexfold chain` CLI, fuse phase `k` as a chain
variable (affects neither `N` nor length), cone/sheet parts (a sheet is
a `hole` wall in a chain).

**Seed placement residuals (from step 0), fixed 2026-09-28.** Three
placement paths seeded 5-26 A crossing bonds that `stick()` then hid
(every earlier test asserted post-relax lengths); each now has a
*pre-relax* `net.seed3` case in `tests/hexfold/test_place_seeds.py`:
nanobud `@` menus (no placement edge; now a six-point Kabsch fit from
`_solve_bud_attach`'s pairs plus a one-sigma outward seed offset, because
the six host atoms mix ring and second-shell atoms and have no shared rim
normal to twist about), k >= 3 seams (placement-only `fuse_frames`
entries per consecutive rim pair; `_place_seeds` runs real fuses first and
seam edges only for what they cannot reach), and flat washers with two
fused rims (outer rim forced antiparallel to the hole rim; flatness is an
absolute extent test in sigma, not a ratio, since a wide len=1 tube has a
small axial/transverse ratio). `hexfold.place.place_graph` (slice 3,
2026-09-28: joint least-squares placement over a part-graph cycle,
alternating Kabsch/Gauss-Seidel, wired into `_place_seeds` as a third
pass scoped to instances not already uniquely pinned by real fuse/bond
edges alone) closes `tube_ring_closure.hx`'s genuine 2-edge loop (24.2 A
-> 3.7 A max crossing bond) but leaves `flanged_doughnut.hx` essentially
unchanged (11.05 A, was 10.91 A): its real `top<->wall<->bottom`
sub-chain has zero redundancy of its own (the k=3 seam's `top<->bottom`
edge is the *only* source of the cycle), so nothing in that chain is up
for grabs, and the seam's own registration disagreeing with it by a
rotation is verified irreducible by any rigid instance placement, joint
or not (an unweighted joint fit that also lets the real chain move
measured worse, 11.6-11.7 A, with sub-1 A near-overlaps). `_place_seeds`
is still single-rooted at `origin` (a fused component unreachable from it
stays unplaced; no example needs it yet); armchair fuses seeded
sigma/cos 30 apart are harmless.

**Step 5 slice 1 built (2026-09-28).** `hexfold.join` (the pure numpy
half, `src/hexfold/join.py`: `Block`, `block_from_net`, `rank_k`, `place`,
`compose`, `SEAM_RADIUS`, the `Relaxer` protocol, `stick_relax_pinned`'s
pin-mask refactor) plus `precis_se.atomic.join` (the store-aware `join`
op, `src/precis_se/atomic/join.py`): compose two already-resolved blocks
over a matched port pair into a new composite se block, on the stick
rung, re-relaxing only the seam sub-graph. Design calls decided in
`nanomachine-slice-5-joiner.md`: (1) the composite is a new parent block
+ new `structure` ref with `a`/`b` as posed children, never an in-place
merge; (2) slice-1 rung is stick with a pinned relax, geo-only deferred;
(3) join-time rings/zones come from rebuilding each side's topology off
its own generator record (recursively, through a chain of earlier
joins) rather than persisting rings/regions/walks — the prerequisite
this needed is `GeneratedPort.lattice`/`payload`/its dangling ring's
atom *labels* now landing in the stored port's `annotations` (only for a
typed — today hexfold — port; the pre-existing single-atom generators'
ports are unchanged). Findings `seam.rings`, `seam.adapter`,
`seam.leak`, `seam.strain`, `seam.radius.unmeasured`, `seam.terminated`,
`fit.alternatives`, `port.mismatch`, `join.lattice`, `seam.mismatch`,
`join.stale`. Still owed: slice 2 (the geo rung, `join.rung` gating a
rung mismatch, leak thresholds re-measured on that rung) and slice 3
(`place_graph` joint placement across a part-graph cycle, wired into
`_place_seeds` as its third pass — the seed-placement residuals above,
`flanged_doughnut.hx`/`tube_ring_closure.hx`, are exactly what that
slice retires).

**Step 5 slice 2 built (2026-09-28).** `geo_relax_pinned`
(`precis_se/atomic/join.py`: a `relax_graph` adapter over the seam
sub-graph `compose` hands it) plus the rung gate `_select_relaxer`: each
side's rung comes off its own bound structure's
`meta['last_relax']['rung']` (absent → `"stick"`); both sides agreeing
picks that relaxer, a mismatch is `join.rung` (`BadInput`, before
`compose` ever runs — no sane geometry to hand back for two blocks on
different rest lengths), and the op's own `"rung": "stick"|"geo"|"auto"`
key (default `auto`) can force one, `join.rung` WARN when that forces
`geo` over a still-stick-rung block. `hexfold.join.compose` gained a
keyword-only `leak_thresholds` override so the geo rung checks against
its own measured numbers (`LEAK_THRESH_GEO`, `(0.002 Å, 0.15°)` uniform
across rim type) rather than the stick numbers above, which are measured
on different physics (a pinned guard band, not a fully free relax) and
would spuriously fire on ordinary geo-rung noise. `meta['generated']
['relaxer']` records the rung actually used. Verified end to end
(`tests/test_se_join_geo.py`, `slow`): two `tube(8,0,len=4)` parts
independently relaxed to geo, joined at the table radius (`seam.leak`
silent), and diffed beyond the guard band (shell > 10) against a
whole-spec fuse of the same two parts also relaxed to geo — measured
max |Δbond| 0.0001 Å, max |Δangle| 0.007° (thresholds 0.002 Å / 0.15°),
comfortably inside. Seam radii/thresholds stay stick-rung numbers for
mixed/unmeasured rim types (unchanged from slice 1). Slice 3 built the same day
(`place_graph`, see the seed-placement paragraph above; a `seam.cycle` finding reports the residual).

## Step 5 join: prod dogfood 2026-09-29, and what it left open

First real exercise of the `join` op in production (scratch design
`hexfold-join-dogfood`, deletable). The op works: plain fuse (120 atoms,
rings `{6:10}`, `seam.strain` rms |dl| 0.0136 Å), a chained join onto a
composite with `k="fit"` (180 atoms, recursive `_rebuild_block` clean),
and the 30° grain-boundary adapter (264 atoms, rings `{5:6, 7:6}`, 11 fit
alternatives). Five findings, four of which the unit tests could not see;
the two real defects only appeared by chaining joins in an order no test
had tried, which is the argument for dogfooding each slice rather than
trusting a green suite.

Landed in `3cd52d01`: `seam.sigma` WARN (two parts built at different
bond lengths — measured, σ 1.75 joined to σ 1.42 was accepted, strain
roughly tripled and both sides leaked, but nothing named the cause;
`compose` uses `a.sigma` for both the fuse placement and the re-relax);
`join.lattice`'s absent-annotation branch now names the un-annotated port
and says regeneration mints it; `seam.leak` now names which of
`|dl|`/`|dtheta|` breached and prints the threshold beside it (it used to
print `0.0000 A` for the measure that did *not* trip, making a genuine
0.060°-vs-0.025° breach read as a false alarm).

**Uncommitted but green at the wind-down (32 passed** across
`tests/hexfold/test_join.py`, `tests/test_se_join.py`,
`tests/test_se_join_geo.py`, `tests/test_hexfold_import_boundary.py`;
ruff and mypy clean**), awaiting the user's own `/qland`:**
- `JOINERS` re-keyed on a **sorted lattice pair**, `("sp2-hex","sp2-hex")`
  today, looked up via `tuple(sorted((a_lattice, b_lattice)))`. The old
  `a_lattice != b_lattice` ERROR sat in the dispatch path and thereby
  hard-coded "a join happens within one lattice", making heterojunctions
  (sp² onto sp³, a future DNA joiner) inexpressible by construction. A
  mismatch now falls through to "no joiner registered", naming both
  endpoints, both lattices and the registered pairs. Deliberately NOT
  built: plugin discovery, entry points, capability negotiation, any
  second joiner. The registry picks the *implementation*; the
  implementation validates the *physics* (that is `seam.sigma`'s job) —
  do not push element/σ into the key.
- `join.part_addressed` ERROR replacing the `join.reparented` WARN that
  `3cd52d01` shipped the same morning. A block already claimed as a part
  of a composite has its free rim exposed as that composite's own port,
  so addressing the part directly was the same rim under a second name —
  an **addressing** defect, not an ownership one. The error redirects to
  the correct address (full accumulated prefix several joins deep, e.g.
  `chain3.composite_tube_a_in`). Discriminator is "parent is a join
  composite AND names this block in its parts", never "has a parent" —
  a block under an ordinary layout parent must stay joinable, and there
  is a regression test for exactly that.

Open, needing a decision rather than work:
1. **`join.reparented` as an INFO on the ordinary-parent path only.**
   Joining a block authored under a layout parent (`add_block(parent=…)`)
   silently moves it into the composite. Not a correctness problem — the
   layout parent makes no claim on it — but it discards authored intent,
   and `join.pose_dropped` INFO is the precedent for reporting exactly
   that class of thing. Not built; the user has not asked for it.
2. **Heterojunction joiners** themselves, and **stable port identities**.
   Composite port names concatenate on every join, so depth 3 already
   reads `composite_tube_a_in` and deeper is unusable. se already solved
   this for blocks with uids (`#41`, addressable as `'#41.bore'`); ports
   want the same, with the concatenated name as a display label. Avoid
   making the concatenated name load-bearing in the meantime.
3. **`spec.md` §28 lists `seam.terminated` as a §13 finding, but it has
   never had a §13 row.** Pre-existing gap, cheap to close next time §13
   is touched (step 6 slice 3 will be).
4. ~~The prod scratch design `hexfold-join-dogfood` still holds the broken
   state.~~ **Done 2026-09-29: design retired (ref 456184, 10 blocks),
   nothing else referenced it.** The broken state was NOT "`chain3`
   missing a part its build record claims", as this item first recorded
   it — checked against the rows before deleting, and `chain3`'s own
   record is self-consistent (parts `composite`/`tube_b_out` +
   `tube_c`/`in`, 180 atoms = 120 + 60). The actual defect is the mirror
   image: **`tube_c` is claimed as a part by two composites**, `chain3`
   (port `in`) and `mixed_sigma` (port `out`, 120 atoms). A block can be
   a part of only one composite — once `chain3` consumed it, its
   remaining free rim was `chain3`'s own port, so the later join
   addressing `tube_c` directly is exactly the `join.part_addressed`
   ERROR landed above. That the landed fix's own motivating case is the
   state left in prod is the confirmation the fix targets the right
   defect; double-ownership is unreachable now.

Gripes: 456201 (pre-annotation blocks un-joinable — RULED: regeneration
is the remedy, no new write path), 456202 (`payload.word` does not
distinguish rim families — NOT a bug, `rim_word` takes `abs(turn)` and
hex rims turn ±60° everywhere, so `z12` and `a12` rims both read `z24`;
`spec.md` has an erratum and the skill now says read `type`), 456203,
456212, 456213 (all three fixed above).

## Step 6 slice 1: `measure_environment`'s seam radius does not converge (2026-09-29)

**Ruling for slice 2: measured rows must NOT override the pinned
wildcards.** The sanity check the slice-1 hand-off asked for was run
before writing any of slice 2, and it fails: `seam_radius` is not a
property of `EnvKey`. It grows monotonically with the *length of the
measurement tube*, which `EnvKey` does not record, on both rungs.

`measure_environment((5,0), rung=…, length=L)`, all other arguments at
their defaults:

| L | depth | stick radius | geo radius |
|---|---|---|---|
| 6 (the `MEASURE_LEN` default) | 22 | 4 | 8 |
| 8 | 30 | 14 | 14 |
| 10 | 38 | 22 | 26 |
| 12 | 46 | 30 | 34 |
| 13 | 50 | 34 | — |

Armchair is no better, and not even monotone: `(5,5)` geo reports 3 at
`len=4` (`coverage="lower-bound"`), **0** at `len=6` and 4 at `len=8`,
against a pinned `SEAM_RADIUS["a"] = 2`.

So the two numbers that looked like a calibration disagreement —
stick zigzag measuring 4 against a pinned 8, geo measuring exactly 8 —
are both artefacts of `MEASURE_LEN["z"] = 6`. Neither is a measurement
of a decay length. Had the default been 10, the same code would have
reported 22 and 26 with `coverage="full"` and the same confidence.

**Mechanism: the free relax does not converge, and the residual is read
as un-decayed seam signal.** The stick rung is fixed-iteration gradient
descent with no convergence check at all (its own docstring says so).
The geo rung has one, and it starts *failing* at exactly the lengths
where the radius runs away: `relax_graph(iters=4000, tol=1e-4)` — the
settings `tests/test_hexfold_seam_decay.py` pins — reports
`trace.converged is False` for `(5,0)` at `len=10` and `len=12`, and
converges at 6 and 8.

The stick `len=10` per-shell profile shows it directly: `max_disp` falls
from 0.268 Å at shell 0 to 0.0056 Å at shell 16, and then *rises again*,
monotonically, to 0.077 Å at shell 38 — the far, **unfused** end of the
tube moves an order of magnitude more than the shells just past the
seam. No physical seam decay produces that; it is the unconverged
interior drifting, redistributed by the Kabsch alignment. The scan then
walks outward looking for a shell past which everything is quiet, and
finds one only near the far rim.

The 3x noise multiplier is a red herring. On the geo rung the threshold
is pinned at the `_MEASURE_FLOOR` (0.002 Å / 0.15°) for `len` 6 and 8 —
the multiplier does not bind at all — and the radius still moves 8 → 14.

### What slice 2 should do instead

1. **Keep the pinned wildcards authoritative.** Store measured rows,
   report them (`seam.radius.narrowed` already exists), but do not let
   `resolve_edge` prefer a measured row over the pinned one until the
   measurement is trustworthy. The DB store, the migration and the
   first-use warm-up are all still worth building; only the override is
   held.
2. If the override is wanted later, `EnvKey` needs the measurement
   extent in it (and therefore in its hash), so two rows measured at
   different lengths cannot silently compete for the same key — today
   they would, and whichever tube happened to warm a shared prod cache
   would set the guard band for every subsequent join.
3. `measure_environment` should refuse rather than report: assert the
   relaxer converged (the geo adapter already has `trace.converged` and
   the seam-decay test already asserts it — the library function
   silently does not), and reject a profile whose `max_disp` is not
   monotonically non-increasing past the seam, which is the cheap,
   direct test for this failure.

Until then the four `seam_radius` numbers pinned by `==` in
`tests/hexfold/test_catalogue.py` and `tests/test_hexfold_seam_decay.py`
are pinning the default tube length, not a physical quantity. They are
still worth pinning — they make a change to this code visible — but no
document should quote them as decay lengths.

## Step 5 join open item 1: ruled (2026-09-29)

`join.reparented` is back as an **INFO**, narrowed to the ordinary-parent
path, per the user's ruling. Scope is sharper than the open item stated:
a join discards `b`'s authored parent only. `a`'s survives — the
composite is minted with `parent = old_a_parent` (`_hexfold_join`'s
`add_op`), so `a`'s layout intent is carried forward, while `b`'s old
parent is overwritten and recorded nowhere. The INFO therefore fires on
the `b` side alone, with `data.block`/`data.old_parent`; the
two-composite case stays the `join.part_addressed` ERROR. `spec.md` §13
gained rows for `join.reparented`, `join.pose_dropped` and
`join.part_addressed`, none of which had one (the same gap open item 3
records for `seam.terminated`, which is still open).

## Step 6 slice 2: built, with two things deliberately left undone (2026-09-29)

The DB store is in: `se_hexfold_catalogue` (migration `precis_se/0016`,
no `ref_id` — an environment row is not a property of a design) and
`precis_se.atomic.catalogue`, with `hexfold.join.compose` now reading it
on the live join path in `precis_se.atomic.join`. That read is
behaviour-neutral by construction today, which is the point: `seed_rows`
restates `join.SEAM_RADIUS`/`_LEAK_THRESH`, and the store withholds
`source="measured"` rows from `resolve_edge`, so every lookup returns the
number the module constant would have returned. The wiring exists so
that trusting a row later is a flag, not a refactor.

**The replay path in `_rebuild_block` gets no catalogue, and must not.**
A replay's job is to reproduce a recorded join exactly; the catalogue is
mutable shared state, so consulting it there would make one design's
warm-up raise `join.stale` on unrelated designs that have not changed.

Two things are open:

1. **The automatic first-use warm-up is held.** The slice-1 ruling kept
   it, but it does not survive its own consequence: under the ruling a
   measured row cannot influence a join, so warming one on first use
   would spend a full build-and-relax per new environment to produce a
   row nothing reads. It exists as `catalogue.warm_edge(...)`, explicit
   and opt-in, so the measurements gripe 456641 needs can still be taken.
   Wire it to every join only once a measured row can be trusted.
2. **`trust_measured=True` is the flip, and gripe 456641's three fixes
   are its precondition** — assert the relaxer converged, reject a
   non-monotone `max_disp` profile, put the measurement extent into
   `EnvKey`. Nothing in the product sets the flag today; one test pins
   both sides of it.

**Erratum filed against `spec.md` §26** (written into the section): it
describes a content-hash *build* cache, `hexfold_cache(key,
format_version, generator, authored_json, generated_json, …)`, keyed by
what was authored — and that table is still unbuilt. The environment-keyed
catalogue of §25.3 is a different store with a different key, and slice 2
built that one. They share the `get`/`put` protocol shape and nothing
else. Do not widen either into the other.
