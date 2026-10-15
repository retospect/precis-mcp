---
id: precis-hexfold-seams-help
family: se
title: precis — hexfold seams: every typed hexfold_scene entry (Y, sp3 seam lines, seam/rail tubes, channel, reservoir tunnel, fin)
summary: the exclusive typed feature entries of generator='hexfold_scene', one section each — the straight Y (k3-sp2-120-z) and its sp3 seam-line siblings (k4-sp3-90-a, k3-sp3-z, k3-sp3-a), the seam tube (fin-k3-120-z), the rail tube (rail-k3-120-z), the channel junction (chan-k3-sp3-z), the reservoir tunnel (tunnel-reservoirs) and the sp3 graft fin (fin-sp3-z)
answers:
  - how do I build a straight 120-degree Y junction of three sheets?
  - how do I run a seam or a strip along a tube axis?
  - how do I close a tube of N arcs on N seams with inward ribs, pores and a site list?
  - how do I close a tube between two reservoir sheets (sheet | tube | sheet)?
  - what feature types does hexfold_scene take, and what are each one's params?
applies-to: put/edit (kind='se', op='generate')
status: active
tags: verbs, design
kinds: se
---

# precis-hexfold-seams-help — equal-120 seams along open sheets and tubes

Every exclusive typed `hexfold_scene` feature entry — the k3 seam
grammar, the sp3 seam lines and the sp3 graft — split out of
[[precis-hexfold-help]] (call shape, scene features, tops, refusals live there). All mint analytic
preview geometry only.

## Straight Y: equal-120 sp2 seam

An exclusive scene entry joins three open sheets along a straight zigzag
seam. It is separate from foot scenes; no `extra` or mixed features.

```json
{"op":"generate","generator":"hexfold_scene","name":"y",
 "params":{"sheet":[30,30],"k_tether":1.0,
 "features":[{"name":"y","type":"k3-sp2-120-z",
              "dihedrals_deg":[120,120,120]}]}}
```

`sheet` is `[seam_periods,honeycomb_row_pairs]`, integers in [2,30].
Two guard columns close segment endpoints. At 30 by 30 this makes 5,790
atoms, 30 trivalent seam atoms and 87 eight-cycle seam faces. `k_tether`
must be finite and positive; analytic seed, minimally relaxed: seam and
neighbours pinned, outer sheet atoms tethered to their sheet plane.
`seam.rings`, `seam.geometry`
and `geom.summary` are retained in the block report. Read the bond
and ring-angle findings; eight-cycle warnings are not hidden. Preview
geometry, not stability evidence.
Replay `generated.scene` params; this entry does not generate hx text.
Three planes are unsupported by the revolution target format, so stored
`surface_deviation` is honestly unavailable. Unequal dihedrals/k>=5 return
`fit.unsolvable`; variant B remains unimplemented.

**The equal-120 sp2 seam is not a free minimum.** Free GFN2-xTB relaxes
the sp2 120° Y and the seam tube INTO the k3 dimer chain (axial pairs
1.60–1.64 Å, sheet bonds 1.50–1.53 Å, seam angle sums ~341°; hexhex
2026-10-13, confirmed again by the 2026-10-14 `a-sharp` control which
converges within 0.01 eV of the authored `k3-sp3-z` structure). Author
new seam scenes on the sp3 rows; the sp2 row remains as the analytic
preview construction.

sp3 siblings on the same contract: `k4-sp3-90-a` (four armchair sheets at
90°, one degree-4 seam atom per period, `sheet[1]`>=3 atom levels) and
`k3-sp3-z` (three zigzag sheets on a dimer seam chain, even `sheet[0]`);
`k3-sp3-a` (three armchair sheets at 120°, two seam atoms per armchair
period — one per dangling class, chained across periods; `sheet[1]`>=3;
the terminal seam atoms stay degree-three open ends) is the GFN2-xTB-
verified armchair sibling: the 2026-10-14 model holds its authored graph
with sheet bonds 1.52 Å, chain bonds 1.55 Å, tetrahedral seam atoms.

