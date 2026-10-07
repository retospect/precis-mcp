---
id: precis-hexfold-help
title: precis — the hexfold generator (curved sp² carbon from a .hx spec)
summary: generate atomic se blocks — sheets, tubes, cones, fullerenes, holes, fused joints, nanobuds — from a topology-only .hx spec text via generator='hexfold'; coordinates are derived, the spec is the regeneration input; fidelity='check' returns the check report without minting; generator='hexfold_scene' tiles an authored smooth surface (sheet + fillet/tube/lid/sphere features) tethered to it
answers:
  - how do I generate a nanotube/cone/fullerene/nanobud block from a hexfold spec?
  - what does a .hx spec look like and which nanobud menus exist?
  - how do I check a hexfold spec without minting a block?
  - what do the hexfold check codes mean?
  - how do I tile an authored smooth surface (sheet with tube features) with carbon?
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
exists. A refused spec comes back as `line:col: message` (ParseError —
including `unknown parameter 'length' for tube — known: hand, len, m, n`
for a keyword outside the primitive's vocabulary) or as the rendered
report (ERROR findings); `hexfold internal error …` means a compiler bug —
file a gripe with the spec.

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
`geom.summary`/`geom.bond.*`/`geom.angle.dev`/`geom.join.*` INFO/WARN
(`geom.summary` also counts every bond/corner past tolerance, `bond_n_over`
/`angle_n_over`, beside rms and max: the findings list stays capped at ten, and
the stored report text of plain `hexfold` builds carries the count; geometry
reports from before hexfold 0.3.1 list at most 10 entries per code and carry no
`n_over` totals) ·
`geom.clash` ERROR under 1.0 Å (overlapping atoms), WARN from 1.0 to
1.8 Å (two non-bonded atoms in the stick geometry, both instances and
elements named with the pair's bar; H–H has its own 1.5 Å bar because a
terminated armchair bay holds its two H at 1.87–1.89 Å; a clean check
without it is not a clean geometry). Nanobud menus
(`[9-6]`, `[8-7]`, `[2+2]`) seed the C60 outside its host; on a flat host
(sheet, lid) a trailing `face=up|down` on the menu line (or inside a
`--bond face=down-->` arrow) picks the face, per bud, so a sheet can
carry one on each side (`place.face_authored` INFO; on a tube it is
ignored with `place.face_ignored` WARN). `[9-6]` and
`[8-7]` keep WARN clashes of 1.2–1.5 Å at the junction neck. These
shipped specs still overlap, are known, and are not yet fixed:
`capped_tube`, `capped_tube_da_neck`, `sheet_pill_bump` (0.90–0.92 Å,
curved rims seeded mirrored, gr459812), `tube_ring_closure` (0.49 Å,
gr462074) and `flanged_doughnut` (0.27 Å inside a washer, gr462075).
`geom.seed_overlap` ERROR: atoms under 0.7 Å in the placed seed, before
stick. A clean relaxed geometry does not clear it. Raised by
`tube_ring_closure`, `sheet_sw`, `flanged_doughnut` and
`sheet_pill_bump` (their seeds stack atoms; `sheet_sw`, gr462144, relaxes
clean anyway). Their ERROR is real ·
`annot.sublattice`/`annot.host_sublattices` INFO.

## Rim types (spec §10)

A rim's type is `(kind, N)` from its dangling pattern: `tube(n,0)` ends
are zigzag `z<n>`, `tube(n,n)` ends armchair `a<2n>`, chiral ends and
cap/hole rims are mixed. Fuse needs equal `N` only; zigzag onto
armchair at equal `N` is the 30° grain-boundary adapter (5-7 seam
rings). Prefer zigzag `N` in multiples of 6 so caps, washers and lids
interoperate; `rim.nonstandard` INFO marks the rest. se ports carry it
as `lattice="sp2-hex"`, `payload={kind: rim, word, N, type}`.

**Read `type`, not `word`, for the rim family.** `word` is the rim's
*turn* word (one symbol per edge, `a` only at a genuine 120° corner such
as a flake's), and a hex-lattice rim turns ±60° everywhere — so a zigzag
and an armchair rim of the same `N` both read `z<2N>` (`z12` and `a12`
rims alike carry `word: "z24"`). The families differ in the turn signs'
*phase*, which the word discards. Spec §10's erratum says the same.

## Joining resolved blocks (spec §22.2)

`join` composes two already-**generated** blocks (each its own `generate`
call, independently minted) into a new composite block over a matched
port pair — a block-level equivalent of `fuse`, for when the two parts
were never in the same spec:

```json
{"op": "join", "name": "composite", "a": "tube_a.out", "b": "tube_b.in",
 "seam": "auto", "k": 0, "seam_radius": {"a": 8, "b": 2}, "rung": "auto"}
```

`a`/`b` are `<block>.<port>` — either side may itself be an earlier
join's composite (a composite is a resolved block too, so a chain of
joins works). `seam` is `auto` (default; `fuse` for equal rim types,
`adapter` for a zigzag↔armchair grain boundary — an explicit value that
disagrees is `seam.mismatch`), `k` a phase (`0..N-1`, or `"fit"` — ranked
the same way `fuse`'s `k=fit` is, surfacing `fit.alternatives`).
`seam_radius` overrides the per-side shell radius the re-relax touches
(table default by rim type, zigzag 8 / armchair 2 shells); dispatch is by
the two ports' `lattice` tag *pair* (a sorted 2-tuple, `join.lattice` if
either is absent or the pair has no registered joiner — today only
`("sp2-hex", "sp2-hex")`, two hexfold rims, is wired).

## What a join mints (spec §22.2)

The composite is a new atomic block, envelope a bounding cylinder in
`a`'s own frame (`a`'s atoms untouched; `b`'s are rigidly placed), bound
to a fresh `structure` design holding `a`'s atoms then `b`'s. `a`/`b`
become its children (re-parented, `b` gets a `set_pose`) and keep their
own refs — a regenerate never patches a composite, it names the parts'
block/structure/version in the build record instead. The consumed
ports (`a`'s `pa`, `b`'s `pb`) get one `connect kind='bond'` recording
the seam; every OTHER port on `a`/`b` becomes a composite port
(`<block>_<port>`, `b`'s carried over with its direction rotated).

## Join findings (spec §22.2)

Findings land in the build record (`view='block'` → "## generated
(join)"): `seam.sigma` WARN (`a`/`b` built at different bond-length
`sigma` — the seam places and re-relaxes with `a`'s sigma only, straining
`b`'s bonds; regenerate one side onto a shared sigma), `seam.element`
WARN (the two rims carry different elements, `data.a_elements`/
`b_elements` — the seam bonds them as one material; regenerate one side
onto a shared element, or accept the heterojunction), `seam.rings`
(census), `seam.adapter`/`seam.strain` INFO, `seam.leak` WARN (re-relax
perturbed geometry past the seam radius, naming which of `|dl|`/
`|dtheta|` breached and its threshold — raise `seam_radius` or resolve a
longer block), `seam.terminated` INFO (a non-carbon atom inside the
re-relaxed sub-graph), `port.mismatch` ERROR (rim sizes differ — nothing
minted), `join.part_addressed` ERROR (an endpoint names a block already
claimed as a *part* of another composite — a part may not belong to two
composites; the message redirects to the owning composite's own
already-exposed port, e.g. `chain3.tube_c_out` instead of `tube_c.out`,
walking the full nested prefix when the part sits several joins deep;
raised before anything else, even lattice checks), `join.lattice` ERROR
(a port has no `lattice` annotation — minted only when a block is
generated, so regenerate it through its own `generate` op; or no
`JOINERS` entry for the pair, which also covers a genuine mismatch
naming both values), `join.stale` ERROR (the block's stored atoms no
longer agree with a rebuild of its own generator record — regenerate
first), `join.rung` ERROR (the two parts' relax rungs disagree and
`rung` wasn't forced) or WARN (`rung` forced `geo` over a stick-rung
part), `join.pose_dropped` INFO (`b` carried a pose/rot before the join;
a join places `b` by the seam transform, so it is discarded) and
`join.reparented` INFO (`b` was authored under an ordinary layout parent
and moves into the composite, discarding that parent — `a`'s parent
survives, inherited by the composite; the two-composite case is the
`join.part_addressed` ERROR above, not this).

## Join relax rungs (spec §22.2)

Both the **stick** and **geo** relax rungs are wired. `rung` picks which
(default `auto`: both parts' own `meta['last_relax']['rung']` must agree
— absent means stick, the generator's untouched preview geometry — or
it's `join.rung` ERROR before anything is minted; an explicit
`"stick"`/`"geo"` forces one, and forcing `geo` over a still-stick-rung
part is allowed but logs `join.rung` WARN, since 1.42 vs 1.52 Å rest
lengths strain the frozen boundary). The geo rung's `seam.leak` checks
against its own measured thresholds (0.002 Å / 0.15°, uniform across rim
type — different physics from the stick numbers above, a pinned guard
band vs. a fully free relax). `meta['generated']['relaxer']` records
which rung actually ran. Joint placement across a part-graph *cycle* is
still a later slice (three-or-more-way joins today only chain linearly,
one pair at a time).

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

## Chains of parts (spec §22.3, §28.4)

Above the spec text, `hexfold.chain.solve(parts, bottom=, top=, wish_A=)`
composes *parts* — abstract records, not instances: `Part(name, kind,
value=|domain=|<don't-care>, periods=|periods_range=)`, `Part(name,
"spacer", length_A=(min, max))`, and a resolved block via
`part_from_payloads(name, in_payload, out_payload, length_A)` from its
ports' `payload` and its `<inst>_len` measure. Every adjacency equates
rim `N`; pin either end (`bottom=Rim(12,"z")`) and the middle resolves
(`chain.propagated` / `chain.unsolvable`); then free periods and spacers
land the total nearest `wish_A=(target, band)` (`chain.length` when
nothing can). `ChainResult.best` is the ranked winner: deviation, then
fewest 30° adapters (`chain.adapter`), then periods. Geometry comes from
a backend; the stub answers from tables, no build — the same solver
later runs over smooth collars and sp³ blocks. No se op yet.

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

## Authored-surface scenes — `generator='hexfold_scene'`

Tiles a *drawn* smooth surface with carbon: the surface is fixed, the
atoms follow it. A flat sheet carries features; each is a fillet of
`radius` Å into an `(n,0)` zigzag tube of `tube_len` periods, then a
`top`. Plain `hexfold` is unchanged and relaxes untethered.

```json
{"op": "generate", "generator": "hexfold_scene", "name": "scene",
 "params": {"sheet": [30, 24],
  "features": [
   {"name": "t", "at": [13, 15], "n": 6,  "radius": 3.0, "tube_len": 5, "top": "lid"},
   {"name": "q", "at": [20, 6],  "n": 12, "radius": 5.0, "tube_len": 1, "top": "lid"}]}}
```

- `sheet` `[w, h]`; `at` `[i, j]` = the sheet cell of the feature's hole;
  `n` the tube's `(n,0)`; `top` = `open` | `lid` (flat `cap(n,0)`, `n` a
  multiple of 6) | `ball` (C60 fused through a hexagon hole, `(6,0)` only) |
  `sphere` (an authored round top, below).
- Instances: `name` is the tube, `<name>f` the frustum, `<name>c` the top;
  the sheet is `s`. `extra` (optional) = verbatim `.hx` lines, e.g. buds:
  `"extra": "d: fullerene(C60)\nd @ s/(32,22,A):0 [2+2]"`.
- `k_tether` (default 1.0): normal-tether stiffness to the surface.
- Each foot is a 3+3 heptagon foot; the planner measures the frustum
  width `k` (narrowest that meets the bars), then relaxes under the tether.
- One synchronous call, ~35 s for 3 features / ~2.9k atoms: one scene per
  call.

Read in the result: `plan` (ks, per-feature rows, tops), `scene` (the
params — the regeneration input), `geom.summary.relax = "tethered"`.
Judged per feature: fillet-zone deviation (mean <= 0.10 Å, max <= 0.3 Å),
bonds 1.36–1.50 Å, ring-ideal angles, pyramidalisation. **Read bonds,
angles and pyramidalisation first** — deviation is small by construction
(tether and judge target the same surface), not independent evidence. A
missed bar mints with WARN `scene.bar` naming the feature; ERROR
`geom.clash`/`geom.seed_overlap` stay ERROR.

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
neighbours pinned, outer sheet atoms tethered to their sheet plane. The central seam atoms and immediate neighbours stay
pinned at their authored registration; remaining sheet atoms relax.
`seam.rings`, `seam.geometry`
and `geom.summary` are retained in the block report. Read the actual bond
and ring-angle findings; regular-polygon eight-cycle warnings are not
hidden. This is preview geometry, not strength or stability evidence.
Replay `generated.scene` params; this entry does not generate hx text.
Three planes are unsupported by the revolution target format, so stored
`surface_deviation` is honestly unavailable. Unequal dihedrals/k>=5 return
`fit.unsolvable`; tube internal rails and variant B remain unimplemented.

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

## Round tops — `top: "sphere"` and a rounded `top: "lid"`

The tube's
surface carries on past the tube as an authored fillet and a sphere (or a
hemisphere) and every top atom is held to it, like the foot.

```json
{"name": "q", "at": [15, 15], "n": 12, "radius": 5.0, "tube_len": 3,
 "top": "sphere", "top_R": 10.0, "top_fillet": 4.0}
{"name": "q", "at": [15, 15], "n": 12, "radius": 5.0, "tube_len": 3,
 "top": "lid", "top_fillet": 4.69}
```

- `sphere`: `n` a multiple of 6, `n >= 12`; any other `n` is refused with the
  reason. `top_R` (Å) and `top_fillet` (Å) are optional. A washer
  (`cap(6k,0) - hex(n/6-1)`, six heptagons), a `(6k,0)` bulge of `L`
  periods and a `cap(6k,0)` lid sit on the tube.
- `lid` + `top_fillet` rounds today's flat lid toward a hemisphere of the
  tube's radius `r`; `top_fillet` is at most `r`. Without `top_fillet` the lid
  is the flat lid, byte for byte. `top_R` belongs to `sphere` only.
- The planner (`plan_top`) builds each candidate (`k` in `r+3..r+5` with
  `r = n/6 - 1`, `L` in 1..3; a lid's candidates are how many tube atom rows
  the dome takes in) and keeps one that meets **five bars**: 0 ERROR, no
  non-bonded pair under 1.34 Å, tethered deviation p95 <= 0.3 Å, top bonds
  < 1.7 Å, θp max <= 12° (C60 is 11.6°). Among those it prefers the smallest
  relaxed p95 (below), then the narrowest `k`, then the shortest `L`. `L = 0`
  is never a candidate (it tears the net).
- `top_R` is a request: R is area-matched to the chosen build's atoms, and an
  authored `top_R` picks the `(k, L)` whose area-matched R is nearest.
  `top_fillet` default = `min(1.5 × R_min, R − r)` and at least 2 Å, where
  `R_min` is an analytic conservative heuristic using the existing 12° bar
  and both shoulder curvature magnitudes (hoop `1/r` plus `1/R_t`), not a
  necessary physical stability bound. At `n = 12`:
  `R_min` 2.70 Å, so the default is 1.5 × 2.70 = 4.05 Å, under the room cap
  `R − r` (4.3 Å at R 9.0). An authored `top_fillet` above that room is
  refused by name.

## Check sphere fillets and read stored top diagnostics

- Explicit sphere `top_fillet` below finite `R_min` is newly refused before
  planning as a **conservative authored-fillet input policy**. Equality
  clears only this check; other room/geometry limits remain. Input must be
  positive finite numeric (no bool/string/NaN/infinity). Omitted room-capped
  defaults may fall below the heuristic and remain unchanged; lids keep their
  existing behavior. No clamp or physical stability verdict.
- `view='block'` adds stored top diagnostics: dedicated
  `scene.top.theta_p_band`, actual/limit in degrees and pass/miss/unknown
  from recorded tethered **scene** measurements, with analytic `R_min` in Å
  separately labelled. This read never regenerates or relaxes; missing,
  nonfinite or failed-build rows are unknown, never trial/grid zero passes.
  Existing saved report/findings remain unchanged. New generated reports
  also carry the dedicated finding (INFO pass/unknown, WARN miss).
- `plan["top_plans"][name]` stores `k`, `L`, the realised `R` and `fillet`,
  the tethered deviation p95 and θp max, `bars_met`, the relaxed p95, and the
  whole candidate grid. WARNs: `scene.top.R_mismatch` (realised R more than
  0.5 Å from the authored `top_R`), `scene.top.bar` (a bar missed on the
  scene), `scene.top.relaxed_shape`.
- **The stored shape is the tethered one.** `plan_top` also relaxes each
  candidate once with the tether off and measures its p95 distance from the
  authored surface; over 0.5 Å (the washer sphere on `n = 12` measures ~0.9 Å
  and drops the pole ~0.9 Å under the stick relax; MACE-MP small shows ~2 Å)
  the block carries `scene.top.relaxed_shape`: "tethered geometry stored;
  relaxes ~X Å flatter at the pole". It is judged on the planner's bare
  trial tube (no sheet); `scene.top.bar` uses the scene re-measurement.
  The stick number is a lower bound and does not rank tops: MACE separates
  tops the stick relax scores alike.
- Cost: **tabled tops are free** — a sphere with the default fillet at
  `n` = 12/18/24/30/36, and a lid with `top_fillet` = r rounded down to
  0.01 Å (4.69 at `n = 12`, 7.04 at 18, 9.39 at 24) come from the plan table
  (`planned: "table"`). Any other top plans live: a sphere ~35 s at
  `n = 12` (9 candidates); a lid ~20 s (4). The scene relax is not tabled:
  a scene with one `n = 12` sphere takes ~55 s. Refused before planning:
  more than one `sphere` per scene op or one with `n > 12`, tabled or not
  (spheres at 12 and 24 in one op relaxed 476 s); or live candidates over
  16 (sphere 9, each distinct `(n, top_fillet)` lid 4). Limits are per
  scene op: put **one round-top scene op per put** (two ran 143 s on prod).
- Grammar (the CAD-style spec layer maps 1:1): `ball_on(R)` ↔ `top: sphere,
  top_R`; `round(r)` ↔ `top_fillet`; `lid_on` + `round(r)` ↔ `top: lid,
  top_fillet`.

## Scene refusals and limits

Refused (`GeneratorError`): a hole cell whose sheet seam is not the planned
three heptagons (hexfold fuse-phase fault, gr464341 — move the hole one
cell, esp. away from the 9→10 index boundary); features whose discs
overlap (reach = tube radius + `radius` + 1.5 Å); build errors.

Limits:

- `top="ball"` always carries WARN `scene.top.joint`: the fused (6,0)→C60
  neck is stick geometry only; MACE-MP small and GFN2-xTB both open 4 of
  its 6 seam bonds (gr464391). Bonded alternative: a `(12,0)` feature with `top="lid"` and the C60
  as a sidewall `[2+2]` bud just under the lid, via `extra`:
  `"b: fullerene(C60)\nb @ <name>/(4,-7,A):0 [2+2]"` (the `(4,-7,A)` site is
  for `tube_len=4`; it moves with the tube length). It passes both
  relaxers as an isolated pillar only; inside a tethered scene its seam
  still opens (MACE-MP 2.6/3.6 Å), so it is not a scene recipe yet.
- `(18,0)` feet crumple (an open row). `k=10` frustums (e.g. `(24,0)`) seed
  3 atoms onto the sheet: ERROR `geom.seed_overlap` (gr464358).
- Fillet `radius` reaches ~3–8 Å at the narrowest frustum; beyond ~12 Å
  bonds stretch past 1.50.

## Rules

- Counting is a diagnostic, not a gate; `geom.*` describes the stick
  preview, not matter.
- Coordinates are derived; the spec string (`topology.spec`) is the
  regeneration input.
- Bond-mode attachment sites come out `sp3` — bonds touching them get
  order 1; sp²–sp² bonds get the Pauling 4/3 aromatic order.
