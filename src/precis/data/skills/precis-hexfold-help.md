---
id: precis-hexfold-help
title: precis — the hexfold generator (curved sp² carbon from a .hx spec)
summary: generate atomic se blocks — sheets, tubes, cones, fullerenes, holes, fused joints, nanobuds — from a topology-only .hx spec text via generator='hexfold'; coordinates are derived, the spec is the regeneration input; fidelity='check' returns the check report without minting
answers:
  - how do I generate a nanotube/cone/fullerene/nanobud block from a hexfold spec?
  - what does a .hx spec look like and which nanobud menus exist?
  - how do I check a hexfold spec without minting a block?
  - what do the hexfold check codes mean?
applies-to: put/edit (kind='se', op='generate')
status: active
tags: verbs, design
kinds: se
---

# precis-hexfold-help — sp² carbon by topology, not coordinates

`hexfold` is a standalone notation/compiler: a `.hx` spec declares *where
defects sit on a hexagonal lattice* (which ring, which site, which
attachment), and the bond graph + coordinates are derived deterministically.
The `se` generator `hexfold` runs that compile inside `generate`.

## Call shape

```json
{"op": "generate", "generator": "hexfold",
 "params": {"spec": "<.hx text>"}, "name": "bud"}
```

Check-only (no block, nothing minted — the report comes back as the echo):

```json
{"params": {"spec": "...", "fidelity": "check", "geometry": true}}
```

`geometry` defaults true for `fidelity="check"`; the report covers
counting laws, ports, and (with `geometry`) the stick-preview bond/angle
deviations. `fidelity="stick"` (the default) builds and mints; `dry_run`
is a deprecated alias (`dry_run=True` ⇒ `fidelity="check"`).

## One spec, whole structure

```text
hexfold 0.2
lattice: element=C sigma=1.42

origin h
h: tube(10,10, len=14)
b: fullerene(C60)
b @ h/(7,0,A):0 [9-6]
```

`h: tube(...)` / `fullerene(C60)` are instances; `b @ h/(site):dir
[menu]` attaches. Remaining rims become block ports, named
`<instance>_<rim>` (hexfold's `h.out` is se port `h_out`; a
single-instance spec keeps bare `in`/`out`). Each carries its dangling
ring and the hexfold path (`hx`) in `topology.ports`.

## Joining parts: rims and `fuse`

Every primitive exposes named rims; a `fuse` glues two rims of equal
dangling count `N`, written **inside the arrow**:

```text
hexfold 0.2
lattice: element=C sigma=1.42

s: sheet(25A, 12)
t: tube(fit in {(5,5),(6,6)}, len=3)
c: cap(5,5)
t.out --fuse k=0--> c.in
```

| primitive | rims | notes |
|---|---|---|
| `tube(n,m,len=)` | `in`, `out` | `N = n+m`; a `- hexagon@…` hole adds `hole` |
| `cap(n,m)` | `in` | `cap(5,5)` N=10; `cap(6k,0)` lid N=6k; a hole adds `hole` |
| `sheet(W,H)` | `rim` | the outer boundary (mixed type, `rim.nonstandard` INFO) |
| `cone(P)` | `base` | the open frustum end; `fullerene(C60)` has no rim (it attaches via `@`) |

`k` is the rotational phase (`0..N-1`); `--bond-->` adds single covalent
edges instead of a seam; `seam <name>: A.r == B.r == C.r` is the k ≥ 3
form. The verb-first spelling `fuse P --> Q` is not a statement.

## Iterating: check, then edit

`fidelity="check"` on a `put` still creates the (empty) design row, and a
second `put` on the same id is a **full replace** that drops any minted
block. Iterate with `edit(kind='se', id=..., ops=[...])` once the design
exists. A refused spec comes back as `line:col: message` (ParseError) or
as the rendered report (ERROR findings); `hexfold internal error …` means
a compiler bug — file a gripe with the spec.

After minting, the build report (`extent.snap`, `fit.propagated`,
`seam.rings`, …) is under `get(kind='se', id=..., view='block',
args={'name': '<block>'})` → "## generated". Before minting, run the
same spec with `fidelity="check"`.

## Nanobud menus (attachments)

| menu | construction | citation |
|---|---|---|
| `[2+2]` | two authored bonds, C60 6-6 bond → host bond | Nasibulin 2007 (pa2069, doi:10.1038/nnano.2007.37) |
| `9-6` | six bonds, C54 (C60 − hexagon) onto intact host; seam {9:3, 6:3} | Zhu & Su 2009, Phys. Rev. B 79, 165401, doi:10.1103/PhysRevB.79.165401 (pa543) |
| `8-7` | the other C3 registration; seam {8:3, 7:3} | Zhu & Su 2009, Phys. Rev. B 79, 165401, doi:10.1103/PhysRevB.79.165401 (pa543) |
| `DA-neck` | C60 − pentagon → (5,0) neck ({7:5}) → host `path3` opening | Baowan, Cox & Hill 2010 (pa679, doi:10.1080/15363830903586625) |
| `DB-neck` | C60 − 3 pentagons → (6,0) neck → hexagon hole + {7×3} collar (needs `k | gcd(n,m)`) | Baowan, Cox & Hill 2010 (pa679, doi:10.1080/15363830903586625) |