```json
{"op":"generate","generator":"hexfold_scene","name":"s",
 "params":{"sheet":[8,4],"k_tether":1.0,
 "features":[{"name":"s","type":"k3-sp3-a",
              "dihedrals_deg":[120,120,120]}]}}
```

An H cap landing under 1.0 Å from another atom is dropped outright
(`terminate.dropped`, bare edge).

## Seam tube: the 120° Y seam run along a tube axis

The strip joins the wall as the equal-120 sp2 k3 seam (the Y grammar)
with the two tube-wall halves as the other sheets, closed on the far
side by a zigzag fuse; the free wall settles with a cusp at the seam
(teardrop outward, notch inward).

```json
{"op":"generate","generator":"hexfold_scene","name":"y",
 "params":{"tube":[10,20],
 "features":[{"name":"y","type":"fin-k3-120-z","side":"out","rows":4}]}}
```

`tube` is `[rows, periods]`: row pairs per wall half (the zigzag seam
forces an armchair-type wall; two halves close to about the `(rows,rows)`
family) and seam periods; `side` and `rows` as for the sp3 fin. An inward
strip within 2 Å of the axis is refused naming the smallest wall that
seats it (strip rows 4 needs 17). One tethered stick
pass: seam atoms and their three neighbours pinned at the exact 120°
registration, strip tethered to its half-plane, **wall free** (no
cylinder tether), so `tube.cross_section` reports what the wall settled
into. `seam.rings`, `seam.geometry` and `geom.summary` are retained;
`surface_deviation` is unavailable. Preview geometry only.

## Rail tube: N inward ribs on equal-120 seams

```json
{"op":"generate","generator":"hexfold_scene","name":"y",
 "params":{"tube":[14,4],
 "features":[{"name":"y","type":"rail-k3-120-z","rails":2,"rows":3}]}}
```

A closed tube: `rails` (2–6) arcs meet at equal-120 seams, each seam's
third sheet an inward rib — no far-side fuse. Same `tube` (even rows),
`rows` (rib), `k_tether`; `side` is always `in` — `fin-k3-120-z` for
`out`. Optional `pores:[{"arc":k,"at":[u,v]}]` carves one hexagon each,
refused at a rim or another pore. Findings add `pore.rings` and `sites`
(open-valence rib/seam carbons, lumen normals, 2.5–3.0 Å pendant pairs).

## Channel junction: N arcs on paired k3-sp3-z seams, strips in or out

```json
{"op":"generate","generator":"hexfold_scene","name":"s",
 "params":{"tube":[4,6],"k_tether":1.0,
 "features":[{"name":"s","type":"chan-k3-sp3-z",
              "sides":["out","out","out"],"rows":3}]}}
```

`tube` is `[rows, periods]` as for the rail tube (even rows in [4,40],
periods in [2,60] — even, since every corner seam carries the k3-sp3-z
dimer chain). `sides` is a list of 2-6 'in'/'out' entries (default three
'out'); `rows` the strip width in [2,12]. Outward strips register the
seam half a sigma outside the wall circle with rim dangling atoms at
±120°; inward strips keep the rail-tube registration and refuse when
their free edge would come within 2 Å of the axis or a neighbouring
inward strip. Same contract as the seam rows: one tethered stick pass,
seams and neighbours pinned at the authored dimer registration, arcs
free; `seam.rings`, `seam.geometry`, `channel.cross_section` retained;
`surface_deviation` unavailable.

The equal-120 sp2 corner seams are **not** stable: the 2026-10-14
GFN2-xTB channel model (b-r4) dimerised all three corners into the
authored `k3-sp3-z` chain (0.07 eV off the pre-paired twin over 354
carbons). The fused-corner siblings (each corner's rim pair bonded, strip
grafted onto it) reconstruct instead — 12 of the authored fuse bonds
opened and re-paired into a half-zipped double graft; no authored
periodic form held, so no row.

