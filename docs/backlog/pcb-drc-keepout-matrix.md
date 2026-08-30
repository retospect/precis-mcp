---
status: draft
title: DRC pair coverage — canary fixtures over the EXISTING rules (matrix collapse KILLED)
prio: high
model: opus
---

# The matrix was killed. Keep the diagnosis, drop the prescription.

**Original proposal (2026-08-29):** collapse six pairwise DRC rules into
one geometry adapter + a declarative `(class × class)` keep-out matrix with
a coverage test. **Reviewed by `fable` and rejected**, with code citations.
Recording the kill in full, because the idea is seductive and someone will
propose it again.

## The diagnosis was right and is worth keeping

Every DRC defect found 2026-08-29 was a **missing pair**: pad↔pour (every
pad shorted to the fill), pad↔track, pad↔via, pad↔pad, pad↔edge, via↔via
(two drills 0.16mm apart, 0.25mm wide). Six holes, same shape, none found
by review — all found by a human looking at a render. **Enumerability beats
vigilance.** That part stands.

## Why the collapse fails — six places the rules resist it

1. **The class list is a THIRD taxonomy.** The spec claimed alignment with
   settled decision 6 (`pad/hole/conductor/keepout/body/marking`) in the
   same sentence it proposed `copper/hole/silk/courtyard/edge` — which
   merges `pad` and `conductor`, renames `marking`, and invents `edge`.
   The merge is exactly where it breaks.
2. **"Same-net policy is per cell" is FALSE.** `check_via_pad_keepout` and
   `check_clearance` are both `copper × copper`, with opposite same-net
   policies and different tiering, selected by **subtype** pair. So cells
   must dispatch on subtype — i.e. the same six functions with extra
   indirection. `check_npth_clearance`'s plated filter splits `hole` the
   same way.
3. **"A drill spans the stack" is false.** `realize.py` emits blind/buried
   vias with real spans; two vias in disjoint spans may legitimately stack
   in x,y. The comparability predicate for holes is span-overlap.
4. **Silk is a phantom class** — zero silk rules in `drc.py`, silk is not
   in the DRC model, and avoidance is enforced at *construction* time in
   `silk.build_silk`. A silk row would duplicate a rule across two
   enforcement points: the exact drift disease this build keeps catching.
   Courtyards likewise arrive as a separate `courtyards=` argument, not off
   the model, and their exemption axis is **refdes**, not net.
5. **Emission arity and thresholds are per-rule.** `check_npth_clearance`
   emits one finding *per hole* (min over copper); clearance emits one per
   pair. Clearance's required gap is a **function of the pair's nets**
   resolved at check time, not a cap name. The edge cell's cap is chosen at
   runtime by `panel_type`.
6. **It would silently weaken the rule that is currently working.**
   `check_via_pad_keepout` uses a circumscribed circle **deliberately** —
   its own comment: overstating a pad costs a spurious finding a human
   sees; understating it hides a real via-on-pad. Unifying to the exact
   polygon yields *fewer* findings on the rule that just produced 55 of 57
   errors on the user's board. The spec's own acceptance criterion calls a
   drop in findings a regression, and its plan mandated one.

## The oracle argument was backwards

The spec claimed that generating the O(n²) oracle from the same table
means it "can only disagree about indexing, never scope — which is the only
thing an oracle is good for." Both halves are wrong. The oracle's real
historical catches (the `""`-layer via bucketing bug; its own
first-primitive layer bug) happened **because the two engines implement the
layer predicate independently**. Share the predicate and that entire class
of catch disappears — one implementation run twice.

Scope was never the oracle's job (the module docstring already says so).
**Coverage owns scope; an INDEPENDENT oracle owns geometry and indexing.**
The proposal weakened both legs and called it strengthening. Keep
`clearance_violations_naive` exactly as it is: no shapely, closed-form, its
own layer logic.

## Wrong week, structurally

Geometric DRC is currently **the only independent check on the maze
router's occupancy-grid guarantee**, the DRC baseline moved three times in
one day, and several agents are editing adjacent files. Rewriting the
verifier while everything it verifies is in motion makes a router
regression and a checker regression indistinguishable — and pins acceptance
against a baseline that is legitimately changing daily.

## DO THIS INSTEAD — the separable 20%

1. **Canary-fixture coverage test over the EXISTING functions.** No
   refactor. Enumerate the real pair universe **by subtype** —
   track / pour / pad / via-annulus / via-drill / NPTH / THT-drill / edge /
   courtyard — and for each pair either a minimal violating fixture
   asserting ≥1 finding of the right rule, or an explicit n/a **with a
   reason a reviewer can check**. "Two pads 0.01mm apart, different nets →
   expect a finding" would have caught **all six** of today's misses
   against the code as it already stood. This is where the whole value was;
   the collapse was never load-bearing for it.
   - Include the pair families the matrix itself forgot: **`soldermask_dam_mm`
     and `silk_width_mm` exist in `capabilities.py` with ZERO consumers in
     `src/precis/pcb/` — an entire pairwise rule family with no cell to be
     missing from.** Enumerating the wrong universe proves completeness of
     the wrong universe.
   - A test that passes the moment someone types `NA` is worse than no
     test: it converts absence of evidence into apparent evidence of
     absence, wearing a green tick.
2. **Ship via↔via as an ordinary `check_*` function** — and note it needs a
   **new capability field** (`hole_to_hole_mm`); `capabilities.FIELDS` has
   `drill_mm`, a minimum drill *size*, which is a different quantity. The
   matrix never removed this work.
3. **Keep the oracle independent.** Generate it from nothing.
4. **Revisit the geometry-class collapse only after engine-plan S9** (the
   vocabulary migration), so any DRC taxonomy is decision 6's enum rather
   than a third one — and evaluate the `via_pad_keepout` pad-shape question
   then, as its own measured decision, never as a rider.
