---
status: idea
pillar: 3d-design
title: tube(n,m) reports its chirality class and electronic rule, and a fit domain can select one
---

# hexfold: `tube(n,m)` names its kind (zigzag / armchair / chiral) and its electronic class

Reto, 2026-10-09 (via chat-interface, pasting the standard (n,m) table):
"when we specify cnt we should pick one, or it should tell us which kind
it is, and explain basic property." Owner: the hexfold build
(`src/hexfold/build.py` `_tube_patch`, `Patch.tube_nm`) and the se block
the precis bridge mints from it (`src/precis_se/atomic/join.py`
`block_from_net`). Skill: `precis-hexfold-help` §Rim types already says
`tube(n,0)` ends are zigzag and `tube(n,n)` armchair, but only as rim
typing for fuse; nothing tells the author what tube they built.

## What

**Report.** Every `tube(n,m,…)` instance (and every roll-up a `fit in
{…}` domain resolves to) emits one `tube.class` INFO finding and a
`tube` entry in the block's generated metadata:

- `class`: `zigzag` (m = 0), `armchair` (n = m), `chiral` (0 < m < n);
  `hand` (`+`/`−`) for chiral tubes, from the `hand=` argument or, for a
  written `(n,m)` with m > n, the mirror of `(m,n)` as
  `tests/test_canon_text.py` already treats it (reported, not an error).
- `chiral_angle_deg` = atan(√3·m / (2n + m)); 0° zigzag, 30° armchair.
- `diameter_a` from the existing `lattice.tube_radius`.
- `electronic`: `metallic` when (n − m) mod 3 == 0 (all armchair; zigzag
  with n divisible by 3), else `semiconducting`. Zone-folding rule, one
  wall. Zigzag and chiral "metallic" tubes carry the qualifier
  `nearly-metallic (curvature gap)`; below ~0.8 nm diameter the finding
  says the simple rule is unreliable. Only when both lattice elements are
  C; hBN or mixed lattices get `class` and geometry, no `electronic`.
- Tags on the structure: `tube:<class>`, `electronic:<class>` so
  `search(kind='se')` can pull "all metallic tubes".

**Pick one — the type is meta on the tube, (n,m) is resolved by fit.**
Reto, 2026-10-09: the specification sits on the tube, but the adapter has
to fit the sheet and the ball has to fit the tube, so a fixed (n,m) is
the wrong authoring unit. The author states the *type* and lets the fit
pick the integers: `tube(fit, electronic=metallic, len=3)`,
`tube(fit, class=armchair)`, `tube(fit, class=chiral, hand=+)`,
`tube(fit in {(5,5),(6,6),(9,0),(10,0)}, electronic=metallic)`.
`class=` takes `zigzag | armchair | chiral` (`helical` is an alias of
`chiral`); `hand=+|−` is the spec §264 parameter the grammar already
accepts on `tube` and only means anything with `class=chiral` (or a fixed
0 < m < n): `hand` on a zigzag or armchair type is a WARN, and
`class=chiral` without `hand` resolves to `+` and says so in the
finding. `class=`, `hand=` and `electronic=` are domain filters in
`hexfold.domains`: they prune the roll-up domain *before* arc consistency
prunes it by `N` against the neighbours (cap, cone, sheet hole, ball
attach), and `fit.propagated` records the type prune as its own
`pruned_by` step. An emptied domain is the existing `fit.unsolvable`,
naming the type and the `N` the neighbours needed. A fixed
`tube(10,0, electronic=metallic)` is an ERROR (`tube.class.mismatch`)
naming the nearest satisfying (n,m). Same meta on the surface-then-tile
path ([hexfold-ideal-surface-then-tile](hexfold-ideal-surface-then-tile.md)):
the tube feature of the authored surface carries `class=`/`electronic=`,
and the tiler chooses the (n,m) that matches the authored radius and the
type.

Expected rows (the test table): (10,0) zigzag semiconducting; (12,0)
zigzag nearly-metallic with the curvature note; (6,6) armchair metallic;
(6,4) chiral semiconducting; (6,3) chiral nearly-metallic; (6,3) vs
(3,6) same class, opposite hand; `tube(fit, class=chiral, hand=−)` picks a
chiral pair and reports `hand: −`; `tube(fit, class=armchair, hand=+)` WARNs; `element=B,N` lattice → no
`electronic` key.

**Solver changes the meta needs** (Reto + agent, 2026-10-09, after
reading `hexfold.domains`): the bare `fit` catalogue holds only `(n,0)`
and `(n,n)`, so `class=chiral` on bare `fit` is empty by construction —
`class=chiral` (or `hand=`) widens the catalogue to every `0 < m < n` up
to the same radius cap. `fit.alternatives` carries each candidate's
`class`, `hand` and `electronic` beside its cost, so a C60-capped
`tube(fit, electronic=metallic)` reports `(5,5)` and `(8,2)` as the two
answers rather than two integer pairs. A collar on a tube needs
`k | gcd(n,m)`; chiral picks with gcd 1 therefore refuse collars with the
existing `port.symmetry`, and the alternatives list says which picks
admit one.

## Why

A tube is the one hexfold primitive whose two integers encode a physical
property the author cares about (the nanoreactor and nanobud chains both
assume a conducting or non-conducting wall) and nothing in the build
output says which one was made. The rule is three lines of integer
arithmetic on numbers the build already holds.

## Not in scope

Any band-structure calculation; multi-wall tubes; the curvature gap's
value (only the qualifier). The `electronic=` selector on anything but
`tube`.

test: `tests/test_build.py` table above; `precis-hexfold-help` gains the
class table with the four pasted examples; `scripts/test -k tube_class`.
