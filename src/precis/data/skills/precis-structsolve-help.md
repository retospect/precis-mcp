---
id: precis-structsolve-help
family: se
title: precis — structural solver verdicts (unilateral equilibrium, completeness, bistability)
summary: reading the se structural-solver output — taut / slack / bearing / separated / seated / unseated statuses, complete / unseated / incomplete / mechanism verdicts, bistability and what its barrier figure means
answers:
  - how are slack cables handled when se checks a cable/strut network under load?
  - what do the complementarity statuses taut / slack / bearing / separated / seated / unseated mean?
  - what does a mechanism verdict mean and how do I find the unresisted node and axis?
  - why is a tie that drives into compression reported incomplete rather than as a stressed member?
  - what does bistable=True mean in an se bistability probe, and why are two free-length states usually not bistable?
  - is the bistability barrier a real snap-through barrier?
applies-to: se views stability / drc / validate (load path, completeness, bistability); the se op formfind
kinds: [se]
status: active
tags: [design]
---

# precis-structsolve-help — structural solver verdicts

The structural solvers run behind the se views (`view='stability'`,
`'drc'`, `'validate'`; arguments in `precis-se-help`) and the se op
`formfind`. They are unit-agnostic (metres/newtons and ångström/nN give
the same verdicts). This page says what their verdicts mean.

## Unilateral equilibrium — taut, slack, bearing, separated

Small-displacement equilibrium of a pin-jointed network whose members
are one-sided or two-sided. Member idioms and the status each can report:

| idiom | engaged | not engaged |
|---|---|---|
| `tension_only` | `taut` | `slack` |
| `compression_only` | `bearing` | `separated` |
| `must_contact` | `seated` | `unseated` (a failed hard stop) |
| `bidirectional` | `taut` / `bearing` by force sign | — |

- Engagement is read off the solved force. A one-sided member installed
  on its illegal sign (a tie shorter than its free length) counts as
  engaged only if the solve stretches it strictly past its free length.
- Small-displacement model: member directions are fixed at the given
  geometry; prestress stiffens transverse directions only, never the
  member axis.
- No complementary equilibrium is reported as a refusal. A singular
  refusal names the unresisted free coordinates, e.g. `unresisted free
  coordinates: node 0 x`. A malformed request is a call error to fix,
  never physics.

## Completeness — per-load-case verdicts

An all-zero load case checks prestress alone.

| verdict | meaning | next move |
|---|---|---|
| `complete` | a legal-sign equilibrium exists; a slack cable is fine | — |
| `unseated` | it solves, but a `must_contact` stop let go (the report lists them) | more preload or a different stop |
| `incomplete` | no legal equilibrium, but all-bidirectional carries the case; the culprits are one-sided members driven onto their illegal sign | re-sign or brace the culprits |
| `mechanism` | even all-bidirectional fails; the cause names the unresisted `node <i> <axis>` | add a member or support on that coordinate |

`complete` means a load path exists, not that members are sized: no
strength, buckling-capacity or cable-sag check. The report's notes say
what was and was not checked; surface them.

## Bistability — one structure, two equilibria?

Probes whether two free-length assignments are two stable equilibria of
one structure, and gives an energy-difference estimate.

- `bistable=True` requires both states stable and distinct, and at
  least one state also a stable equilibrium of the other's free
  lengths. Two assignments that each have their own single equilibrium
  (a photoswitch's trans and cis lengths, two prestress levels) are two
  structures: `bistable=False`, notes say "different structures".
- `stable` is second order: a state can solve yet be unstable when
  compression prestress drives a transverse stiffness negative.
- The barrier is always exactly `|E_a − E_b|` (a straight-line path in
  a linear model is convex) — an actuation-energy scale, never a
  snap-through barrier. A real snap-through barrier needs a
  geometrically nonlinear continuation, where member directions follow
  the displaced geometry and folds are visible; the se views do not
  offer one.

## Other solvers

| solver | does |
|---|---|
| form-finding | force-density form-finding: geometry from topology, force densities and anchors (se op `formfind`) |
