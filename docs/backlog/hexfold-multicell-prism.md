---
status: draft
pillar: 3d-design
title: hexfold multi-cell prisms with H-columns — 2-wall folds with an H side, k3-sp3-z prism seams, honeycomb cross-sections, interior hydrogenation
prio: medium
model: opus
---

# hexfold: multi-cell prisms with H-columns

Reto, 2026-10-22: "spec the work, we want these anyways, what will all the
work and skills be?" Evidence: memory me503663 (GFN2 results), se design
`dogfood-prism-variants-20261021-chat99` (UFF-relaxed blocks). Prototype:
hexfold repo, local branch `proto/multicell-prism` (commit `af09a8d`),
`prototypes/multicell-prism/` — `build.py` is the de-facto reference
builder; `relax.py`/`strain.py` (RDKit UFF), `xtb_relax.py`/`xtb_refs.py`/
`xtb_analyze.py` (tblite GFN2, periodic), `analyze.py` (fold wedge and H
side). Paths in the scripts are hard-coded to `/tmp/hp99`.

**Where the code lands.** The generator lives in this repo:
`src/hexfold/` is the authoritative package (the standalone hexfold repo's
README says development moved here on 2026-09-16; that repo keeps only the
package name, the citation record and now the prototype branch). Every
slice below lands in precis-mcp: the construction in `src/hexfold/`, the
scene feature in `src/precis_se/atomic/generators/`, the docs in
`src/precis/data/skills/`.

## Terms

- **wall strip** — a flat, zigzag-edged graphene strip of `rows` zigzag
  chains running along the prism axis; one per polygon side.
- **seam** — the line along the axis where wall strips meet. A **k3 seam**
  joins three strips at one atom column (here always the `k3-sp3-z` dimer
  chain). A **fold** (k2) is two strips fused rim to rim with no seam
  atom: an all-six-ring sheet bent 60°.
- **fin / rib** — the third sheet of a k3 seam, outward (`+`) or inward
  (`-`).
- **cell** — one hexagon of the cross-section tiling. A **merge group** is
  a set of cells whose shared walls are removed, forming one lumen. **Core
  cell** = the central lumen; **outer cell** = a cell seated against it.
- **H-column** — the column of C–H bonds along a hydrogenated fold atom
  column. A hydrogenated fold carries two (both fold atom columns go sp3;
  one column alone leaves a radical). H-columns point into a lumen only at
  a re-entrant fold.
- **bundle** — a core lumen with outer cells seated in its notches.

Glossary entries for H-column, fold, wall strip and merge group are part
of slice 1.

## Problem

None of the six chat-99 targets is expressible in hexfold today; they were
built by a 200-line scratch builder outside the package, so they carry no
hexfold findings, no atom paths, no catalogue rows and no regenerate path.
Six gaps:

1. **No 2-wall fold.** `prism-k3-120-z` makes every vertex a k3 seam with
   a fin or rib. A bare or hydrogenated fold (two walls, no third sheet,
   H on a chosen side) has no token.
