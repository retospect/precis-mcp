---
status: idea
pillar: 3d-design
title: A typed instrumentation leg on an unequal 3-sheet seam
---

# A typed instrumentation leg on an unequal 3-sheet seam

What: Reto's torus (product-plan review, 2026-09-30) — a 3-sheet sp²
join where two sheets carry structure and the third leg carries
instrumentation (a sensing/actuation port), rather than three structural
sheets. `hexfold-seam-type-catalogue.md` records Reto's ruling: "joining 3
sheets unequally we just don't do yet" — the catalogue's `k=3 unequal` row
is explicitly OUT ("Refuse, do not approximate"). The shipped "flanged
doughnut" case is a k=3 seam of three *equal* sheets, which the catalogue
does cover.

Why: the blanket "unequal — refuse" ruling is correct for the general case
(no principled way to decide seam geometry when sheet roles differ
arbitrarily), but a leg typed specifically as *instrumentation* — not
carrying structural load, only a port — is a narrower case than "unequal
structural sheets" and may not need the same refusal. Worth a typed
exception (a port-role leg on an otherwise-equal seam) rather than
reopening the general unequal-join question.

Owner anchor: `docs/backlog/hexfold-seam-type-catalogue.md` (the k=3 row
this item proposes narrowing, not overturning); `hexfold-sp3-seam.md` (the
three/four-sheet sp³ join this sits next to).

test: none yet — idea stage. A testable slice would be one seam instance
with two equal structural sheets + one leg tagged `role=instrumentation`,
building and `check`-passing under a narrowed catalogue rule, while a
three-*structural*-unequal-sheet seam still refuses.

Closest existing items: `hexfold-sp3-seam.md`, `hexfold-seam-type-catalogue.md`
(the ruling this item proposes a narrow exception to), `rotary-ratchet-valve.md`.
Blocked-by: `hexfold-integration.md`.
