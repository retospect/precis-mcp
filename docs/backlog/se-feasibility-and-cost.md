---
status: draft
title: se feasibility and cost — one check registry, hard floor plus graded objective
prio: high
model: opus
---

# se: can we make it, can we assemble it, and how much does it hurt?

Design session 2026-09-05 (Reto + agent), precious-juggling-map worktree,
from Reto's framing:

> These all mirror drc/cost function in the pcb world — if we can not make
> it or we can not assemble/synthesize it it's a useless solution. If we
> can assemble it with binding and a lot of pain, it's a working solution
> but bad.

That is the whole architecture of this item, and it is worth stating
plainly because it is *not* the obvious design. The obvious design is a
pile of independent analyses (tolerance stack-up, reliability, assembly
study), each with its own report. The correct design is **one check
registry with two output tiers**, because the tiers are the same
question asked at different severities:

- **Hard — DRC.** The design cannot be realized. Not a worse solution: not
  a solution. Emits an `error` finding, and a candidate carrying one is
  removed from consideration rather than ranked last.
- **Soft — objective.** The design *can* be realized, at a price: it binds,
  it needs a jig, it needs a 0.05 mm placement accuracy, it wears out in a
  year. Emits a scored penalty, and a design with a big one is a working
  design that we should want to beat.

Companion to `se-kind.md` (the kind) and
`se-off-the-shelf-fabrication.md` (the modes and the bought-part
machinery, which is where the first hard checks live). This item owns the
**evaluation layer above both**.

## The insight that makes it one system, not two

**The boundary between hard and soft is a capability number, not a
property of the check.** "This joint needs the two holes located within
0.1 mm of each other" is:

- a **penalty** if the process holds 0.05 mm (buildable, tight, annoying);
- an **error** if the process holds 0.5 mm (it will not go together).

Same check, same computed quantity. What moves it across the line is a
row in the capability table. So the registry stores *one* check that
returns a magnitude plus the capability field it is measured against, and
**severity is resolved at read time against the capability row for the
design's declared process** — never hardcoded at the check site.

Two things fall out of that, both of which we want:

1. **Changing the process re-grades the design without re-running
   analysis.** Move a part from `laser/acrylic` to `cnc-2.5ax` and the
   same stack-up becomes a penalty instead of an error.
2. **The DRC message can quote the headroom it spent**, exactly as
   `pcb_capabilities.json`'s two-tier rows already let the pcb digest say
   "0.15 mm against a 0.10 mm minimum" instead of a bare pass/fail. That
   is the shape to copy.

This is the same posture as the mode realizability predicates in
`se-off-the-shelf-fabrication.md` engine 3 — those are the hard tier of
"can we make it". This item adds "can we *assemble* it", which nothing in
the tree touches today, and the soft tier for both.

## Fit class is a tolerance declaration, not a hole diameter

Resolved 2026-09-05 (Reto): **fit class is picked per assembly, and
picking it sets the tolerances.**

That is a sharper claim than the fit-class resolution recorded in
`se-off-the-shelf-fabrication.md`, and it changes where the setting
lives. A fit class is not a number the hole generator looks up. It is a
declaration about the **assembly compliance budget**:

| M6 clearance | Ø | diametral clearance | position slack per hole |
|---|---|---|---|
| house (`d + 0.2`) | 6.2 | 0.2 | ±0.1 |
| ISO 273 fine | 6.4 | 0.4 | ±0.2 |
| ISO 273 medium | 6.6 | 0.6 | ±0.3 |
| ISO 273 coarse | 7.0 | 1.0 | ±0.5 |

The right-hand column is the thing the tolerance analysis consumes. So
rung 3's `screw` stamping does not just emit geometry — **it emits a
tolerance relation alongside every hole it stamps**, and the fit class is
what parameterizes both. That generalizes engine 2's "a joint stamps
features into both members" one level up: *a joint stamps features and
the tolerance relations that make those features meaningful.* A stamped
hole without its position tolerance is the same folklore as a press fit
without an interference relation — which se already refuses
(`MECHANISMS['press']['demands_relation']`).

Storage: a design-level facet (`fit_class`, default `house`), overridable
per connect. **Not** a global constant — a chassis and a jig want
different answers, and the whole point of the per-assembly choice is that
it is a decision with a recorded reason.

## The three analyses

### 1. Assembly tolerance stack-up

**Mostly built, pointed the wrong way.** `precis_se/measures.py` already
does worst-case stack-up over *declared* measure relations, and
`drc.py` already reports unresolvable/cyclic ones. Two additions:

- **Derive the chain from the joint graph** instead of requiring it to be
  hand-declared. Today a designer must know which measures to relate;
  after this, a chain of joints from datum to feature *is* the chain, and
  the stack-up walks it.
