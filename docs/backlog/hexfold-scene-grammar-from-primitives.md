---
status: draft
title: hexfold scene grammar from primitives — tube, sheet, seam/graft line, strip, each with its own tether and a composition rule, one shared planner
pillar: 3d-design
prio: high
---

# hexfold scene grammar from primitives

Reto, 2026-10-07 (via chat-interface, after the Y-junction and the two fin
scenes): ruled YES on growing the `hexfold_scene` grammar from primitives.
Every scene today is one exclusive code path — the foot features
(`plan_scene`), the equal-120 Y (`type: k3-sp2-120-z`), the sp3 fin
(`type: fin-sp3-z`), and next the 120° seam tube — each with its own
seed, its own tether and its own relax call, and each needing a deploy
before Reto can look at it. The grammar makes them parameter sets over a
small set of primitives, with one planner and one judgement, so a new
scene is a new parameter set, not a new module. Owner thread:
[hexfold-toolkit](threads/hexfold-toolkit.md).

## Motivation / why

- Three exclusive feature types in two weeks, each a copy of the same
  shape: analytic seed, a tether function, `stick_relax_pinned`, a set of
  pinned joint atoms, `geometry_findings`, a `surface.target.unavailable`
  note, `_block_from_net`. The Y adapter and the fin adapter differ in
  the seed and the tether and in nothing else that matters.
- Each new scene costs a deploy before it is askable; the coordinator's
  round cadence puts hours to a day between "built" and "Reto's look".
- The judgement bars are duplicated per path, so a scene can quietly get
  a weaker judge than its sibling.
- H-termination of open edges (Reto, 2026-10-07) is a rule every build
  must apply at the end; with one path per scene it is one more thing
  each path forgets.

## In scope

**Primitives and their tethers.** Each primitive names the surface its
atoms are tethered to and takes its own `k_tether`.

| primitive | atoms | tether surface | parameters |
|---|---|---|---|
| `sheet` | honeycomb patch | plane | `[w, h]` or `[periods, row_pairs]`, lattice orientation |
| `tube` | rolled patch, `(n, m)` | cylinder (axis, radius from `tube_radius`) | chirality `(n, m)`, `len`, which axial line is the seam/graft line |
| `seam` | the seam atoms of an equal-120 sp2 k3 seam, or the sp3 hosts of a graft line | the seam line (pinned registration, as the Y pins seam + neighbours) | `kind: k3-sp2-120-z | sp3-graft`, `along: axis | lattice direction`, dihedrals (120·3 only; unequal stays refused, catalogue), period |
| `strip` / fin | finite zigzag-edged sheet | its half-plane | width in hex rows, `side: in | out`, which seam/graft line it hangs on |
| `top` | sphere, lid, ball, open — as today | the authored surface of revolution (`plan_top`) | as today |

**Composition rule.** A scene is a list of primitives plus joins:

- `seam(kind=k3-sp2-120-z)` joins exactly three sheets (or two wall halves
  of a tube and one strip) at authored dihedrals; the Y's `compose_k3`
  is the solver; the far side of a tube's two wall halves closes by a
  zigzag fuse in direct register.
- `seam(kind=sp3-graft)` bonds a strip's zigzag edge to a wall along an
  axial line, one radial bond per period, hosts become sp3
  (`hexfold.fin`).
- foot features join a tube to a sheet through a hole with a fillet, as
  `plan_scene` does today.
- The planner measures what today's planners measure (frustum widths
  `k`, registration, which table row fits) and chooses by the same
  bars; the judgement is the one `geometry_findings` + scene bars pass,
  labelled `relax=tethered`, with `surface.target.unavailable` whenever
  the composed target is not a surface of revolution.
- Every primitive's atoms are pinned or tethered, never free, unless the
  scene says `k_tether: 0` for that primitive (the 120° seam tube's wall
  halves are the first use: free wall, pinned seam, tethered strip).

**Backward compatibility.** Today's foot features and the two exclusive
types become parameter sets:

- `features: [{name, at, n, radius, tube_len, top…}]` ≡ a `sheet` plus,
  per feature, a `tube` joined by a foot and a `top`.
- `type: k3-sp2-120-z` with `sheet: [periods, row_pairs]` ≡ three
  `sheet` primitives on one `seam(k3-sp2-120-z, dihedrals 120·3)`.
- `type: fin-sp3-z` with `tube: [n, periods]` ≡ a `tube` plus a `strip`
  on a `seam(sp3-graft, along axis)`.
- Existing stored scenes keep rendering byte-identically: the stored
  `generated.scene` is replayed through the old normaliser, which maps to
  the primitive form; a regenerated block must match the stored one
  within the bars (acceptance below).