## Reservoir tunnel: sheet | tube | sheet

The nanoreactor's reservoir stage closes a tube between two flat sheets:
each end seats into a hole the armchair opening solver cuts in its own
sheet (twist × scale scan on the rim's projected polygon), the dangling
atoms stitched to the boundary in cyclic order and defect-ring faces
minted (pentagons through nonagons where the lattices disagree;
`opening.face.open` names seam faces that could not close).

```json
{"op":"generate","generator":"hexfold_scene","name":"t",
 "params":{"tube":[6,6],
 "features":[{"name":"t","type":"tunnel-reservoirs","sheet":[30,30]}],
 "k_tether":1.0}}
```

`tube` is `[n, periods]` for an armchair `tube(n,n)` — other chiralities
refuse by name — or `{"rail":[rows,periods],"rails":N,"rows":M,"pores":[...]}`
for a rail tube, whose ribbed end follows the projected star outline.
The feature takes `name`, `type`, `sheet` only; `sheet` is
`[x_periods,row_pairs]` per reservoir sheet, refused when it cannot clear
the mouth polygon plus margin. One tethered stick pass: both sheets
plane-tethered, tube and junctions free. Findings: `opening.defects`
(pairs + defect census per mouth), `junction.strain` (seam-bond and
face-angle residuals), `seam.strain` (rail tubes: per-seam
bond, angle and pyramidalisation residuals), `termination.covered` / `geom.contacts` gates.
`topology["sites"]` labels junction atoms `fused`/`defect`/`unbonded`
and rail lumen/seam sites with (z, θ, normal) plus a `to_stored` frame.
The grammar special case lives at `hexfold/examples/tunnel_reservoirs.hx`
(a `hex(1)` hole fused to a `(6,6)` end mints six heptagons per mouth).
Analytic seed + tethered preview only; no stability claim.

## Fin on a tube: sp3 graft along an axial zigzag chain

The other exclusive scene entry grafts a single-layer graphene strip to
an armchair tube wall along the tube axis. It is the §11.1 `bond`
attachment, not a k3 seam: each grafted wall atom takes a fourth, radial
C–C bond (recorded sp3) to one dangling atom of the strip's zigzag edge,
one per lattice period (2.46 Å), so the strip stands perpendicular to
the wall, outward or inward. The unequal 180/90/90 sp2 seam stays refused.

```json
{"op":"generate","generator":"hexfold_scene","name":"f",
 "params":{"tube":[10,20],"k_tether":1.0,
 "features":[{"name":"f","type":"fin-sp3-z","side":"out","rows":4}]}}
```

`tube` is `[n, periods]`: an `(n,n)` tube, n in [4,40], axial periods in
[2,60]; `side` is `out` or `in`; `rows` (row pairs, default 4, about three
hexagon rows, 7.1 Å) in [2,12]. The two open end rims are not grafted, so
`periods` axial periods carry `periods − 1` grafts and `periods − 2`
six-cycles with two sp3 vertices each (`graft.rings`). An inward fin that
would come within 2 Å of the axis is refused naming the smallest tube
that seats it (rows 4 needs (16,16)). Analytic seed, one tethered stick
pass: wall atoms toward the cylinder, strip atoms toward its half-plane,
the grafted pairs pinned at their authored registration. `graft.geometry`
reports the measured bond angles at the sp3 hosts against 109.47°; at
the pinned registration they stay near 90–124°, which is the honest
residual of a radial graft on an unpuckered wall, not a relaxed
structure. A cylinder with a half-plane is no surface of revolution, so
stored `surface_deviation` is unavailable. Preview geometry only.


## See also

- [[precis-hexfold-help]] — the .hx spec, scene grammar, tops, refusals
- [[precis-se-atomic-help]] — the atomic generate op and its verbs
