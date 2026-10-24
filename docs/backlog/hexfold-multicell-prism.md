---
status: draft
pillar: 3d-design
title: hexfold multi-cell prisms over shared sheet primitives — one sheetnet layer (strip, pore, place, junction, assemble, decorate), existing tubes re-expressed as layouts, then folds with an H side, k3-sp3-z seams and honeycomb cells as parameters
prio: medium
model: opus
---

# hexfold: multi-cell prisms over shared sheet primitives

Reto, 2026-10-22: "spec the work, we want these anyways, what will all the
work and skills be?" and, as the first principle for this item: "keep
types DRY so the code stays DRY — is a cell wall not just a graphene
sheet?" Evidence: memory me503663 (GFN2 results), se design
`dogfood-prism-variants-20261021-chat99` (UFF-relaxed blocks). Prototype:
hexfold repo, local branch `proto/multicell-prism` (commit `af09a8d`),
`prototypes/multicell-prism/` — `build.py` is the reference for counts and
geometry, not for code structure; `relax.py`/`strain.py` (RDKit UFF),
`xtb_relax.py`/`xtb_refs.py`/`xtb_analyze.py` (tblite GFN2, periodic),
`analyze.py` (fold wedge and H side). Paths in the scripts are hard-coded
to `/tmp/hp99`.

**Where the code lands.** `src/hexfold/` in this repo is the authoritative
package (the standalone hexfold repo's README says development moved here
on 2026-09-16; that repo keeps the package name, the citation record and
the prototype branch). Construction lands in `src/hexfold/`, scene features
in `src/precis_se/atomic/generators/`, docs in `src/precis/data/skills/`.

## The DRY rule

A wall, a fin, a rib, a rail arc and a cell wall are all one thing: a
graphene strip, possibly pored, placed in a frame, joined to other strips
at junctions. hexfold gets **one** primitive layer for that, and every tube
builder becomes a layout over it.

**Rule:** v2 vocabulary — pores in cells, unequal walls, fins and ribs on
cell walls, other decorations, axial patterns — is added only as a
parameter of a primitive below or of a layout, never as a new builder
module. A spec or PR that adds a strip, pore, seam, fold, dimer or
assembly loop outside `sheetnet` is refused in review.

## Problem

None of the six chat-99 targets is expressible in hexfold today; they were
built by a 200-line scratch builder outside the package, so they carry no
hexfold findings, no atom paths, no catalogue rows and no regenerate path.
Adding them the obvious way (a new `cells.py` porting the prototype
`build()`, a fold in `prism.py`, a third dimer placement) would add a third
pore and assembly path, a second fold and a third dimer chain. A read-only
audit (2026-10-22) of the existing builders:

**Shared today:** the sheet (`hexfold.y_junction._sheet`), k3 seam bonds
(`hexfold.join._seam_k3`), H termination
(`precis_se.atomic.generators.hexfold_spec._block_from_net`).

**Duplicated:**

| concern | copies |
|---|---|
| strip builder | `fin._strip`, `sp3_seam._armchair_sheet` |
| top-rim lookup | `fin120._top_dangling`, `railtube._top_segment` |
| pores | `railtube._pore_cells` ≈ `prism._pore_cells`, plus inline ring/rim/remap loops in `railtube.rail_tube` and `prism.prism_tube` (`channel` has none) |
| tube assembly (offsets, bonds, rings, `paths()`, pinned zone, regions, `Net`) | ~5×: `prism_tube`, `rail_tube`, `channel_junction`, `fin120.seam_tube`, `sp3_seam._seam_net`/`_place_*` |
| dimer chain at 1.60 Å | `sp3_seam._place_k3z`; inline in `channel.py` (`DIMER_A`, paired loop, `hdx`) |
| registration geometry | `fin120.wall_radius`, `railtube.rail_radius`, `prism._polygon` |
| inward-clearance gate | 4 places |
| strip count | `fin._count`, `fin120._count` |
| k2 rim-to-rim fuse | only inline in `fin120.seam_tube` (`fuse_pairs`/`fuse_rings`) |
| SE adapter (`_normalize`, tether, `stick_relax_pinned`, findings, topology) | `generators/{prism120,rail120,channel,fin,fin120,y_junction,sp3_seam}.py`; `prism120` and `rail120` nearly identical (`_site_ordinals`/`_site_list` copies) |

In scope: ~2.3k lines of construction and ~2.1k lines of SE adapters.
Expected net change after slice 1: −1.2k to −1.5k lines.