**Slice 0, first — the escape hatch.** A tethered-relax op that takes
`.hx` text (or a join) plus a tether list — `[{atoms | instance | region,
surface: plane | cylinder | sphere | line, params, k, pinned?}]` — and
returns the standard judgement, so any scene Reto can describe in `.hx`
is askable the same day with no deploy. This is the thing that turns a
"next variant" chat message into an op instead of a module.

**Slices 1+.** The primitives, in the order that reproduces, as parameter
sets, what exists: Y-A (`se:hexfold-dogfood-y-a`), the two fin scenes
(outward (10,10), inward (18,18)), and the heart-shaped 120° seam tube.

**Cross-cutting: default H-termination.** At the end of every build the
grammar caps every under-coordinated carbon with H (1.09 Å along the
missing bond; sp2 edges and sp3 hosts alike), records it on the block
(`terminated: {element: H, count, mode}`) and tags the structure
`terminated:h` (the `autoterminate` rule), so se reports, renders and a
DFT handoff know the edges are capped and why. Opt-out per op:
`terminate: "none"`; `terminate: "ports-open"` leaves join ports bare for
a later fuse. Shipped first in `_block_from_net` for the hexfold family
(hexfold, hexfold_scene, Y, fin), then inherited by the grammar.

## Explicitly NOT in scope

- Unequal-dihedral k3 seams and k ≥ 5 (catalogue ruling, 2026-09-26:
  refuse, do not approximate).
- Stability, strength or printability claims; the judgement stays the
  preview-geometry one.
- A new relaxer. `stick_relax_pinned` with per-primitive tethers is the
  relax; MACE/xTB stay the separate structure jobs.
- Rewriting the foot planner's measured tables; they are reused as the
  `top` primitive's planner.
- The pcb/se-level assembly (poses, joins between blocks): the grammar
  builds one block.

## Acceptance criteria

- The four existing scenes — Y-A 30×30, fin outward `tube:[10,20]`, fin
  inward `tube:[18,20]`, and the 120° seam tube — regenerate from their
  parameter-set form and match their stored blocks within the bars:
  same atom and bond counts, same ring census, stored-vs-regenerated
  coordinate RMS under 0.05 Å, same finding codes.
- One new scene of the implementer's choice (for example a tube with two
  strips on opposite generatrices, or a sheet with a strip on each face)
  is built from a parameter set with zero code change.
- Slice 0: an `.hx` text plus a tether list produces the same block as
  the Y adapter for Y-A's spec (the `.hx` form of the Y, with the three
  plane tethers listed), within the bars above, with no deploy between
  writing the op and reading the block.
- Every scene built through the grammar carries `terminated: H` unless
  the op opted out, and its H count equals the number of under-coordinated
  carbons before termination.
- Stored pre-grammar scenes render byte-identically (`view='block'` text
  unchanged for `se:hexfold-dogfood-y-a`, `-r4`, `-r5`).

## Target + blast radius

- `src/precis_se/atomic/generators/hexfold_scene.py` (dispatch),
  `authored_foot.py` (planner tables), `y_junction.py`, `fin.py`
  (become parameter-set normalisers), `hexfold_spec.py::_block_from_net`
  (termination), a new `scene_grammar.py` (primitives, composition,
  tether assembly).
- `src/hexfold/`: `y_junction.py`, `fin.py`, `join.py::compose_k3`
  (reused, not changed), a new far-side zigzag fuse helper.
- Skills `precis-hexfold-help`, `precis-se-atomic-help`.
- Reads: `view='block'` rendering of stored scenes (must not change).

## Open questions / decisions log

- 2026-10-07, Reto: ruled YES on the grammar; slice 0 (tethered-relax op)
  first.
- 2026-10-07, Reto: default H-termination with the `autoterminate` tag is
  the cross-cutting rule; opt-out for intentionally open rims.
- Open: whether slice 0's tether list addresses atoms by `.hx` instance
  names only, or also by region/ordinal (needed for a join's seam atoms).
- Open: the strip primitive's width unit — hex rows (Reto's phrasing) or
  honeycomb row pairs (the Y's `row_pairs`); the fin uses row pairs with
  "about three hexagon rows" at 4.
- Open: whether `k_tether: 0` on a primitive (free wall) needs a clash
  guard beyond `geom.clash`, since nothing then holds the wall off the
  strip.

test: tests/test_se_scene_grammar.py (new); tests/test_se_y_junction.py,
tests/test_se_fin.py, tests/test_se_hexfold_scene_generator.py (byte-identity
of stored scenes).
