---
status: draft
title: multiscale design system — Addendum A (verbatim; A1–A10 recovered from pre-2026-09-11 notes)
prio: high
model: opus
---

# Intake routing (added 2026-09-12)

Reto's Addendum A, verbatim below (tables restored to markdown pipes;
content untouched). Supersedes the summary-derived *(added 2026-09-12)*
sections that were briefly integrated into the main spec from Reto's
itemized message — those are now pointers here. Same precedence rules
as the main spec's preamble: existing owners win; anchors below name
where each item attaches.

Reconciliation completed 2026-09-12: the main spec, the architecture
map, and the owning backlog docs (`situation-rule-tables.md`,
`complementarity-solver.md`) now point here rather than duplicating —
this addendum is the anchor of record for A1–A10.

The provenance note stands as process rule: anything remembered as
decided but appearing in neither document is lost — re-decide, never
assume.

---

# Multiscale Geometry & Optimisation Model — Addendum A

Companion to: multiscale-design-system-spec.md. That document is
unchanged; this one is additive. Provenance: recovered from notes made
before the 2026-09-11 evening session. No transcript survives for those
sessions, so this is reconstructed from the notes file alone. Status:
same standing as the main spec — architecture decided, physics partial.

Each item below carries an Anchor naming where it attaches in the main
spec, so the two can be merged later or sliced as separate tranches.

## A1. Reference targets

Anchor: §0.1, new subsection.

Two worked targets keep the architecture honest. They are not examples
chosen for exposition; they are the cases the system exists to handle.

**Mechanical.** A printed bracket or frame realised as struts and
strings rather than a solid block — material only where it is needed.
The unicycle remains the running whole-assembly example, but the
bracket is the representative part.

**Cross-scale flagship.** A bistable azobenzene structure switched
between states by two wavelengths, driving a folding-chair-like
tensegrity mechanism with springs.

This second target is load-bearing for the architecture because it
forces almost every mechanism in the main spec to meet in one artefact:

| It requires | Main spec |
|---|---|
| irradiance cast into a chemical contract | §4.8 |
| isomerisation, barriers, conformer bistability | §4.9 |
| spring and strain solving on both sides of the scale boundary | §4.9, A6 below |
| tension-only structural idioms and pre-stress | A6 below |
| two-wavelength switching with distinct branching ratios | §4.9 chromophore termination |

Use as an acceptance test: if a proposed simplification breaks this
target, it is the wrong simplification.

## A2. Standing principles 11 and 12

Anchor: §0.2, appended to the numbered list.

11. **Deliberately limit degrees of freedom.** Component DOF are capped
on purpose to keep the whole problem tractable. This is a standing
design choice, not a temporary limitation. It is what makes the
single-assembly-direction rule (§2.1) and the over-constraint counting
(§2.1, §4.9) affordable, and it is the justification for rejecting
cleverer multi-step assemblies rather than searching for them.

12. **Generate solve-friendly geometry.** The system controls the model
it emits, so it rounds corners and shapes features to keep meshing and
sphere-tracing well behaved. Numerical convenience is a legitimate term
in the cost function, not a compromise imposed afterwards. Rounding is
by constant offset; note where it compounds (§4.1).

## A3. Lifecycle scenarios

Anchor: §1.3, extending the scenario model.

There are *n* lifecycle scenarios, not a fixed three. The schema must
not hard-code the list.

Standing set:

| Scenario | What it evaluates |
|---|---|
| static assembled | the resting design under its load cases |
| operational 4D motion | the design moving through its working envelope, including deliberate hard-stop contact — a stop that is meant to be hit is a design feature, not a collision error, and the solver must not treat it as interpenetration |
| assembly | insertion paths, tool access, single shared direction |
| maintenance / diagnostic | service access, probe access, part replacement |
| fixturing | clamping faces and datums (process-dependent, §4.4) |
| test / probe access | as above |
| packaging | nesting, drop survival, flat-pack |
| end-of-life | assembly swept volumes reversed, plus may_be_destroyed |