Remaining gaps for the chat-99 targets, each closed by a parameter, not a
builder:

1. **No 2-wall fold.** Every prism vertex is a k3 seam with a fin or rib;
   a bare or hydrogenated fold (two walls, no third sheet, H on a chosen
   side) has no token. → `junction(k=2)` + fold tokens (slice 3).
2. **Prism seams mint the sp2 equal-120 row**, which free GFN2 dimerises
   into the `k3-sp3-z` chain (relax campaign 2026-10-13 in
   [hexfold-toolkit](threads/hexfold-toolkit.md#relax-campaign-2026-10-13)).
   `chan-k3-sp3-z` authors the dimer chain for channels; prisms do not.
   → `junction(k=3, flavour="sp3-z")` (slice 3).
3. **No multi-cell cross-section.** The tri-tube and bundles are subsets
   of the hexagonal tiling with merge groups. → tiling layout (slice 4).
4. **No interior-seam hydrogenation.** H enters a net only as open-rim
   termination (`_block_from_net`, `terminate_mode`). The decoration
   primitive is specced in
   [hexfold-sp3-isolation-band](hexfold-sp3-isolation-band.md) and unbuilt.
   → `decorate` (slice 2).
5. **xtb relaxes cannot be dispatched from the session MCP host.**
   `structure` `_dispatch_relax` (`precis/handlers/structure.py`) refuses
   "no compute node configured" when neither `PRECIS_DFT_NODE` nor
   `PRECIS_MLIP_NODES` is set. Another agent owns the route (2026-10-22).
   **Open dependency; this spec does not own it.** Until it lands, GFN2
   checks run locally with the prototype's tblite scripts.

## Primitive layer: `src/hexfold/sheetnet.py`

One module. Signatures are the contract; internals reuse the shared code
named above.

- **`strip(rows, periods, edge="zigzag"|"armchair")`** → block (atoms,
  bonds, rings in strip-local coordinates) + named rim segments. Replaces
  the `y_junction._sheet` call sites, `fin._strip`,
  `sp3_seam._armchair_sheet`, `fin120._top_dangling`,
  `railtube._top_segment` and both `_count`s.
- **`pore(strip, cells)`** → the strip with the listed ring cells removed,
  rims and ordinal remap updated. Replaces both `_pore_cells` and the
  inline loops in `rail_tube`/`prism_tube`; `channel` gains pores as a
  parameter.
- **`place(strip, frame)`** with `frame` = `flat` | `arc(radius, span)` |
  `half_plane(origin, normal, tilt)` → world coordinates. One
  registration function replaces `fin120.wall_radius`,
  `railtube.rail_radius` and `prism._polygon`.
- **`junction(rims, k, flavour, side=None)`** — k = 2: fold/fuse two rims
  directly (the `fin120.seam_tube` fuse, generalised; σ/(2·sin 60°) seed
  offset from the prototype `vinfo` k2 branch); k = 3: wraps
  `join._seam_k3`, `flavour="sp2"` (equal-120 row), `"sp3-z"` (the paired
  dimer chain at 1.60 Å — one implementation replacing `_place_k3z` and
  the inline `channel` loop) or `"sp3-a"`; k = 4: the `straight_k4` seam.
  `side` selects fin/rib direction and, for folds, the H side. Returns
  the bonds, seam atoms and the pinned zone. The inward-clearance gate
  lives here once.
- **`assemble(parts, junctions)`** → `Net` with offsets, bonds, rings,
  regions, `AtomPath`s and pinned zone. The only assembly loop.
- **`decorate(sites, face, element="H")`** — slice 2; the same primitive
  [se-modules-and-assemblies](se-modules-and-assemblies.md) exposes as
  its `decorate` op.
- Termination stays `_block_from_net`.

**Layouts.** A layout is a function from authored parameters to a
cross-section graph — vertices carrying junction specs, edges carrying
strip specs — fed to `assemble`. Three cross-section shapes cover every
builder: `polygon` (turn sequence: `prism`), `circle` (arcs on a radius:
`railtube`, `fin120`, `channel`), `tiling` (hex cells with merge groups:
`cells`). `y_junction.straight_y` and `sp3_seam.straight_*` are the
degenerate one-junction layouts. `fin.py` (`fin_tube`, a graft onto an
existing tube) stays separate and uses only `strip`.

**Byte identity.** Re-expressed builders keep atom ordinal order (strips,
then walls, then seams) and `AtomPath` names (`s{k}`, `w{k}`, `a{k}`,
`y{k}`), so every existing design regenerates with the same atoms and
coordinates. The slice-0 goldens enforce it.

**One SE adapter.** One shared module in `precis_se/atomic/generators/`
(name settles in the build): normalize, tether, `stick_relax_pinned`,
findings, topology, site list. Each generator module shrinks to its
parameter schema and a layout call; `prism120` and `rail120` share one
site-list function.

## Terms

- **strip** — a flat graphene ribbon of `rows` chains along the axis,
  zigzag- or armchair-edged; the one sheet type. A **wall**, **fin**,
  **rib**, **rail arc** and **cell wall** are strips in different frames.
- **junction** — where strip rims meet along the axis: k = 2 **fold** (two
  rims fused, no seam atom; an all-six-ring sheet bent 60°), k = 3 **seam**
  (three strips at one atom column; flavour `sp2`, `sp3-z`, `sp3-a`),
  k = 4.
- **layout** — a function from authored parameters to a cross-section
  graph of strips and junctions (`polygon`, `circle`, `tiling`).
- **fin / rib** — the third strip of a k3 seam, outward (`+`) or inward
  (`-`).
- **cell** — one hexagon of the cross-section tiling. A **merge group** is
  a set of cells whose shared walls are removed, forming one lumen. **Core
  cell** = the central lumen; **outer cell** = a cell seated against it.
- **H-column** — the column of C–H bonds along a hydrogenated fold atom
  column. A hydrogenated fold carries two (both fold atom columns go sp3;
  one column alone leaves a radical). H-columns point into a lumen only at
  a re-entrant fold.
- **bundle** — a core lumen with outer cells seated in its notches.

Glossary entries for strip, junction, layout, H-column, fold and merge
group land in slice 0.

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

Wider (4-chain) walls for the bundles are being tested separately; slice
4's bundle warning text is updated from that result.

## Format changes

**Scene features, not `.hx` grammar.** Construction stays private and is
reached through `hexfold_scene` feature types. `src/hexfold/spec.md` moves
from format **0.2 → 0.3 (draft)**: a subsection under §11 ("Folds and
interior decoration `[spec 0.3]`"), a section "Cross-sections on the
hexagonal tiling `[spec 0.3]`", and §30 decision-log entries for the DRY
rule, the H-side rule and the stability-claim policy. Existing feature
types keep their semantics; new semantics arrive as new type names or new
tokens, each mapped onto junction or layout parameters.

**F1 — fold tokens in `seams` (polygon layout).** Each token is a junction
spec.

| token | junction | turn | H |
|---|---|---|---|
| `+` | k3 seam, outward fin | +60° | — |
| `-` / `−` | k3 seam, inward rib | −60° | — |
| `c` | k2 fold, convex | +60° | none |
| `r` | k2 fold, re-entrant | −60° | none |
| `C` | k2 fold, convex | +60° | 2 H-columns, reflex side (outward) |
| `R` | k2 fold, re-entrant | −60° | 2 H-columns, reflex side (into the lumen) |

Closure law unchanged: (count of +60 tokens) − (count of −60 tokens) = 6,
side vectors sum to zero, polygon simple. The H side is always the fold's
reflex side; there is no token for convex-side H (A0). H-columns are
`decorate` calls on the fold atom columns. Examples: A1 = `CCCCCC`, A2 =
`cccRcccRcccR`.

**F2 — `prism-k3-sp3-z`.** Feature type name for the polygon layout with
`junction(k=3, flavour="sp3-z")` at every `+`/`-` vertex; same keys as
`prism-k3-120-z` (`rows`, `periods`, `strip_rows`, `seams`, `pores`).
`periods` must be even (`fit.unsolvable` otherwise). `prism-k3-120-z` is
**retired** (Reto 2026-10-22: "retire things that are broken"): its sp2
seams collapse in GFN2. New builds refuse with `seam.sp2_retired` naming
`prism-k3-sp3-z` and the token mapping; stored blocks keep their
coordinates and say so on regenerate. Its slice-0 golden is the
byte-identity check through slice 1 and is dropped with the refusal in
slice 3.

**F3 — `cells-k3-sp3-z`.** Feature type name for the tiling layout:

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
- `rows` (even, wall chains), `periods` (even). Side length is derived
  from `rows` (prototype `side_L`); every wall is the same length in v1.
- Junction spec from the kept-wall degree: 3 → k3 `sp3-z`, 2 → fold.
- `fold_h`: `{group: "reflex"}` hydrogenates every fold whose reflex side
  lies in that group's lumen; `"outside"` names the exterior. Default
  `{}` (all folds bare).
- No fins, ribs or pores in v1 (ruling 4: later, as parameters).

**F4 — interior hydrogenation record.** Blocks gain
`topology.hydrogenated: {element, count, sites, face}` beside
`topology.terminated`, and the structure tag `hydrogenated:interior`.
`terminated` is edge capping, `hydrogenated` is authored chemistry.

## Slices, in build order

Every slice lands in precis-mcp and ships its skill edits in the same
commit (§Skills). Goldens live in `tests/hexfold/data/goldens/` (SHA) and
`tests/hexfold/data/multicell/` (census).

### Slice 0 — goldens first

No coordinate goldens exist today (`tests/hexfold/data/` holds only
`foot_anchors.json`).

1. **Per-builder SHA goldens.** For every builder — `prism_tube` (`+`/`-`
   outlines, with and without pores), `rail_tube`, `channel_junction`
   (paired and unpaired), `fin120.seam_tube`, `y_junction.straight_y`,
   `sp3_seam.straight_k4`/`straight_k3z`/`straight_k3a`/`compose_sp3`,
   `fin.fin_tube` — and for each SE generator module's emitted block:
   SHA-256 of seed3 coordinates (packed float64), sorted bonds, sorted
   rings, `AtomPath` names and members, regions and pinned zone, at two
   sizes each. One JSON file plus the regenerating command.
2. **Prototype census.** From `build.py` (`periodic=True`), per chat-99
   target at (R, P) = (2, 4) and (4, 6): C, H, walls, k3 seams, folds,
   hydrogenated folds, role counts, degree histogram, ring census.
   Reference (4, 6): A0/A1 C288 H72; A2 C576 H36 (12 walls, 3 H folds);
   tri-tube C744 H0 (15 walls, 4 k3); bundle3 C1188 H36 (24 walls, 6 k3,
   15 folds); bundle6 C2088 H72 (42 walls, 12 k3). (2, 4): A0/A1 C96 H48;
   A2 C192 H24; tri-tube C256; bundle3 C408 H24; bundle6 C720 H48. Closed
   forms: C = 2·R·P·walls + P·k3; H = 2·P·(hydrogenated folds).
3. Glossary entries (§Terms).

Acceptance: the SHA test passes on main unchanged; census fixtures load;
a test asserts the closed forms on every census fixture.

### Slice 1 — `sheetnet` primitives; existing types as layouts

Byte-identical against slice 0 throughout; no new feature type, no new
token, no skill change. Three builds:

- **1a — primitives + prism and railtube as layouts.** `strip`, `pore`,
  `place`, `junction` (k2 fuse, k3 `sp2`, k3 `sp3-z`), `assemble` in
  `sheetnet.py`; `prism_tube` and `rail_tube` rewritten as layouts; both
  `_pore_cells` and the inline pore loops deleted.
- **1b — the rest of the builders.** `channel_junction`,
  `fin120.seam_tube`, `y_junction.straight_y`, `sp3_seam.straight_*`/
  `compose_sp3` as layouts; one dimer chain, one clearance gate, one
  registration function; `fin._strip`, `_armchair_sheet`, the two
  `_count`s and the two top-rim lookups deleted. `fin.fin_tube` moves onto
  `strip` only.
- **1c — one SE adapter.** Shared normalize/tether/relax/findings/
  topology/site list; the seven generator modules shrink to schema +
  layout call; `prism120`/`rail120` site-list copies merged.

Acceptance: every slice-0 SHA golden byte-identical; every existing
hexfold and generator test unchanged; the commit bodies record `wc -l`
over the in-scope files (target −1.2k to −1.5k lines across 1a–1c);
`grep` finds no strip, pore, seam, dimer or assembly loop outside
`sheetnet.py` and `fin.py`'s graft.

### Slice 2 — `decorate` (gap 4)

`sheetnet.decorate(sites, face, element="H")`: relabel `hyb="sp3"`, add
one H at 1.09 Å along `face`, record the decoration on the net.
`_block_from_net` emits F4 and keeps `_SP2_BOND_ORDER` off C–H. `check`:
a decorated carbon has valence 4; WARN `geom.sp3.mismatch` as the
isolation-band item defines it. This is the site-decoration core
[hexfold-sp3-isolation-band](hexfold-sp3-isolation-band.md) needs (that
item then adds band site selection and chair face assignment) and the
primitive behind the `decorate` op in
[se-modules-and-assemblies](se-modules-and-assemblies.md).

Acceptance: decorating one zigzag column of a flat strip gives every
decorated C four bonds and one H; `counting_residual` unchanged;
`topology.hydrogenated.count` equals the sites passed; `terminated`
records unchanged for the same block; slice-0 goldens unchanged.

### Slice 3 — fold tokens and `k3-sp3-z` as junction specs (gaps 1, 2)

F1 and F2 in the polygon layout: the token parser maps each token to a
`junction` spec (k2 fold with optional `decorate` on the reflex side, or
k3 `sp3-z`). No new builder.

Validator rules: `prism.fold.h_side` (ERROR if any fold H points to the
convex side after the stick pass: H vector · reflex bisector ≤ 0);
`prism.fold.wedge` (INFO, per-fold wedge angle — prototype `analyze.py`);
`prism.hcol.clash` (ERROR, H···H across a notch < 1.8 Å); `prism.closure`
counts fold tokens in the turn law; odd `periods` with `sp3-z` refused
`fit.unsolvable` before build.

Acceptance:
- `CCCCCC` and `cccRcccRcccR` at (2, 4) and (4, 6) match the census.
- `++++++` and `+++-+++-+++-` as `prism-k3-sp3-z`: every seam atom has
  degree 4 and exactly one same-seam partner at 1.60 Å (seed); each
  seam's ring census equals `straight_k3z`'s; `prism.rib.clearance` still
  enforced.
- `prism-k3-120-z` refuses new builds with `seam.sp2_retired` (one test);
  its SHA golden is dropped; the skills drop it from the examples.
- H on the reflex side for 100% of H in seed and after stick.
- Every other slice-0 golden unchanged.
- GFN2 (local tblite, prototype settings, periodic (2, 4)): no C–C bond
  broken or formed; within 0.01 eV/C of A1 +0.075 and A2 +0.160; `++++++`
  dimer pairs stay at 1.55–1.65 Å. Recorded per §Stability claims; not
  in CI.

### Slice 4 — cells as the tiling layout (gap 3)

F3: a tiling layout function (cells → cross-section graph, junction specs
from kept-wall degree) feeding `assemble`, plus a schema entry on the
shared SE adapter. The layout is the only new construction code. Prism is
the one-cell case: a single cell with every vertex `+` is the polygon
hexagon `++++++`.

Validator rules:
- `cells.connected` (ERROR): the cell set is one edge-connected component.
- `cells.group.contiguous` (ERROR): each merge group is edge-connected.
- `cells.hole` (ERROR in v1): a non-cell region fully enclosed by walls.
- `cells.fold.enclosed` (WARN): a hydrogenated fold with a cell, not the
  exterior, on its convex side — "bundle notch fold: ruptured in GFN2 at
  2-chain walls (me503663); no stability claim". Revised when the 4-chain
  wall result lands (ruling 3).
- `cells.fold_h.group` (ERROR): `fold_h` names a group not in `cells`.

Acceptance:
- tri-tube, bundle3, bundle6 match the census at both (R, P).
- Single-builder identity: the three tri cells in one group with
  `fold_h:{core:"reflex"}` and the polygon `cccRcccRcccR` (A2) yield the
  same cross-section graph, so `assemble` emits the same SHA; likewise one
  cell vs `++++++`. This tests two layouts over one builder.
- `cells.fold.enclosed` fires on bundle3/bundle6, not on A2 or tri-tube.
- GFN2 (local, periodic (2, 4)): tri-tube within 0.01 eV/C of +0.119. The
  bundle3 rerun is a manual campaign item (≈1.5 h on the Mac), not CI.

### Slice 5 — GFN2 from the session MCP (gap 5, blocked)

Blocked on the xtb dispatch route (owned elsewhere). When it lands: rerun
A1, A2, tri-tube and the 4-chain bundle3 through `relax` with
`fidelity='xtb'` on the built blocks so the GFN2 result is stored on the
structure; replace the local-run records.

Acceptance: each target's structure has an xtb-rung relax record; the
energies agree with the local runs within 0.01 eV/C.

## Stability claims

No block description, finding, catalogue row or skill example says a
geometry "holds", "is stable" or "is a minimum" without a recorded GFN2
result. Recorded means: an xtb-rung relax stored on the structure (after
slice 5), or until then a memory or finding linked to the block that
states method, cell, k-points, Te, fmax, energy vs reference and the bond
change census. UFF and stick are previews and are described as such.
Generator findings never claim stability; `cells.fold.enclosed` marks the
case GFN2 has not confirmed. The skills state this rule (§Skills).

The current se design blocks state UFF results only and claim no
stability; me503663 holds the GFN2 record.

## Skills

| skill (`src/precis/data/skills/`) | change | slice |
|---|---|---|
| `precis-hexfold-seams-help.md` | prism section: fold tokens table, H-side rule, new validator codes; `prism-k3-sp3-z` section; `cells-k3-sp3-z` section with example; one paragraph on strips, junctions and layouts; front-matter `summary`/`answers` gain the new types | 3, 4 |
| `precis-hexfold-help.md` | the seams pointer names the new types; the H-termination section distinguishes `hydrogenated` from `terminated`; the stability-claim rule | 2, 3, 4 |
| `precis-se-atomic-help.md` | the terminate bullet gains `topology.hydrogenated` and tag `hydrogenated:interior` | 2 |
| `precis-structure-help.md`, `precis-job-observe-help.md` | xtb dispatch from the session host — owned by the dispatch work | 5 |

Slices 0 and 1 change no user-visible behaviour and edit no skill. No new
skill: `precis-hexfold-seams-help` is about 1,700 words; split
`cells-k3-sp3-z` out only past about 2,500.

## Work

| slice | gap | lands in | size | agent |
|---|---|---|---|---|
| 0 SHA goldens + census + glossary | — | `tests/hexfold/data/`, `docs/glossary.md` | S–M, 1 build | coder (Sonnet) |
| 1a primitives; prism + railtube as layouts | — | new `src/hexfold/sheetnet.py`; `prism.py`, `railtube.py` | L, 1 build | coder-chain (Sonnet); Opus design-review of the primitive signatures |
| 1b channel, fin120, y_junction, sp3_seam as layouts | — | `channel.py`, `fin120.py`, `y_junction.py`, `sp3_seam.py`, `fin.py` | M–L, 1 build | coder-chain (Sonnet) |
| 1c shared SE adapter | — | `precis_se/atomic/generators/` | M, 1 build | coder (Sonnet) |
| 2 decorate | 4 | `sheetnet.py`, `check.py`; `hexfold_spec.py` | M, 1 build | coder (Sonnet); design-review |
| 3 fold tokens + `sp3-z` junctions | 1, 2 | polygon layout, token parser, `canon.py` | M, 1 build | coder (Sonnet); design-review |
| 4 tiling layout | 3 | tiling layout + adapter schema entry | M, 1 build | coder (Sonnet); Opus design-review |
| 5 GFN2 via MCP | 5 | route owned elsewhere; prod structures | campaign, after dependency | coder or cmd-runner |
| docs per slice | — | the skills above, `src/hexfold/spec.md` 0.3 | with each slice | documenter (Sonnet) |
| GFN2 local checks (3, 4) | — | Mac-local tblite, prototype scripts | ~0.5–1.5 h compute each | cmd-runner |

Total: about 7 builds plus the blocked slice 5. Slices 3 and 4 are each
smaller than the old fold, seam and cells builds because the junction and
assembly code exists after slice 1.

## Not in scope

- The xtb dispatch route itself (gap 5).
- In cells v1: fins, ribs and pores on cell walls; unequal wall lengths;
  chiral or armchair-axis walls; axial patterns. Each arrives later as a
  primitive or layout parameter under the DRY rule.
- Fluorinated folds (the isolation-band item carries `terminator=F`).
- Public `.hx` text grammar for prisms or cells.

## Rulings (Reto, 2026-10-22)

1. Retire `prism-k3-120-z` — new builds refuse (F2, slice 3).
2. Drop `ribs:"meet"` — the `cells` form builds the tri-tube; one way to
   build a structure, not two.
3. Bundle notch fold: WARN until the 4-chain wall GFN2 result; then ERROR
   if wider walls also rupture, rule removed if they hold.
4. Fins/pores in cells: later, as vocabulary additions once the bundle
   result is in — under the DRY rule, as primitive or layout parameters.
5. DRY first: a cell wall is a graphene strip; one primitive layer, every
   tube builder a layout (this rewrite).