## Check codes (hexfold spec §13, terse)

`euler.chi` INFO · `euler.residual` INFO/WARN (curvature budget; both
per sheet, `data.sheet`) · `euler.closed_unreachable` ERROR ·
`valence.over`/`under` ERROR/WARN · `cut.overlap` ERROR (opening spans
the circumference — wider tube) · `ring.size.unusual` WARN (outside
4..8; seam faces exempt) · `port.mismatch`/`port.symmetry` ERROR ·
`fit.unsolvable` ERROR · `fit.alternatives` INFO (the ranked rest of a
`fit` family) · `fit.propagated` INFO (a roll-up domain before/after
chain propagation) · `extent.snap` INFO (an Å sheet extent snapped to
cells, `data.delta_A`) · `seam.rings` INFO (ring census along a fuse or `seam`,
`data.k`) · `registry.closure` WARN (a part-graph cycle closes with a
phase residual, `data.residual` of `data.period`) · `gen.stale` WARN
(`.hx.json` generated block behind its authored hash) · `op.dangling`
ERROR (a `bond`/`terminate` names an atom or port that no longer exists)
· `frag.unrealized` INFO ·
`geom.summary`/`geom.bond.*`/`geom.angle.dev`/`geom.join.*` INFO/WARN ·
`annot.sublattice`/`annot.host_sublattices` INFO.

## Rim types (spec §10)

A rim's type is `(kind, N)` from its dangling pattern: `tube(n,0)` ends
are zigzag `z<n>`, `tube(n,n)` ends armchair `a<2n>`, chiral ends and
cap/hole rims are mixed. Fuse needs equal `N` only; zigzag onto
armchair at equal `N` is the 30° grain-boundary adapter (5-7 seam
rings). Prefer zigzag `N` in multiples of 6 so caps, washers and lids
interoperate; `rim.nonstandard` INFO marks the rest. se ports carry it
as `lattice="sp2-hex"`, `payload={kind: rim, word, N, type}`.

## Options (spec §25.3)

`hexfold.options.options(spec, handle, wish)` / `hexfold options FILE
HANDLE`: state a wish at one fit site and get the realisable values
near it. Handles `t.len` (periods; `target_A`/`band_A` in Å) and
`a.out.k` (phase steps, modular). Wish = target + band, or nothing for
the plain fit family. Returns `options` (clean, ranked by distance then
seam ring, residual, index), `rejected` (in band but does not build,
with the ERROR codes) and `applied` (what a plain build picks). One call
per wish; it does not search across sites.

## Domains and chains (spec §12.1, §22.3)

A roll-up may be a domain: `tube(fit in {(5,5),(6,6)}, len=3)`,
`cap(fit)` (catalogue: `(5,5)` and the `(6k,0)` lids; `tube(fit)` is the
64 pure zigzag/armchair roll-ups). Pin either end of a chain and
propagation resolves the middle: every fuse equates its rims' `N`
(`n+m` for a tube end), arc consistency prunes each domain, the
survivors are built and ranked (all-hexagon seam beats the 5-7
adapter). Read `fit.propagated` for what pruned what; an emptied domain
is `fit.unsolvable` with `needs`/`offers`/`constraint`. Domains resolve
before `len=fit`.

## Å extents and se measures (spec §7)

`sheet(25A, 12)` snaps to whole cells along the lattice vector
(`a = 2.46 Å`) and reports `extent.snap` with the delta. The generated
se block declares its lengths as measures — `<inst>_W`, `<inst>_H`,
`<inst>_len` (value = realised length, `min`/`max` = the snap cell: the
requests that land on the same integer) and `<inst>_R` (a point). Relate
your own measures to them (`relation.source = "<block>.<inst>_W"`,
grammar in [[precis-se-help]] `add_measure`) and `view='measures'` stacks
the tolerance up: the `status` column says `ok`, or that the declared
value disagrees with the derived one beyond the accumulated tolerance;
`view='drc'` reports the same as a `tolerance_mismatch` warning. A `tol`
tighter than the snap delta is exactly that case.

## Rules

- Counting is a diagnostic, not a gate; `geom.*` describes the stick
  preview, not matter.
- Coordinates are derived; the spec string (`topology.spec`) is the
  regeneration input.
- Bond-mode attachment sites come out `sp3` — bonds touching them get
  order 1; sp²–sp² bonds get the Pauling 4/3 aromatic order.
