---
status: draft
title: situation rule tables — three-verdict pair checks over swept volumes in se drc
prio: normal
blocked-by: design-state-core
---

# Situation rule tables — three-verdict pair checks over swept volumes

Build-order step 3's "three-verdict scenario rule table" (map
`multiscale-design-architecture.md` §Scenarios;
`multiscale-design-addendum-a.md` A3, the lifecycle-situations delta).
Vocabulary per glossary ruling 2026-09-12: these are **situations** (named
swept-volume bundles: assembly, operation, maintenance, shipping, …) —
the §1.3 **Scenario** (production context: prototype / mass_production)
is a different object and is untouched here.

## Motivation / why

A part owns several volumes, not one: static occupied, assembly-swept
(shape × insertion path), tool-access-swept, operational 4D motion
envelope. Today's machinery (`precis.cad.relate.clearance`,
`precis_se.drc`) can only *forbid* overlap between static envelopes, so
three whole classes of defect are invisible: (1) a **hard stop that
fails to seat** — contact was *required*, and no clearance check can say
so; (2) parts that clear statically but collide along their insertion
or motion sweeps; (3) parts that can only avoid each other via assembly
*ordering*, which is a sequence fact no per-pair static check can
express. The lifecycle situation list is *n*, not three, and never a
hardcoded enum. Spec §2.3 already writes two checks as IOUs against
this mechanism ("clamping faces … as a scenario row", "test/probe
access — same scenario-row mechanism"); this item builds the mechanism.

## In scope

1. **Situation record** (table home: `src/precis/design/` — the schema
   *stub* ships with `design-state-core.md`; this item fills it):
   `name` (free text, open set) · per-block **active volume set** —
   which of {static, assembly-swept, tool-access-swept, motion
   envelope} participates, each a named volume · **allowed
   configuration set** — joint-state ranges/samples in the shipped
   `expand_instances(..., state={joint: q})` vocabulary
   (`precis.cad.scene`). A swept volume is built as the union over
   sampled configurations (min-over-poses SDF) — no new kernel
   primitive; it rides the pose machinery that exists. **Standing seed
   set** (addendum A3): static assembled, operational 4D motion,
   assembly, maintenance/diagnostic, fixturing, test/probe access,
   packaging, end-of-life — an open catalogue an se design starts with,
   never a hardcoded enum.
2. **Three-verdict pair-rule table** per situation, over volume pairs —
   all situations are rows in **one table, evaluated in a single pass**
   (addendum A3): fixturing, probe access and packaging already share
   this shape (spec §2.3); operational motion and maintenance join the
   same table rather than becoming their own subsystems. Verdicts:
   `must_clear` (overlap ⇒ violation) · `may_touch` (no check) ·
   `must_contact` (signed gap must be ≈ 0 — a hard stop, seat, or
   preload face that does NOT touch is a violation, symmetric with one
   that interferes). `must_contact` is the genuinely new verdict, and is
   the built form of addendum A3's `LifecycleCase.contact_allowed[]`
   field (interfaces where contact is intended) — expressed per pair
   rather than as a per-case list, since the pair is this table's
   addressable unit. Verdicts evaluate via `relate.clearance` (signed-gap
   `ClearanceResult`) and land as findings in `precis_se.drc`
   (`view='drc'`), reporting which situation governed.
3. **Cross-configuration queries**: for a pair active in several
   configurations, *clears in every configuration* ⇒ safe; *clears only
   in some* ⇒ an **assembly-ordering constraint**, extracted and
   reported as data (partial order over blocks). Consuming the ordering
   as a discrete optimiser variable is map §Optimisation, not here.
4. **Removability** = sweep the part along a candidate path, intersect
   against the union of the other active statics (empty ⇒ path valid),
   then a free-space **escape test against a NAMED bounding volume** —
   named because a concave parent assembly's wider machine can trap a
   part that cleared its subassembly's box. Disassembly = plan, then
   reverse.
5. **Volume-equivalence comparison** for dynamic configurations: two
   configurations are equivalent for a rule-table check when their
   active swept volumes are — dedupe check work by volume, not by
   configuration identity.

Slices (each independently shippable):
- **Slice 1** — situation records + `must_clear`/`may_touch` evaluation
  over static + assembly-swept volumes, findings in se drc.
- **Slice 2** — `must_contact` verdict (+ seat tolerance), the
  non-seating-hard-stop finding; ties to design-state-core's discrete
  states (a hard stop is a state boundary).
- **Slice 3** — cross-configuration ordering extraction (item 3) +
  removability/escape (item 4) + volume-equivalence dedupe (item 5).

## Explicitly NOT in scope

- The situation **schema home** — `design-state-core.md` owns the stub,
  the `precis/design/` package, uids, and persist rewrites. This item
  waits for it (blocked-by) and adds the table + checks on top.
- Rung 3b of `se-off-the-shelf-fabrication.md` (driver envelopes per
  drive_type × size as capability data; assembly-order *existence* via
  `translational_dof`). Adjacent, not duplicated: 3b supplies
  tool-access volumes and a cheap existence check; this table is where
  such volumes get situation membership and verdicts.
- Path *planning* — removability takes a *candidate* path (straight-line
  insertion axes first); no motion planner.
- Ordering as an optimiser variable; hardcoding the situation list;
  contact *force* (complementarity solve — map §Complementarity).
- Scenario comparison / anything touching the §1.3 Scenario object.

## Acceptance criteria

- A design with a "maintenance" situation whose serviced part's removal
  sweep hits a neighbour yields a `must_clear` violation naming both
  volumes and the situation; the same pair passes in "operation".
- A hard-stop pair ruled `must_contact` with a 0.2 mm gap yields a
  violation; at contact (|gap| ≤ tol) it passes; a clearance-only run
  of today's drc on the same design reports nothing — the new-verdict
  gap is demonstrated in a test.
- Two blocks that overlap in sweep but clear when inserted in order
  A-then-B produce an extracted ordering constraint (A < B), not a
  violation; a pair clearing in every configuration produces neither.
- Removability against a small named bounding volume traps a part that
  a larger one frees (the concave-assembly case), test-covered.
- Two configurations with identical active swept volumes evaluate the
  rule table once (observable via check-count or cache stats).
- Situation list is data: adding a "shipping" situation requires no
  code change. mypy/ruff clean, `scripts/test --impacted` green.

## Target + blast radius

`src/precis/design/` (situation + rule tables, on the core stub) ·
`src/precis/cad/relate.py` (swept-union SDF helper over sampled poses —
additive) · `src/precis_se/drc.py` + `validate.py` (verdict findings,
governing situation in report) · `src/precis_se/persist.py` (situation
rows ride the core save path) · skills `precis-se-design-help`.
Post-deploy: live unicycle design gains an "assembly" situation and its
drc view shows verdict findings.

## Open questions / decisions log

- Sweep conservativeness: sample density for the pose union, and
  whether to dilate by sample spacing (conservative) or report exact —
  needs a Lipschitz-style bound; flagged, not decided.
- Default verdict for unruled pairs: `must_clear` (safe) or `may_touch`
  (quiet)? Leaning must_clear-with-warn, undecided.
- `must_contact` tolerance: reuse the clearance contact tolerance
  (scale-relative after units-policy-cutover) or a per-row seat
  tolerance from the fit tables?
- Who names the escape bounding volume — explicit user declaration
  only, or a default from the parent assembly's AABB with a warning?
- Volume-equivalence test: SDF sampling hash vs. configuration-set
  equality — cheap hash accuracy unproven.

Resolved (addendum A3, 2026-09-12): §2.3's clamping/fixturing and
probe-access rows land **here**, as seed situations in slice 1's
standing set (item 1 above) — they're already rows in the one-table
shape A3 generalises, not a separate fabrication-track mechanism.
