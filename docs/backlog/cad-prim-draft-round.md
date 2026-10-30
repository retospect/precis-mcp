---
status: draft
title: "cad DSL prim payloads: draft key on box/cyl + a fillet/round payload (molding)"
pillar: 3d-design
---

# cad DSL: draft and round prim payloads

Source gripes: gr529022, gr524645.

Two paired prim-payload additions, ruled together by Reto 2026-10-28:
prim payloads, not new prim types and not mold-op derivations.

## Motivation / why

**Draft (gr529022).** se-box3's molding variant wants 0.5–1°/side draft
on the enclosure walls and gusset ribs (boss-design skill: OD/ribs
0.5–1°/side, bore 0.25–0.5°). The only draft key in the DSL is relief's
`a`; box/cyl carry none and no tapered-box prim exists. The shipped
workaround models the bosses as tcones (rb>rt, 0.75°/side, verified by
ray probes at two heights), but the 3 mm walls, the lid plate/lip and
the 1.5 mm gussets run straight — box3-molding as published is not fully
mold-ready. A chamfer/rd edge treatment cannot close this: draft is a
face taper, not an edge treatment.

**Round (gr524645).** se-box2 needs R6/R3 concentric rounded corners —
constant 3 mm wall on the arc. The rounded shell is necessarily
cut-modeled (union of two bars + four corner cylinders, minus the
cavity), and the mold op refuses cut-node parts ("model the part
additively"). The corner ring segment is a quarter-annulus, and a union
of boxes/cylinders/frustums cannot produce an inner concave arc — unions
only add material — so rounded-corner parts can never mold today. Sharp-
corner additive boxes (se-box v1, boxmake-molding) mold fine; this is
purely the rounding gap.

## In scope

- A `draft` key plus a pull direction on the `box` and `cyl` prims —
  ruled by Reto 2026-10-28 ("prim payloads"; there is no separate tbox
  prim).
- A fillet/round payload in the cad DSL — an edge treatment like the
  existing chamfer payload but radius; explicit and inspectable, usable
  for FDM too (same ruling).

## Explicitly NOT in scope

- A separate `tbox` prim (ruled out).
- Teaching the mold op to fold or derive rounds — "mold ops do not
  derive rounds"; the op stays additive-only and cut-modeled parts stay
  refused (gr524645's option 2, ruled out).
- Draft on prims other than box/cyl.

## Acceptance criteria

- A box or cyl carrying `draft` plus a pull direction tapers the
  relevant faces by the declared angle; the same prim without the key is
  unchanged.
- A round payload on a box yields concentric rounded corners (the
  se-box2 R6/R3 case) as an additive model the mold op accepts.
- The payloads are inspectable — the declared draft/round is readable
  back (gr524645 notes a round payload could carry the corner radius
  into the envelope tier).

## Target + blast radius

cad DSL grammar and prim fold (`src/precis/cad/`), the mold op and
`mold.undercut`, the viewer/envelope tier; FDM designs may also use the
round payload.

## Open questions / decisions log

- **[decided, Reto 2026-10-28 via GRIPE_FIX]** Prim payloads: box and
  cyl get a draft key plus a pull direction; no separate tbox prim.
  Pairs with the round payload.
- **[decided, Reto 2026-10-28 via GRIPE_FIX]** A fillet/round payload in
  the cad DSL, explicit and inspectable, usable for FDM too; mold ops do
  not derive rounds. Pairs with the draft key.
- Open: key spellings and direction convention (`draft=` + `pull=`?),
  and interaction order with `rd`/`chamfer` when several treatments land
  on one prim.
- Open: does the round payload carry its radius into the envelope tier
  (the gripe notes it could fix the viewer/envelope story)?