- **Statistical alongside worst-case.** Worst-case is correct and
  useless at scale (it says a 20-joint chain can't hold anything); RSS is
  optimistic and standard. Report both, never one — the gap between them
  is itself the interesting number, and picking one silently is how
  tolerance analysis lies.

Hard tier: the stack exceeds the clearance the fit class bought → it will
not assemble. Soft tier: it fits, with less margin than the process
comfortably holds.

### 2. Assembly motion / DFM-DFA

`se-off-the-shelf-fabrication.md` already scopes the hard half —
**assembly-order existence**: each part insertable along *some* free
direction against the partial assembly (`relate.translational_dof`
against a growing union), an existence check and explicitly not path
planning. That stays the hard floor: no insertion direction exists ⇒ the
thing cannot be built, regardless of how good it looks.

The soft half is new and is what Reto's "a lot of pain" names. The
established vocabulary is **Boothroyd-Dewhurst DFA**: per part, a
handling penalty (is it symmetric? does it nest/tangle? is it under
2 mm?) plus an insertion penalty (is it self-locating? does it need
holding? is access obstructed? is it blind?). The score is a **time**,
which is why it composes with cost (below) rather than being a separate
verdict.

What we can actually compute from what se stores, without new data:

- **obstructed access** — already the tool-access check (a swept driver
  envelope, `drive_type` × size), which is in the fabrication item;
- **needs holding** — a part with no `rigid` joint until a later step;
- **blind insertion** — the insertion direction has no line of sight;
- **re-orientation count** — how many times the assembly must be flipped
  to follow the insertion order. This one is cheap, is the single
  biggest DFA lever in practice, and nothing else in the tree measures it.

Deliberately *not* in scope: fastener-count minimization advice,
part-consolidation suggestions ("could these two be one part?"). Those
are design proposals, not analyses, and they belong to the propose loop.

### 3. Reliability — scoped honestly, because most of it would be folklore

This is where the pcb analogy is weakest and needs saying so. MIL-HDBK-217
part-count prediction works for electronics because failure rates are
published per part family. **Nobody publishes an MTBF for a bolt.** A
generic "MTBF of this welded frame" would be an invented number wearing a
unit, which is the exact failure this codebase refuses everywhere else.

What is real, in descending order of confidence:

- **Bearing L10 life — computable *today*, and nearly free.** The chain
  already exists in shipped code: a `bearing` mechanism demands a BOM
  line (rung 1, `joints.MECHANISMS['bearing']['demands_bom']`), that line
  binds a `component`, `dynamic_load_rating` is a seeded core spec on the
  `bearing` category (migration 0093), and the joint's load comes from
  the `objectives` vocabulary (`force`/`torque`/`cycles`/`duty`, slice 3).
  L10 = (C/P)³ × 10⁶ revolutions is then arithmetic. **This is the
  cheapest genuine reliability result in the whole item and should be the
  first one built.**
- **Seal and elastomer service life** — published against temperature and
  medium; `temperature_max` is already a seeded spec.
- **Fatigue on a loaded structural member** — real engineering, but it
  needs stress, which needs FEA or a closed-form case. Out of scope until
  a load path exists; name it here so it is not re-derived.
- **A system MTBF rollup over the assembly tree** — only meaningful once
  the leaves carry real per-leaf numbers, i.e. after the above. Series
  reliability over `contains` is the easy part; having honest leaves is
  the hard part. **Do not build the rollup first** — it would produce
  confident totals from absent data, and `component`'s BOM rollup already
  has the right posture to copy ("priced: N of M" — say how much of the
  answer is actually grounded).

## The objective: a named vector, not a scalar

The soft tier's terms are in **different units** and must not be silently
summed:

- **money** — parts (`unit_cost` via the existing `view='bom'` rollup),
  stock consumed, cut charges, offcut waste;
- **time** — DFA handling + insertion seconds, machine time, setup count;
- **risk** — L10 hours below the duty requirement, tolerance margin
  consumed, single points of failure.

Monetizing these into one number requires shop rates and a risk appetite
that are **not** properties of the design. So: report a **named vector,
each term carrying its provenance and the fraction of inputs that were
actually grounded**, and let a selection layer weight it. That is the
same discipline as the BOM rollup's "priced: N of M", and it keeps the
comparison honest when two candidates are grounded to different depths.

A scalar may be derived *on request* with explicit weights. It is never
the primary artifact, and a weight set is a recorded decision, not a
default.

## Ship order

1. **The check registry + severity-against-capability resolution.** The
   spine. Nothing else is worth building until a check can say "0.15
   against a 0.10 floor" and have the severity fall out of the capability
   row rather than an `if`.
2. **Fit class as a design facet emitting tolerance relations** — rides
   with rung 3's `screw` stamping, which is its first producer.
3. **Bearing L10.** Small, real, entirely from shipped data; the proof
   that the vector's `risk` term can be grounded.
4. **Joint-graph-derived stack-up, worst-case + RSS.**
5. **Assembly-order existence** (hard) — from the fabrication item.
6. **DFA scoring**, re-orientation count first.
7. **The objective vector**, assembled from whatever terms exist by then.

Rungs 1–3 do not depend on the fabrication item's rungs 4–5. Rung 5 here
is rung 3 there; build it once, in whichever lands first.

## Deferred, named so they are not re-derived

FEA and any stress-derived quantity; fatigue life; thermal analysis;
part-consolidation proposals (propose loop, not analysis); assembly *path*
planning (existence only, as already decided); monetized scalar objectives
as a default; MTBF for anything whose leaves have no published failure
rate; Monte Carlo tolerance analysis beyond RSS (worth it only once RSS
is shown insufficient on a real design).

## Open questions for Reto

- **Where does `fit_class` sit** — a design-level facet with per-connect
  override (my lean), or purely per-connect with no design default?
- **Is the re-orientation count worth its own hard floor?** ("more than N
  flips" as an error rather than a penalty) — my lean is no: it is the
  archetypal *painful but working* case, which is exactly what the soft
  tier exists for.