2. **Prism seams mint the sp2 equal-120 row**, which free GFN2 dimerises
   into the `k3-sp3-z` chain (relax campaign 2026-10-13 in
   [hexfold-toolkit](threads/hexfold-toolkit.md#relax-campaign-2026-10-13)).
   `chan-k3-sp3-z` already authors the dimer chain for channels
   (`hexfold.channel`, `paired=True`); prisms do not.
3. **No centre junction.** Ribs end in open rims; nothing joins three rib
   tips in one k3 seam, and rib length is independent of wall length.
4. **No multi-cell cross-section.** A prism is one polygon. The tri-tube
   and bundles are subsets of the hexagonal tiling with merge groups.
5. **No interior-seam hydrogenation.** H enters a hexfold net only as
   open-rim termination (`_block_from_net`, `terminate_mode` in
   `precis_se/atomic/generators/hexfold_spec.py`). The decoration primitive
   that would put H on interior sites is specced in
   [hexfold-sp3-isolation-band](hexfold-sp3-isolation-band.md) and unbuilt.
6. **xtb relaxes cannot be dispatched from the session MCP host.**
   `structure` `_dispatch_relax` (`precis/handlers/structure.py`) refuses
   "no compute node configured" when neither `PRECIS_DFT_NODE` nor
   `PRECIS_MLIP_NODES` is set. Another agent is investigating the route
   (2026-10-22). **Open dependency; this spec does not own it.** Until it
   lands, GFN2 checks run locally with the prototype's tblite scripts.

## What the GFN2 results rule (me503663)

GFN2-xTB, local tblite, periodic 4-period cell, 2-chain walls, Γ only,
Te 1000 K, fmax 0.05 eV/Å; energies eV/C vs graphene + H2; references
(8,8) CNT +0.060, (6,6) +0.110.

| target (se block) | outline | GFN2 | rule it sets |
|---|---|---|---|
| A0 `prism_hcol_forced` | hexagon, 6 H folds, H into the lumen | +0.389, half the H leave as H2 | H on the convex side of a fold is not offered |
| A1 `prism_hfold_out` | hexagon, 6 H folds, H outward | +0.075, holds | H on the reflex side is the only H side |
| A2 `prism_hcol` | 12 walls `+++-+++-+++-`, 3 H notch folds (6 inward H-columns), 9 bare convex folds | +0.160, holds | inward H-columns live at re-entrant folds |
| B `tri_tube` | 3 hex cells, 4 `k3-sp3-z` seams, 9 bare folds | +0.119, holds | multi-cell with k3-sp3-z seams is viable |
| C3 `bundle3` | A2 core + 3 outer cells in the notches | ruptures: all 12 notch fold bonds break | H fold with a cell on its convex side is unverified at 2-chain walls |
| C6 `bundle6` | 7-hex core + 6 outer cells | UFF only | — |

Wider (4-chain) walls for the bundles are being tested separately; slice 5's
bundle warning text is updated from that result.

## Format changes

**Scene features, not `.hx` grammar.** Like the Y, the fins and the
prism, the construction stays private and is reached through
`hexfold_scene` feature types. `src/hexfold/spec.md` moves from format
**0.2 → 0.3 (draft)**: a new subsection under §11 ("Folds and interior
decoration `[spec 0.3]`") and a new section "Cross-sections on the
hexagonal tiling `[spec 0.3]`", plus §30 decision-log entries for the H-side
rule and the stability-claim policy. Existing feature types keep their
semantics; new semantics arrive as new type names or new tokens, so every
existing design regenerates with the same atoms and coordinates.

**F1 — fold tokens in `seams` (both prism types).**

| token | vertex | turn | H |
|---|---|---|---|
| `+` | k3 seam, outward fin | +60° | — |
| `-` / `−` | k3 seam, inward rib | −60° | — |
| `c` | bare fold, convex | +60° | none |
| `r` | bare fold, re-entrant | −60° | none |
| `C` | hydrogenated fold, convex | +60° | 2 H-columns, reflex side (outward) |
| `R` | hydrogenated fold, re-entrant | −60° | 2 H-columns, reflex side (into the lumen) |

Closure law unchanged: (count of +60 tokens) − (count of −60 tokens) = 6,
side vectors sum to zero, polygon simple. The H side is always the fold's
reflex side; there is no token for convex-side H (A0). Fold seed: each
wall's rim sits σ/(2·sin 60°) from the vertex line and the two rims bond
directly (prototype `build.py`, `vinfo` k2 branch). Examples: A1 =
`CCCCCC`, A2 = `cccRcccRcccR`.

**F2 — `prism-k3-sp3-z`.** New feature type, same keys as
`prism-k3-120-z` (`rows`, `periods`, `strip_rows`, `seams`, `pores`).
Every `+`/`-` vertex is minted as the `k3-sp3-z` dimer chain (seam atoms
paired along the axis, `hexfold.channel.DIMER_A` = 1.60 Å, registration
from `sp3_seam._place_k3z`). `periods` must be even (`fit.unsolvable`
otherwise). `prism-k3-120-z` stays as the sp2 preview row and gains WARN
`seam.sp2_preview` pointing at the relax campaign.

**F3 — `cells-k3-sp3-z`.** New feature type for multi-cell
cross-sections:

```json
{"op":"generate","generator":"hexfold_scene","name":"t",
 "params":{"features":[{"name":"t","type":"cells-k3-sp3-z",
   "rows":4,"periods":6,
   "cells":[[0,0,"a"],[1,0,"b"],[1,-1,"c"]],
   "fold_h":{"core":"reflex"}}]}}
```

- `cells`: `[q, r, group]` axial coordinates on the flat-top hexagonal
  tiling (prototype `cell_center`). Walls between two cells of the same
  group are removed.
- `rows` (even, wall zigzag chains), `periods` (even). Side length is
  derived from `rows` (prototype `side_L`); every wall is the same length.
- Vertex rule from the kept-wall degree: 3 → `k3-sp3-z` seam, 2 → fold.
- `fold_h`: `{group: "reflex"}` hydrogenates every fold whose reflex side
  lies in that group's lumen; `"outside"` names the exterior. Default
  `{}` (all folds bare).
- No fins, ribs or pores in v1.

**F4 — centre rib junction (sugar).** `prism-k3-sp3-z` gains
`"ribs": "meet"`: valid only when the outline has exactly three `-`
vertices at 120° whose ribs reach the centroid; rib length is then the
wall length (`strip_rows` must be absent), and the three tips join in one
`k3-sp3-z` seam. Canonical form is the equivalent `cells-k3-sp3-z` input,
so there is one builder. Example: tri-tube = `ccc-ccc-ccc-` + `ribs:"meet"`
≡ `cells:[[0,0,"a"],[1,0,"b"],[1,-1,"c"]]`.

**F5 — interior hydrogenation record.** Blocks gain
`topology.hydrogenated: {element, count, sites, face}` beside the existing
`topology.terminated`, and the structure tag `hydrogenated:interior`. The
two records stay separate: `terminated` is edge capping, `hydrogenated`
is authored chemistry.

## Slices, in build order

Every slice lands in precis-mcp and ships its skill edits in the same
commit (§Skills). Golden fixtures live in `tests/hexfold/data/multicell/`.

### Slice 1 — golden census from the prototype

Generate, from the prototype `build.py` (`periodic=True`), a census per
target at (R, P) = (2, 4) and (4, 6): C, H, walls, k3 seams, folds,
hydrogenated folds, role counts, degree histogram, ring census. Commit the
JSON plus the one command that regenerated it; the prototype itself stays
on the hexfold branch. Add the glossary entries (§Terms).

Reference counts (R=4, P=6): A0/A1 C288 H72; A2 C576 H36 (12 walls, 3 H
folds); tri-tube C744 H0 (15 walls, 4 k3); bundle3 C1188 H36 (24 walls,
6 k3, 15 folds); bundle6 C2088 H72 (42 walls, 12 k3). At (2, 4): A0/A1
C96 H48; A2 C192 H24; tri-tube C256; bundle3 C408 H24; bundle6 C720 H48.
Closed forms: C = 2·R·P·walls + P·k3; H = 2·P·(hydrogenated folds).

Acceptance: fixtures load; a test asserts the closed forms on every
fixture.

### Slice 2 — interior-site hydrogenation primitive (gap 5)

A `hexfold.build` decoration: given carbon sites and a face direction per
site, relabel `hyb="sp3"`, add one H at 1.09 Å, record the decoration on
the net. `_block_from_net` emits F5 and keeps `_SP2_BOND_ORDER` off C–H.
`check`: a decorated carbon has valence 4; WARN `geom.sp3.mismatch` as the
isolation-band item defines it. This is the site-decoration core
[hexfold-sp3-isolation-band](hexfold-sp3-isolation-band.md) needs; that
item then adds only band site selection (distance from a closed path)
and chair face assignment.

Acceptance: decorating one zigzag column of a flat sheet gives every
decorated C four bonds and one H; `counting_residual` unchanged;
`topology.hydrogenated.count` equals the sites passed; open-rim
`terminated` records are unchanged for the same block.

### Slice 3 — folds with an H side in the prism (gap 1)

F1 in `hexfold.prism` and `prism120.py`, built on slice 2.

Validator rules: `prism.fold.h_side` (ERROR if any fold H points to the
convex side after the stick pass: H vector · reflex bisector ≤ 0);
`prism.fold.wedge` (INFO, per-fold wedge angle — prototype `analyze.py`);
`prism.hcol.clash` (ERROR, H···H across a notch < 1.8 Å); `prism.closure`
counts fold tokens in the turn law.

Acceptance:
- `CCCCCC` and `cccRcccRcccR` at (2, 4) and (4, 6) match the slice-1
  census.
- Every existing `+`/`-` prism test passes unchanged (byte-identical
  coordinates for unchanged inputs).
- H on the reflex side for 100% of H in seed and after stick.
- GFN2 (local tblite, the prototype's settings, periodic (2, 4)): no C–C
  bond broken or formed; energy within 0.01 eV/C of A1 +0.075 and A2
  +0.160. Recorded per §Stability claims; not in CI.

### Slice 4 — prism seams mint `k3-sp3-z` (gap 2)

F2, reusing `sp3_seam._place_k3z` and the `channel` registration (the
outward-strip registration sits half a sigma outside the wall).

Acceptance:
- `++++++` and `+++-+++-+++-` build; every seam atom has degree 4 and
  exactly one same-seam partner at 1.60 Å (seed); each seam's ring census
  equals `straight_k3z`'s; `prism.rib.clearance` still enforced.
- Odd `periods` refused `fit.unsolvable` before build.
- `prism-k3-120-z` outputs unchanged apart from the new
  `seam.sp2_preview` WARN.
- GFN2 (local, periodic `++++++`, smallest even cell): dimer pairs stay at
  1.55–1.65 Å, no bond change.

### Slice 5 — multi-cell cross-sections (gap 4)

F3 as a new `hexfold.cells` module (port of the prototype `build()` onto
`Net`/`Atom`/`AtomPath`, with fold minting from slice 3 and seam minting
from slice 4) and a `precis_se` generator module routed by
`hexfold_scene`.

Validator rules:
- `cells.connected` (ERROR): the cell set is one edge-connected component.
- `cells.group.contiguous` (ERROR): each merge group is edge-connected.
- `cells.hole` (ERROR in v1): a non-cell region fully enclosed by walls.
- `cells.fold.enclosed` (WARN): a hydrogenated fold with a cell, not the
  exterior, on its convex side — "bundle notch fold: ruptured in GFN2 at
  2-chain walls (me503663); no stability claim". Text revised when the
  4-chain wall result lands.
- `cells.fold_h.group` (ERROR): `fold_h` names a group not in `cells`.

Acceptance:
- tri-tube, bundle3, bundle6 match the slice-1 census at both (R, P).
- Cross-grammar equivalence: `cells` = the three tri cells in one group
  with `fold_h:{core:"reflex"}` produces the same graph and census as
  prism-k3-sp3-z `cccRcccRcccR` (A2).
- `cells.fold.enclosed` fires on bundle3/bundle6 and not on A2 or tri-tube.
- GFN2 (local, periodic (2, 4)): tri-tube holds within 0.01 eV/C of
  +0.119. The bundle3 rupture rerun is a manual campaign item (≈1.5 h on
  the Mac), not CI.

### Slice 6 — centre rib junction (gap 3)

F4 on top of slice 5: validate the three-rib condition (`prism.rib.meet`,
ERROR naming which condition failed), rewrite to the cells form, build.

Acceptance: `ccc-ccc-ccc-` + `ribs:"meet"` yields the slice-5 tri-tube
graph; `strip_rows` alongside `ribs:"meet"` is refused; `canon` emits the
cells form.

### Slice 7 — GFN2 from the session MCP (gap 6, blocked)

Blocked on the xtb dispatch route (open dependency, owned elsewhere).
When it lands: rerun A1, A2, tri-tube and the 4-chain bundle3 through
`relax` with `fidelity='xtb'` on the built blocks, so the GFN2 result is
stored on the structure; replace the local-run records.

Acceptance: each target's structure has an xtb-rung relax record; the
energies agree with the local runs within 0.01 eV/C.

## Stability claims

No block description, finding, catalogue row or skill example says a
geometry "holds", "is stable" or "is a minimum" without a recorded GFN2
result. Recorded means: an xtb-rung relax stored on the structure (after
slice 7), or until then a memory or finding linked to the block that
states method, cell, k-points, Te, fmax, energy vs reference and the bond
change census. UFF and stick are previews and are described as such.
Generator findings never claim stability; `cells.fold.enclosed` and
`seam.sp2_preview` are the cases where GFN2 has contradicted or not yet
tested the construction. The skills state this rule (§Skills).

The current se design blocks state UFF results only and claim no
stability; me503663 holds the GFN2 record.

## Skills

| skill (`src/precis/data/skills/`) | change | slice |
|---|---|---|
| `precis-hexfold-seams-help.md` | prism section: fold tokens table, H-side rule, new validator codes; new section `prism-k3-sp3-z`; new section `cells-k3-sp3-z` with example; `ribs:"meet"`; front-matter `summary`/`answers` gain the new types | 3, 4, 5, 6 |
| `precis-hexfold-help.md` | the seams pointer paragraph names the new types; the H-termination section distinguishes `hydrogenated` from `terminated`; a short stability-claim rule | 2, 3, 5 |
| `precis-se-atomic-help.md` | the terminate bullet gains `topology.hydrogenated` and tag `hydrogenated:interior` | 2 |
| `precis-structure-help.md`, `precis-job-compute-help.md` | xtb dispatch from the session host — owned by the dispatch work, not this spec | 7 |

No new skill: `precis-hexfold-seams-help` is about 1,700 words and absorbs
two sections. Split `cells-k3-sp3-z` into its own skill only if that file
passes about 2,500 words.

## Skills and work

| slice | gap | lands in | size | agent |
|---|---|---|---|---|
| 1 golden census + glossary | — | precis-mcp `tests/hexfold/data/`, `docs/glossary.md` | S, part of one build | coder (Sonnet) |
| 2 interior hydrogenation | 5 | precis-mcp `src/hexfold/build.py`, `check.py`; `precis_se` `hexfold_spec.py` | M, 1 build | coder (Sonnet); design-review |
| 3 folds with H side | 1 | precis-mcp `src/hexfold/prism.py`; `precis_se` `prism120.py` | M, 1 build | coder (Sonnet); design-review |
| 4 prism `k3-sp3-z` | 2 | precis-mcp `src/hexfold/prism.py`, `sp3_seam.py`; new generator entry | M, 1 build | coder (Sonnet) |
| 5 multi-cell | 4 | precis-mcp new `src/hexfold/cells.py`; new `precis_se` generator | L, 2 builds | coder-chain (Sonnet); Opus design-review |
| 6 centre rib junction | 3 | precis-mcp `src/hexfold/prism.py`, `canon.py` | S, part of one build | coder (Sonnet) |
| 7 GFN2 via MCP | 6 | precis-mcp (route owned elsewhere); prod structures | campaign, after dependency | coder or cmd-runner |
| docs per slice | — | the skills above, `src/hexfold/spec.md` 0.3 | with each slice | documenter (Sonnet) |
| GFN2 local checks (3, 4, 5) | — | Mac-local tblite, prototype scripts | ~0.5–1.5 h compute each | cmd-runner |

Total: about 6 builds plus the blocked slice 7.

## Not in scope

- The xtb dispatch route itself (gap 6).
- Fins, ribs and pores on multi-cell walls; unequal wall lengths; chiral
  or armchair-axis walls.
- Fluorinated folds (the isolation-band item carries `terminator=F`).
- Public `.hx` text grammar for prisms or cells.

## Open questions for Reto

1. Default seam row: should `prism-k3-120-z` be retired in favour of
   `prism-k3-sp3-z`, or kept as the sp2 preview row with a WARN (this
   spec's choice)?
2. Is `ribs:"meet"` (slice 6) worth having, or is the `cells` form enough?
3. Bundles: until the 4-chain wall result, is the WARN enough, or should
   `cells.fold.enclosed` be an ERROR at `rows` < 4?
4. Should cells carry fins or pores in v2, or is the honeycomb wall-only?