All are implemented as rows in one scenario table — a swept volume plus
a verdict rule — evaluated in a single pass, not as separate
subsystems. This is consistent with §2.3, which already treats
fixturing, probe access and packaging that way; A3 simply states that
operational motion and maintenance belong in the same table.

Dynamic configurations are compared by volume. A mechanism with several
working configurations is scored on the union and the differences of
its swept volumes — the same machinery as tool access and beam
clearance.

Schema delta:

```
Scenario
  ...
  lifecycle_cases[]  -> LifecycleCase       # open list, not an enum

LifecycleCase
  name
  swept_volume_rule        # how the volume is generated
  verdict_rule             # what constitutes a violation
  contact_allowed[]        # interfaces where contact is INTENDED (hard stops)
  configurations[]?        # for dynamic cases; compared by volume
```

`contact_allowed` is the field that distinguishes a designed hard stop
from a collision. Without it, every mechanism that bottoms out reads as
a failure.

## A4. Tolerance budgeting and error correlation

Anchor: §2, new subsection after §2.5 Margins. Extends the stack-up
handling in §2.2.

Tolerance and stiffness are carried on the interface contracts, which
makes the traversal bidirectional and single-pass:

- **variance accumulates upward** — child tolerances sum into the
  parent's achievable tolerance;
- **tolerance budget allocates downward** — a parent's required
  tolerance is apportioned among its children.

Both happen on the same traversal. This is what makes tolerance a
contract negotiation rather than a post-hoc check, and it is the
missing half of §2.2, which currently only sums upward.

Error classification:

| Class | Behaviour | Treatment |
|---|---|---|
| uncorrelated | averages out with scale | eligible for statistical / RSS |
| systematic bias (thermal expansion, tool wear offset) | does not average out | enters the arithmetic sum regardless of stack-up mode |

**Correlation by tagging.** Correlation is tracked by tagging error
sources. Two tolerances sharing a tag — same process, same fixture,
same thermal cycle, same batch — are not independent and must not be
combined as though they were.

The tag is what makes the worst-case-versus-statistical choice
defensible per chain rather than globally. The main spec defaults to
worst-case arithmetic (§2.2) precisely because this tagging did not yet
exist; with tags in place, the statistical field becomes usable for the
chains that are genuinely independent.

```
ToleranceContribution
  value, sign_convention
  class      enum{uncorrelated, systematic}
  source_tags[]      # process_id, fixture_id, thermal_cycle_id, batch_id
```

## A5. Standing simplifications

Anchor: §2, new subsection. Listed explicitly so they are not later
mistaken for oversights.

| Simplification | Rationale and consequence |
|---|---|
| Static loads only | Dynamics enter only through the standard load-case library (shock, vibration) as equivalent static cases. |
| Fluids: rigid / solid walls | Therefore one-way coupling only — the fluid sees the geometry, the geometry does not deform in response. The fluid domain itself comes from negating the SDF (§4.1), so no remodelling of the void. |
| Convection from handbook tables | Coefficients looked up rather than solved. This terminates the thermal-fluid branch and is why a fan can be a pressure–flow curve (§4.3). |
| Corner rounding for meshing | See A2 principle 12. |
| Vacuum at molecular scale | No solvent model. Already logged in §6.1 as a gap; recorded here as the current operating assumption. |

Each of these is revisitable. The one most likely to need revisiting
first is one-way fluid coupling, since a deflectable microfluidic
membrane (§4.8) is by definition two-way.

## A6. Load-sign idioms: tension-only, compression-only, tensegrity

Anchor: §4.2, new subsection alongside representation escalation.

Members are not all bidirectional. A block or segment declares which
sign of axial load it can carry, and this changes both the solver and
the legal topology.

