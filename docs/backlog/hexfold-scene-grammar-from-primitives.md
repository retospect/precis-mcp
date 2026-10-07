---
status: draft
title: hexfold scene grammar from primitives — tube, sheet, seam/graft line, strip, each with its own tether and a composition rule, one shared planner
pillar: 3d-design
prio: high
model: opus
blocked-by: hexfold-tethered-relax-op
---

# hexfold scene grammar from primitives

Reto, 2026-10-07 (via chat-interface, after the Y-junction and the fin
scenes): ruled YES on growing the `hexfold_scene` grammar from primitives.
Every scene today is one exclusive code path — the foot features
(`plan_scene`), the equal-120 Y (`type: k3-sp2-120-z`), the sp3 fin
(`type: fin-sp3-z`) and the 120° seam tube (`type: fin-k3-120-z`) — each
with its own seed, its own tether and its own relax call, and each
needing a deploy before Reto can look at it. The grammar makes them
parameter sets over a small set of primitives, with one planner and one
judgement, so a new scene is a new parameter set, not a new module.
Slice 0, the tethered-relax generator, is its own item
([hexfold-tethered-relax-op](hexfold-tethered-relax-op.md)) and ships
first; it also extracts the shared planner this item builds on. Owner
thread: [hexfold-toolkit](threads/hexfold-toolkit.md).

## Motivation / why

- Three exclusive typed features in two weeks, each a copy of the same
  shape: analytic seed, a tether function, `stick_relax_pinned`, a set of
  pinned joint atoms, `geometry_findings`, a `surface.target.unavailable`
  note, `_block_from_net`. The adapters differ in the seed, the tether,
  the pinned set and the ring-size filter, and in nothing else.
- Each new scene costs a deploy before it is askable; the coordinator's
  round cadence puts hours to a day between "built" and "Reto's look".
- The judgement bars are duplicated per path, so a scene can quietly get
  a weaker judge than its sibling.

## In scope

**Primitives and their tethers.** Each primitive names the surface its
atoms are tethered to and takes its own `k_tether` (0 = free, authored,
never an omission — the `tether.coverage` rule of slice 0).

| primitive | atoms | tether surface | parameters |
|---|---|---|---|
| `sheet` | honeycomb patch | plane | `[periods, row_pairs]`, lattice orientation |
| `tube` | rolled patch, `(n, m)` | cylinder (axis, radius from `tube_radius`) | chirality `(n, m)`, `len`, which axial line is the seam/graft line |
| `seam` | the seam atoms of an equal-120 sp2 k3 seam, or the sp3 hosts of a graft line | pinned registration, as the Y pins seam + neighbours | `kind: k3-sp2-120-z | sp3-graft`, `along: axis`, period |
| `strip` / fin | finite zigzag-edged sheet | its half-plane | width in row pairs (`rows`, default 4 = about three hexagon rows), `side: in | out`, which seam/graft line it hangs on |
| `top` | sphere, lid, ball, open — as today | the authored surface of revolution (`plan_top`) | as today |

**Composition rule.** A scene is a list of primitives plus joins. The
joins are exactly the three solvers that exist; the grammar names them,
it does not generalise them:

- `seam(kind=k3-sp2-120-z)` joins exactly three sheets, or a strip and the
  two wall halves of a tube, at 120·3 (`hexfold.join.compose_k3`, which
  refuses anything else); a tube's two wall halves close on the far side
  by the zigzag fuse `hexfold.fin120.seam_tube` already builds.
- `seam(kind=sp3-graft)` bonds a strip's zigzag edge to a wall along an
  axial line, one radial bond per period, hosts become sp3
  (`hexfold.fin.fin_tube`).
- foot features join a tube to a sheet through a hole with a fillet. This
  stays the `plan_scene` text path, wrapped, not decomposed: a
  `features:` scene is the `sheet + tube + top` parameter set by
  definition, and `plan_scene` is its planner. Decomposing `plan_scene`
  is out of scope.
- The two mechanisms meet in `relax_tethered` (slice 0): both produce a
  `Net` plus seed coordinates plus a tether list; the planner relaxes and
  judges both the same way, labelled `relax=tethered`, with
  `surface.target.unavailable` whenever the composed target is not a
  surface of revolution.

**Backward compatibility.** Today's foot features and the three exclusive
types become parameter sets:

- `features: [{name, at, n, radius, tube_len, top…}]` ≡ a `sheet` plus,
  per feature, a `tube` joined by a foot and a `top`.
- `type: k3-sp2-120-z` with `sheet: [periods, row_pairs]` ≡ three
  `sheet` primitives on one `seam(k3-sp2-120-z)`.
- `type: fin-sp3-z` with `tube: [n, periods]` ≡ a `tube` plus a `strip`
  on a `seam(sp3-graft, along axis)`.
- `type: fin-k3-120-z` with `tube: [rows, periods]` ≡ two `sheet` wall
  halves plus a `strip` on one `seam(k3-sp2-120-z)`, far side fused.
- The old param forms keep working: the normaliser maps them to the
  primitive form, and the stored `generated.scene` is replayed through it.

**H-termination** is already shipped in `_block_from_net`
(`terminate: H | ports-open | none`, record `terminated: {element, mode,
count, hosts, clashes, …}`, tag `terminated:h`); the grammar inherits it
through `_block_from_net` and adds nothing.

