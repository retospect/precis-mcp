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
`fit` family) · `seam.rings` INFO (ring census along a fuse or `seam`,
`data.k`) · `registry.closure` WARN (a part-graph cycle closes with a
phase residual, `data.residual` of `data.period`) · `gen.stale` WARN
(`.hx.json` generated block behind its authored hash) · `op.dangling`
ERROR (a `bond`/`terminate` names an atom or port that no longer exists)
· `frag.unrealized` INFO ·
`geom.summary`/`geom.bond.*`/`geom.angle.dev`/`geom.join.*` INFO/WARN ·
`annot.sublattice`/`annot.host_sublattices` INFO.

## Rules

- Counting is a diagnostic, not a gate; `geom.*` describes the stick
  preview, not matter.
- Coordinates are derived; the spec string (`topology.spec`) is the
  regeneration input.
- Bond-mode attachment sites come out `sp3` — bonds touching them get
  order 1; sp²–sp² bonds get the Pauling 4/3 aromatic order.