| Idiom | Member capability | Failure modes | Notes |
|---|---|---|---|
| Tension-only (strings, cables, ties) | tension only; zero stiffness in compression | going slack; yield | buckling does not apply; structure must be pre-stressed to stay taut |
| Compression-only (stacks of blocks, masonry-like) | compression only; joints carry no tension | overturning; separation; contact-patch escape | load path must stay inside the contact patch |
| Bidirectional (struts, tubes, brackets) | both | yield; buckling | the default |
| Spring | bidirectional with a declared rate | yield; coil bind; loss of preload | the coupling element in the flagship target (A1) |
| Tensegrity | isolated compression struts in a continuous tension network | any of the above, plus loss of pre-stress | equilibrium is satisfiable only at a particular pre-stress level, which is itself a solved parameter |

Consequences elsewhere:

**Topology completeness must respect load sign.** The "no unreacted
load" check in §2.1 is currently sign-blind. A string that would go
into compression under some load case makes the structure incomplete,
not merely highly stressed — and this must be checked per load case,
including the shock and off-axis cases from the standard library. This
is the single most important correction in this addendum, because a
sign-blind completeness check will pass structures that fall apart.

**Pre-stress is a contract field, not an afterthought.** It propagates
by statics like any other load and occupies the six components of the
contract.

**The struts-and-strings bracket is a mixed case.** The branch tree
(§4.2) supplies compression members; tension-only members are separate
library blocks; which segments become which is a discrete optimiser
choice, and therefore a move in the annealing layer.

Schema delta:

```
Block | Segment
  load_sign   enum{tension_only, compression_only, bidirectional, spring}
  pre_stress?             # required preload, propagated by statics
  spring_rate?            # for spring
  slack_check             # tension_only: verdict per load case
```

## A7. Bayesian optimisation as the adaptive surrogate

Anchor: §3.1, naming the surrogate the main spec leaves generic.

The main spec says "cheap surrogate early, full relaxation only for
promising candidates" without saying what the surrogate is. It is
Bayesian optimisation.

Where an evaluation is expensive — a full FEA solve, a DFT junction, a
coupled screen that came back active — the surrogate model decides
where to spend the next evaluation, rather than the annealing schedule
deciding blindly.

Three places it earns its keep:

1. Inside the inner continuous solve, as the cheap stand-in before full
   relaxation.
2. On contested decisions (§3.8) — the decisions whose runner-up was
   within ~5% are exactly where another evaluation changes the answer,
   so the acquisition function should be pointed at them.
3. Adaptive Pareto front extension (§3.4) — when a block front is
   queried outside its sampled envelope, the acquisition function
   chooses the new sample point rather than extending by a fixed rule.

Note the interaction with §3.3: Chebyshev scalarisation is non-smooth,
which is awkward for a surrogate built on smoothness assumptions.
Either fit the surrogate to the individual objectives and scalarise
afterwards, or accept degraded surrogate quality near the kinks.
Fitting per objective is preferred and costs little, since the
objectives are already tracked separately for the Pareto machinery.

## A8. Process catalogue extension

Anchor: §4.4, filling out the process list.

The main spec describes the process object fully but only instantiates
printing, milling and moulding. The full standing set:

| Process | Characteristic constraint | Emits |
|---|---|---|
| 3D printing (FDM/SLA) | build orientation, overhang angle, wall thickness, layer anisotropy | support volume, build-plate footprint |
| CNC milling | cutter reachability, minimum internal radius, pocket depth | clamping faces, datum, holder swept volume |
| Turning / lathe | axisymmetry about a single axis; off-axis features force a second setup | chuck grip length, tailstock access |
| Injection moulding | pull direction, draft, undercut, even wall thickness | ejector marks, parting line, the mould as its own milled part |
| Laser cutting | 2D sheet; kerf width; constant thickness; material-dependent edge taper and heat-affected zone | nesting layout, kerf offset |
| Die cutting | 2D sheet; minimum feature size and web width; die cost is fixed setup amortised over quantity | nesting layout, die tooling cost |
| Lamination / bonding (microfluidic) | layer registration; bonding method coupled to chemistry (§4.8) | adhesive or weld interface, alignment features |
| Chemical apparatus / synthesis route | reaction compatibility, yield, reaction speed | see §4.9 and A9 |