## Explicitly NOT in scope

- Unequal-dihedral k3 seams and k ≥ 5 (catalogue ruling, 2026-09-26:
  refuse, do not approximate; `hexfold-seam-type-catalogue.md`).
- Stability, strength or printability claims; the judgement stays the
  preview-geometry one.
- A new relaxer. `stick_relax_pinned` with per-primitive tethers is the
  relax; MACE/xTB stay the separate structure jobs.
- Decomposing `plan_scene` or rewriting the foot planner's measured
  tables; they are reused behind the `top`/foot parameter set.
- A general join. `compose_k3` keeps its three-block, 120·3 contract.
- The pcb/se-level assembly (poses, joins between blocks): the grammar
  builds one block.
- Slice 0 itself (the tethered-relax generator and the `relax_tethered`
  extraction): `hexfold-tethered-relax-op.md`.

## Acceptance criteria

All comparisons run with `terminate: "none"` on both sides (the stored
prod scenes predate the H-cap default and do not record `terminate`),
and in the gate against committed small goldens, not prod rows:

- Parameter-set parity, in the gate: for each of the four existing
  scenes at test size — Y `sheet:[4,6]`, fin `tube:[6,4]` out, fin
  `tube:[18,4]` in, seam tube `tube:[6,4]` — the primitive form and the
  old typed form return identical elements, bonds, ring census and
  coordinates (`np.array_equal`), and the same finding codes
  (`tests/test_se_scene_grammar.py`, goldens under `tests/fixtures/`
  written by the existing adapter tests' fixtures).
- Prod parity, recorded on the thread after the deploy: regenerate
  `se:hexfold-dogfood-y-a` (30×30), the two sp3 fin scenes
  (`tube:[10,20]` out, `tube:[18,20]` in) and the two seam tubes from the
  primitive form with `terminate:"none"` into sibling slugs; atom and
  bond counts and ring census equal the stored block, coordinate RMS
  against the stored block under 0.05 Å, same finding codes. Procedure:
  `view='block'` on both slugs plus one read-only `scripts/prod-psql`
  pull of positions for the RMS; the numbers go on the thread.
- One new scene from a parameter set with zero code change: a tube
  `(10,10)`, 20 periods, with two `strip`s on opposite generatrices
  (`sp3-graft`, both `side: out`, `rows: 4`). Bars: `geom.clash` empty,
  both grafts report `graft.rings` with `periods − 1` rings each, host
  angles within [85°, 125°] on both lines, ring census equals the tube's
  plus twice one strip's, and the two strips' planes are antiparallel
  within 1°.
- Stored pre-grammar scenes render unchanged: `view='block'` text for
  `se:hexfold-dogfood-y-a`, `-r4`, `-r5` compared before and after the
  deploy (recorded on the thread), and in the gate the typed-form
  goldens of `tests/test_se_hexfold_scene_generator.py` and
  `tests/test_se_fin120.py` stay pinned.

## Target + blast radius

- `src/precis_se/atomic/generators/hexfold_scene.py` (dispatch →
  normaliser to the primitive form), `y_junction.py`, `fin.py`,
  `fin120.py` (become parameter-set normalisers over `relax_tethered`), a
  new `scene_grammar.py` (primitives, composition, tether assembly).
  `authored_foot.py` is reused, not changed.
- `src/hexfold/`: `y_junction.py`, `fin.py`, `fin120.py`,
  `join.py::compose_k3` reused, not changed.
- Skills `precis-hexfold-help`, `precis-se-atomic-help`.
- Reads: `view='block'` rendering of stored scenes (must not change).
- Sibling specs touching the same files: `hexfold-seam-type-catalogue.md`
  (seam contracts), `hexfold-sp3-seam.md` (sp3 seam types; the
  `sp3-graft` seam kind here is the `bond` attachment of spec §11.1,
  not a new seam type).

## Open questions / decisions log

- 2026-10-07, Reto: ruled YES on the grammar; slice 0 (tethered-relax op)
  first.
- 2026-10-07, Reto: default H-termination with a tag is the cross-cutting
  rule; opt-out for intentionally open rims. Shipped the same day
  (`_block_from_net`); recorded in the thread's "Default behaviours".
- 2026-10-07, ready vet (needs-work): split slice 0 out; termination is
  shipped; inventory had three typed features, not two; acceptance must
  run with `terminate:"none"` against committed goldens, prod parity as a
  recorded procedure; name the shared planner; composition rule limited
  to the three existing solvers; `plan_scene` wrapped not decomposed;
  name the new scene and its bars; `model: opus`. All applied above.
- Decided: strip width unit is row pairs (`rows`), as the fins already
  use; "about three hexagon rows" = 4 row pairs.
- Decided: `k_tether: 0` on a primitive needs no extra guard; fin120 runs
  a free wall under `geom.clash` and `terminate.clash` and the seam tube
  measured clean (0 clashes outward and inward). Revisit only if a free
  primitive clashes in practice.

test: tests/test_se_scene_grammar.py (new); tests/test_se_y_junction.py,
tests/test_se_fin.py, tests/test_se_fin120.py,
tests/test_se_hexfold_scene_generator.py (typed-form goldens stay pinned).
