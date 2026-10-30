---
status: draft
title: "se/cad: suggest_checks — verification probes derived from declared intent"
pillar: 3d-design
---

# se/cad: suggest_checks

Source gripes: gr521590.

## Motivation / why

CADCodeVerify (pa521196, pc5380840/pc5380931) generates yes/no
verification questions from the design description, answers them on the
object and folds failures into actionable feedback each iteration;
CADSmith (pa521198, pc5380752) drives its outer correction loop with
kernel measurements (bbox, volume, face/edge/vertex counts, validity)
plus rendered inspection.

Ours: `validate`/`drc` run fixed rules; observation rays and measurands
must be hand-authored — nothing derives checks from the design's declared
intent (ports, pockets, notes), and nothing proposes a ray, so an agent's
verify loop starts blank.

## In scope

- A `suggest_checks`/`propose_rays` affordance seeded by declarations
  (ruled by Reto 2026-10-28):
  - each threaded port ⇒ a `bolt_axis` ray;
  - each pocket ⇒ a `region` ray;
  - each property named by `measurand_unchecked` ⇒ a proposed measurand.

## Explicitly NOT in scope

- Accepting or applying proposals — the affordance proposes; the agent
  authors the check.
- Free-form check synthesis beyond the three seed rules (e.g. notes →
  checks); the ruled seed set is ports, pockets and unchecked
  properties.
- The probe machinery the proposed rays run on — landed in
  `src/precis/cad/inspect.py` ([cad-inspect](cad-inspect.md), gr526015).

## Acceptance criteria

- On a design with a threaded port, `suggest_checks` proposes a
  `bolt_axis` ray for it.
- On a design with a pocket, it proposes a `region` ray.
- On a design where `drc` reports `measurand_unchecked` for a property,
  it proposes that property as a measurand.
- Proposals are deterministic — same declarations, same proposals.

## Target + blast radius

se/cad check surface (`view='drc'`, `validate`, `measurand_unchecked`),
`precis.cad.inspect` probes, se workspace declarations (ports, pockets).

## Open questions / decisions log

- **[decided, Reto 2026-10-28 via GRIPE_FIX]** Build `suggest_checks`
  seeded by declarations: threaded port → `bolt_axis` ray, pocket →
  `region` ray, `measurand_unchecked` property → proposed measurand.
- Open: verb name — `suggest_checks` vs `propose_rays` (the gripe offers
  both).
- Open: surface — a `view=` arg, a verb on `se`/`cad`, or a section in
  `view='drc'` output.
- Open: does a proposal carry a full ray spec the agent can put
  verbatim, or a template it fills in?