**One kernel, three processes.** Laser cutting, die cutting and
microfluidic lamination share the 2.5D sheet shape: a 2D layout problem
per layer plus through-features. Build that kernel once.

**Composability is asserted, not proven.** §4.4 states that a process
sequence has capability = union and cost = sum. That is stated
optimistically and should be validated pair by pair — turning then
milling composes cleanly; moulding then laser cutting almost certainly
does not.

**Intermediate representations are first-class outputs**, not merely
the coarse and final states. The stick-figure sketch (§5.8) is the
canonical one, and any phase boundary should be exportable.

## A9. Non-bonded interaction terms, ratchets and hysteresis

Anchor: §4.9, extending the molecular tier.

π-stacking is one member of a family, not a special case. Each is
modelled as a wanted-or-unwanted term — the same interaction is an
objective in one design and a penalty in another, so the sign is a
design input.

| Term | Notes |
|---|---|
| π-stacking | shares its representation with the π-system object (§4.9) |
| electrostatic / charge patterns | patterned charge as a recognition and positioning mechanism |
| hydrogen bonding | directional, so it carries a geometry story as well as an energy |
| van der Waals / steric | the baseline exclusion term |

**Behavioural idioms.** Two idioms the model must support directly
rather than hope to see emerge:

**Molecular ratchets.** Directional motion with a preferred sense.
Requires asymmetric barriers between conformers, so it is read straight
off the state-and-barrier graph already used for bistability and
catalysis (§4.9): a ratchet is the case where forward and reverse
barriers differ. No new machinery, but the barrier graph must be
directed — storing one barrier per edge is insufficient.

**Bistable snap, with hysteresis.** The molecular analogue of a
mechanical over-centre snap. The switching threshold differs going up
and coming down, so state is path-dependent and cannot be evaluated as
a function of the current geometry alone.

This is the one place in the entire model where history is
load-bearing. Everywhere else a design state is fully determined by its
parameters. Any cache keyed on "state given configuration" — and there
are several implied by §1.5 and §3.4 — will be wrong for these blocks.
Hysteretic blocks must be flagged and their results keyed on the path,
not the configuration.

**Spacer and building-block library.** Known building blocks with
DFT-computed spacers — C₂H₄-type spacers, azobenzene photoswitches and
similar — each characterised once and cached with provenance, so common
junctions are never re-solved. This is the seed content for the library
described in §4.9, ahead of bulk ingestion from compound databases.

**Synthesisability: two independent routes.** Both are wanted, and they
have entirely different constraint sets:

| Route | Constraint set |
|---|---|
| click chemistry | reaction compatibility, library filtering, yield |
| atomic assembly arm | reachability, sequence, positioning accuracy |

A fragment may be reachable by one, both, or neither. The route is a
discrete optimiser choice with its own cost model, and a design that is
only reachable by the assembly arm is a different proposition
commercially from one reachable by click chemistry.

## A10. Additions to open items

Anchor: §6.2.

- How far processes actually compose — see A8.
- Yield and reaction speed on the synthesis side. Raised early, still
  unresolved, and the item most likely to invalidate an otherwise
  feasible molecular design.
- Two-way fluid coupling, needed as soon as a deflectable membrane is
  modelled rather than characterised (A5).

## Provenance note

The evening session of 2026-09-11 is the only one with a surviving
transcript. Everything in this addendum was recovered from the running
notes file, which is a summary rather than a verbatim record. Items
discussed in earlier sessions but never written to the notes are
unrecoverable; if something is remembered as decided and does not
appear in either document, it is genuinely lost and should be
re-decided rather than assumed.
